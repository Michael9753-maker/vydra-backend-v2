"""
worker_manager.py

Robust worker pool for VYDRA.

Behavior:
- Resolves the JOB_QUEUE / JOBS / JOBS_LOCK stores at runtime (avoids import-time circulars).
- Resolves a download callable from download_manager in a few fallback ways.
- Starts WORKER_COUNT daemon threads that consume job IDs and update job records.
- Logs extensively via logger_manager.
"""

import importlib
import threading
import time
import traceback
from typing import Any, Callable, Optional

from logger_manager import log_event, log_exception

# Worker pool configuration (tweak as needed)
WORKER_COUNT = 6
WORKER_RESTART_DELAY = 1.0  # seconds before retrying after an error
JOB_RESOLVE_RETRY_DELAY = 0.5  # seconds to wait if job store isn't found immediately
JOB_RESOLVE_MAX_ATTEMPTS = 6


# ---------- Runtime resolvers ----------
def _resolve_job_store():
    """
    Attempt to locate JOB_QUEUE, JOBS dict and JOBS_LOCK.
    Try multiple modules:
      1) job_queue (if you extracted the job store)
      2) app (your Flask app module)
    Returns (JOB_QUEUE, JOBS, JOBS_LOCK) or (None, None, None) if not found yet.
    """
    # Try import job_queue module first
    candidates = ["job_queue", "app"]
    for name in candidates:
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        # Check attributes
        JOB_QUEUE = getattr(mod, "JOB_QUEUE", None)
        JOBS = getattr(mod, "JOBS", None)
        JOBS_LOCK = getattr(mod, "JOBS_LOCK", None)
        if JOB_QUEUE is not None and JOBS is not None and JOBS_LOCK is not None:
            return JOB_QUEUE, JOBS, JOBS_LOCK
    # not found
    return None, None, None


def _resolve_download_callable() -> Callable[..., Any]:
    """
    Resolve a callable to perform a download. Tries the common names in download_manager:
      - download_manager.process (preferred if present)
      - download_manager.process_download
      - download_manager.download_video
      - download_manager.get_default_manager().download
    Returns a callable that accepts kwargs (url=..., user_id=..., meta=..., job_id=..., **kwargs)
    and returns a result (path or dict).
    If none found, raises ImportError.
    """
    try:
        dm = importlib.import_module("download_manager")
    except Exception as e:
        raise ImportError(f"download_manager module not importable: {e}")

    # Preferred: process(...)
    if hasattr(dm, "process"):
        return getattr(dm, "process")

    if hasattr(dm, "process_download"):
        return getattr(dm, "process_download")

    if hasattr(dm, "download_video"):
        # wrap to accept kwargs
        func = getattr(dm, "download_video")

        def _wrap(**kwargs):
            url = kwargs.get("url")
            quality = kwargs.get("quality", "best")
            audio_only = kwargs.get("audio_only", False)
            return func(url, quality=quality, audio_only=audio_only)

        return _wrap

    # Try get_default_manager().download
    if hasattr(dm, "get_default_manager"):
        try:
            mgr = dm.get_default_manager()
            if hasattr(mgr, "download"):
                def _mgr_download(**kwargs):
                    # call download with keyword-friendly mapping
                    url = kwargs.get("url")
                    job_id = kwargs.get("job_id", None)
                    quality = kwargs.get("quality", "best")
                    audio_only = kwargs.get("audio_only", False)
                    # pass through meta/enhancement if present
                    call_kwargs = {"url": url, "quality": quality, "audio_only": audio_only}
                    # include job_id if manager accepts it (many versions do)
                    try:
                        return mgr.download(**{**call_kwargs, **({"job_id": job_id} if job_id else {})})
                    except TypeError:
                        # fallback: positional
                        return mgr.download(url, quality, audio_only)
                return _mgr_download
        except Exception:
            pass

    raise ImportError("No usable download entrypoint found in download_manager.py")


