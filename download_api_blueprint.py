from __future__ import annotations
import os
import atexit
import logging
import uuid
import sys
import importlib
from typing import Any, Dict, List, Optional
from flask import Blueprint, request, jsonify

# CORS decorator fallback
try:
    from flask_cors import cross_origin
except Exception:
    def cross_origin(*a, **kw):
        def _deco(f):
            return f
        return _deco

# attempt to import download_manager for progress/shutdown operations (best-effort)
try:
    from download_manager import get_default_manager as _dm_get_default
except Exception:
    _dm_get_default = None

# utils fallback for cleanup
try:
    import utils as _utils
except Exception:
    _utils = None

LOG = logging.getLogger("download_api")
if not LOG.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s: %(message)s")

# Blueprint
download_bp = Blueprint("download_api", __name__)

# Use a safe default for batch size; will prefer env var
MAX_BATCH_URLS = int(os.getenv("VYDRA_MAX_BATCH_URLS", "20"))
SHUTDOWN_SECRET = os.getenv("VYDRA_SHUTDOWN_SECRET", os.getenv("SECRET_KEY", ""))

# ----------------------
# Helpers
# ----------------------
def _get_main_app():
    """
    Return the already-loaded `app` module object.
    Try several safe strategies to locate the main app module to avoid circular-import issues.
    """
    # common case: app was imported as module 'app'
    main_app = sys.modules.get("app")
    if main_app:
        return main_app
    # try to import dynamically (best-effort)
    try:
        main_app = importlib.import_module("app")
        return main_app
    except Exception:
        pass
    # last resort: look for a Flask app instance in sys.modules
    for m in list(sys.modules.values()):
        try:
            if hasattr(m, "app"):
                return m
        except Exception:
            continue
    return None

def _make_job_id() -> str:
    main_app = _get_main_app()
    if main_app and hasattr(main_app, "gen_job_id"):
        try:
            return main_app.gen_job_id()
        except Exception:
            LOG.exception("gen_job_id failed on main_app; falling back to uuid")
    return uuid.uuid4().hex

def _record_queue_entry(job_id: str, url: str, user_id: Optional[str]):
    """Best-effort: call app.record_download_history or app.save_history_entry if present."""
    try:
        main_app = _get_main_app()
        if main_app:
            if hasattr(main_app, "record_download_history"):
                try:
                    main_app.record_download_history(job_id, url, user_id or "guest", "queued", None, None, None, None)
                    return
                except Exception:
                    LOG.exception("record_download_history failed")
            if hasattr(main_app, "save_history_entry"):
                try:
                    from datetime import datetime, timezone
                    now = datetime.now(timezone.utc).isoformat()
                    entry = {
                        "user_id": user_id or "guest",
                        "job_id": job_id,
                        "title": url,
                        "file": None,
                        "thumbnail": None,
                        "caption": None,
                        "hashtags": [],
                        "created_at": now
                    }
                    main_app.save_history_entry(entry)
                except Exception:
                    LOG.exception("save_history_entry fallback failed")
    except Exception:
        LOG.exception("_record_queue_entry unexpected failure")

def _ensure_job_record_in_main_app(main_app, job_id: str, user_id: Optional[str], url: str, meta: dict):
    """
    Ensure the job is recorded in the main app's JOBS store using the canonical make_job_record if available,
    otherwise fall back to a thread-safe insert using main_app.JOBS_LOCK.

    Returns True if job exists in main_app.JOBS afterwards.
    """
    try:
        # Prefer canonical API
        if hasattr(main_app, "make_job_record"):
            try:
                main_app.make_job_record(job_id, user_id or "guest", url, meta=meta)
                LOG.info("✅ main_app.make_job_record succeeded for %s", job_id)
            except Exception:
                LOG.exception("main_app.make_job_record raised for %s; falling back to direct insert", job_id)
        # Check if job exists now
        try:
            jobs = getattr(main_app, "JOBS", None)
            if jobs is not None and job_id in jobs:
                return True
        except Exception:
            pass
        # Fallback: attempt a safe insert
        try:
            lock = getattr(main_app, "JOBS_LOCK", None)
            job_entry = {
                "job_id": job_id,
                "user_id": user_id or "guest",
                "url": url,
                "status": "queued",
                "attempts": 0,
                "retries_left": getattr(main_app, "MAX_RETRIES", 2),
                "progress": {"percent": 0},
                "file": None,
                "error": None,
                "created_at": datetime_now_iso(),
                "meta": meta,
            }
            if lock:
                with lock:
                    main_app.JOBS[job_id] = job_entry
            else:
                main_app.JOBS[job_id] = job_entry
            LOG.info("✅ inserted fallback job_entry into main_app.JOBS for %s", job_id)
            return True
        except Exception:
            LOG.exception("Fallback JOB insert failed for %s", job_id)
    except Exception:
        LOG.exception("_ensure_job_record_in_main_app unexpected failure for %s", job_id)
    return False

# ----------------------
# Routes
# ----------------------
@download_bp.route("/", methods=["GET"])
def api_root():
    return jsonify({"message": "VYDRA Backend API is running"}), 200

@download_bp.route("/download", methods=["POST"])
@cross_origin()
def start_download():
    """
    Create job(s) and enqueue into app.JOB_QUEUE

    Accepts JSON body with keys:
      - url: str | list | newline-separated
      - user_id: optional
      - mode, quality, audio_only, audio_format, enhancement (optional meta)
    """
    data: Dict[str, Any] = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "invalid json body"}), 400

    url = data.get("url")
    user_id = data.get("user_id")
    mode = data.get("mode", "speed")
    quality = data.get("quality")
    audio_only = bool(data.get("audio_only", False))
    audio_format = data.get("audio_format")
    enhancement = data.get("enhancement")

    if not url:
        return jsonify({"error": "url is required"}), 400

    # normalize to list
    urls: List[str] = []
    if isinstance(url, (list, tuple)):
        urls = list(url)
    elif isinstance(url, str) and ("\n" in url or "\r" in url):
        lines = [u.strip() for u in url.splitlines()]
        urls = [u for u in lines if u]
    else:
        urls = [url]

    if len(urls) > MAX_BATCH_URLS:
        return jsonify({"error": f"batch size limit exceeded (max {MAX_BATCH_URLS})"}), 400

    # dynamic app lookups
    main_app = _get_main_app()
    ul = getattr(main_app, "ul", None) if main_app else None

    # try to reserve from user_limits if available
    reserved_jobs: List[str] = []
    failed_reserve = False
    for _ in urls:
        if ul and hasattr(ul, "check_and_reserve"):
            try:
                ok = ul.check_and_reserve(user_id or "guest", count=1)
                if not ok:
                    failed_reserve = True
                    break
                reserved_jobs.append("reserved")
            except Exception:
                failed_reserve = True
                break

    if failed_reserve:
        # release any reservations already made
        try:
            if ul and hasattr(ul, "release_reserved_slots"):
                for _ in reserved_jobs:
                    ul.release_reserved_slots(user_id or "guest", count=1)
        except Exception:
            pass
        return jsonify({"error": "user download limit exceeded"}), 403

    created_job_ids: List[str] = []
    for u in urls:
        job_id = _make_job_id()
        meta = {"mode": mode, "quality": quality, "audio_only": audio_only, "audio_format": audio_format, "enhancement": enhancement}

        # create job record within app module if available (use make_job_record for canonical structure)
        if main_app:
            ok = _ensure_job_record_in_main_app(main_app, job_id, user_id, u, meta)
            if not ok:
                LOG.warning("Job %s could not be recorded in main_app; it may not be visible to workers", job_id)
        else:
            LOG.warning("main app not available; created local job id only for %s", job_id)

        created_job_ids.append(job_id)

        # record queue entry (best-effort)
        _record_queue_entry(job_id, u, user_id)

        # finally enqueue the job id (app.JOB_QUEUE)
        if main_app and hasattr(main_app, "JOB_QUEUE"):
            try:
                main_app.JOB_QUEUE.put(job_id)
            except Exception:
                LOG.exception("Failed to enqueue job %s", job_id)
        else:
            LOG.warning("JOB_QUEUE not available on main_app; job %s won't be processed", job_id)

    return jsonify({"job_id": created_job_ids if len(created_job_ids) > 1 else created_job_ids[0]})