# ---------- Worker thread ----------
class Worker(threading.Thread):
    def __init__(self, worker_id: int, download_callable: Callable[..., Any]):
        super().__init__(daemon=True)
        self.worker_id = worker_id
        self.download_callable = download_callable

    def _safe_time_iso(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def run(self):
        log_event("WORKER_START", f"Worker {self.worker_id} started")

        # Resolve job store (retry a few times if not present yet)
        attempts = 0
        JOB_QUEUE = JOBS = JOBS_LOCK = None
        while attempts < JOB_RESOLVE_MAX_ATTEMPTS:
            JOB_QUEUE, JOBS, JOBS_LOCK = _resolve_job_store()
            if JOB_QUEUE is not None:
                break
            attempts += 1
            time.sleep(JOB_RESOLVE_RETRY_DELAY)

        if JOB_QUEUE is None:
            log_event("WORKER_NO_JOB_STORE", f"Worker {self.worker_id} could not find job store after {attempts} attempts; will retry in-loop.")
        # Enter main loop (will try resolving each iteration if needed)
        while True:
            job_id = None
            try:
                # (re-)resolve store if missing
                if JOB_QUEUE is None or JOBS is None or JOBS_LOCK is None:
                    JOB_QUEUE, JOBS, JOBS_LOCK = _resolve_job_store()
                    if JOB_QUEUE is None:
                        # sleep briefly to avoid busy-loop when store not ready
                        time.sleep(JOB_RESOLVE_RETRY_DELAY)
                        continue

                # BLOCK until we get a job id
                job_id = JOB_QUEUE.get(block=True)

                # load job record
                with JOBS_LOCK:
                    job = JOBS.get(job_id)

                if not job:
                    log_event("WORKER_INVALID", f"Worker {self.worker_id} got unknown job_id {job_id}")
                    # notify queue that task done to avoid stuckness if job queue expects that
                    try:
                        JOB_QUEUE.task_done()
                    except Exception:
                        pass
                    continue

                url = job.get("url")
                user_id = job.get("user_id")
                meta = job.get("meta", {}) or {}

                # mark started
                with JOBS_LOCK:
                    job["status"] = "downloading"
                    job["started_at"] = self._safe_time_iso()
                    # ensure attempts counter exists
                    job["attempts"] = int(job.get("attempts", 0)) + 1

                log_event("WORKER_JOB_START", f"Worker {self.worker_id} processing job {job_id} url={url}")

                # call downloader (best-effort mapping)
                # allow the download callable to accept kwargs - pass meta elements as kwargs
                dkwargs = {"url": url, "user_id": user_id, "meta": meta, "job_id": job_id}
                # flatten some common meta fields as top-level hints
                if isinstance(meta, dict):
                    if "quality" in meta:
                        dkwargs["quality"] = meta["quality"]
                    if "audio_only" in meta:
                        dkwargs["audio_only"] = meta["audio_only"]
                    if "audio_format" in meta:
                        dkwargs["audio_format"] = meta["audio_format"]
                    if "enhancement" in meta:
                        dkwargs["enhancement"] = meta["enhancement"]

                result = None
                try:
                    result = self.download_callable(**dkwargs)
                except TypeError:
                    # try calling with only url (older APIs)
                    try:
                        result = self.download_callable(url)
                    except Exception as ex:
                        raise

                # Mark finished (successful)
                finished_at = self._safe_time_iso()
                with JOBS_LOCK:
                    job["status"] = "finished"
                    job["result"] = result
                    # if result looks like a path or dict containing filename, try to set file/name
                    if isinstance(result, str):
                        job["file"] = result.split("/")[-1]
                    elif isinstance(result, dict):
                        job["file"] = result.get("file") or result.get("filename") or result.get("path")
                    job["finished_at"] = finished_at
                    job["progress"] = {"percent": 100}

                log_event("WORKER_JOB_DONE", f"Worker {self.worker_id} finished job {job_id}")

            except Exception as e:
                # record error, log details, and continue (with restart delay)
                try:
                    log_exception("WORKER_ERROR", f"Worker {self.worker_id} error: {e}\n{traceback.format_exc()}")
                except Exception:
                    pass

                if job_id is not None and JOBS_LOCK is not None:
                    try:
                        with JOBS_LOCK:
                            job = JOBS.get(job_id)
                            if job is not None:
                                job["status"] = "error"
                                job["error"] = str(e)
                                job["finished_at"] = self._safe_time_iso()
                    except Exception:
                        pass

                time.sleep(WORKER_RESTART_DELAY)
                # continue loop to pick next job / retry resolving stores


# ---------- Pool starter ----------
def start_workers() -> None:
    """
    Start the worker pool. Safe to call multiple times; subsequent calls do nothing.
    """
    # store started flag on module to avoid double-start
    if getattr(start_workers, "_started", False):
        return
    try:
        download_callable = _resolve_download_callable()
    except Exception as e:
        log_event("WORKER_RESOLVE_ERROR", f"Could not resolve download callable: {e}")
        # still attempt to start workers; each worker will try to resolve later
        download_callable = lambda **kw: (_raise := (_ for _ in ()).throw(ImportError("download callable not found")))

    log_event("WORKER_POOL_START", f"Starting {WORKER_COUNT} workers...")

    for i in range(WORKER_COUNT):
        w = Worker(worker_id=i + 1, download_callable=download_callable)
        w.start()

    start_workers._started = True
    log_event("WORKER_POOL_READY", f"{WORKER_COUNT} workers running")