@download_bp.route("/job_status", methods=["GET"])
@cross_origin()
def job_status():
    """
    GET /api/job_status?job_id=...
    Returns job record (from app.JOBS) and merges manager progress when available.
    """
    job_id = request.args.get("job_id") or (request.get_json(silent=True) or {}).get("job_id")
    if not job_id:
        return jsonify({"error": "job_id required"}), 400

    main_app = _get_main_app()
    job = None
    if main_app and hasattr(main_app, "JOBS"):
        try:
            lock = getattr(main_app, "JOBS_LOCK", None)
            if lock:
                with lock:
                    job = main_app.JOBS.get(job_id)
            else:
                job = main_app.JOBS.get(job_id)
        except Exception:
            LOG.exception("failed reading JOBS")
            job = getattr(main_app, "JOBS", {}).get(job_id) if main_app else None

    resp: Dict[str, Any] = {"job": job}
    # attach manager progress if available
    try:
        dm = _dm_get_default() if _dm_get_default else None
        if dm:
            p = dm.get_progress(job_id) or {}
            resp["manager_progress"] = p
    except Exception:
        resp["manager_progress"] = {}

    if job is None and not resp.get("manager_progress"):
        return jsonify({"error": "job not found"}), 404
    return jsonify(resp)

@download_bp.route("/cleanup", methods=["POST"])
@cross_origin()
def cleanup():
    data = request.get_json(silent=True) or {}
    max_age_hours = int(data.get("max_age_hours", 3))
    keep_latest = int(data.get("keep_latest", 5))

    ok_any = False
    # try utils cleanup if present
    if _utils:
        try:
            _utils.cleanup_old_files(folder=getattr(_get_main_app(), "DOWNLOAD_DIR", None) or _utils.DOWNLOAD_DIR, hours=max_age_hours, keep_latest=keep_latest)
            _utils.cleanup_expired_history(max_age_hours=max_age_hours, keep_latest=keep_latest)
            ok_any = True
        except Exception:
            LOG.exception("utils cleanup failed")

    # try manager-level cleanup if available
    try:
        dm = _dm_get_default() if _dm_get_default else None
        if dm and hasattr(dm, "_cleanup_old_files"):
            try:
                dm._cleanup_old_files(max_age_hours=max_age_hours, keep_latest=keep_latest)
                ok_any = True
            except Exception:
                LOG.exception("manager cleanup failed")
    except Exception:
        pass

    if not ok_any:
        return jsonify({"error": "no cleanup method available"}), 500
    return jsonify({"ok": True})

@download_bp.route("/shutdown", methods=["POST"])
@cross_origin()
def shutdown():
    data = request.get_json(silent=True) or {}
    secret = data.get("secret", "")
    if SHUTDOWN_SECRET and secret != SHUTDOWN_SECRET:
        return jsonify({"error": "unauthorized"}), 403

    wait = float(data.get("wait_seconds", 2.0))

    # Try manager shutdown
    try:
        dm = _dm_get_default() if _dm_get_default else None
        if dm and hasattr(dm, "shutdown"):
            dm.shutdown(wait)
            return jsonify({"ok": True})
    except Exception:
        LOG.exception("manager shutdown failed")

    # As fallback, attempt to stop scheduler thread on main_app (best-effort)
    try:
        main_app = _get_main_app()
        if main_app and hasattr(main_app, "_stop_event"):
            try:
                main_app._stop_event.set()
                return jsonify({"ok": True, "note": "signalled main_app._stop_event"})
            except Exception:
                pass
    except Exception:
        pass

    return jsonify({"error": "shutdown not available"}), 500

# ----------------------
# atexit
# ----------------------
def _atexit_shutdown():
    try:
        dm = _dm_get_default() if _dm_get_default else None
        if dm and hasattr(dm, "shutdown"):
            LOG.info("Shutting down download manager via atexit")
            try:
                dm.shutdown(2.0)
            except Exception:
                LOG.exception("dm.shutdown failed on atexit")
    except Exception:
        LOG.exception("atexit shutdown failed")

atexit.register(_atexit_shutdown)

# ----------------------
# small helper used above
# ----------------------
def datetime_now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
