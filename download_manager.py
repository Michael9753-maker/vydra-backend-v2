# download_manager.py
"""
Download manager for VYDRA (Hard Premium Enforcement).

Features:
- Robust wrapper around yt-dlp + ffmpeg.
- Progress store (thread-safe) for job polling.
- Metadata caching and short probes.
- Basic audio extraction for free users.
- Premium gating (strict validation via premium_manager.is_premium_active).
- Best-effort integrations: cache_manager, media_processor, ai_manager, models/database.
- Backwards-compatible convenience functions.
"""

from __future__ import annotations

import os
import uuid
import shutil
import subprocess
import threading
import json
import time
import re
from pathlib import Path
from typing import Optional, Callable, Dict, Any

# --- Logging (attempt import; fallback to no-op)
try:
    from logger_manager import log_info, log_warning, log_error, log_event
except Exception:
    def log_info(*args, **kwargs): pass
    def log_warning(*args, **kwargs): pass
    def log_error(*args, **kwargs): pass
    def log_event(*args, **kwargs): pass

# --- Optional integrations (best-effort imports) ---
try:
    from models import DownloadRecord, insert_download_record, ensure_user, mark_ai_usage
except Exception:
    DownloadRecord = None
    insert_download_record = None
    ensure_user = None
    mark_ai_usage = None

try:
    from database import get_default_db
except Exception:
    get_default_db = None

# premium_manager is required for strict (hard) premium enforcement; if missing, no-one is premium.
try:
    from premium_manager import is_premium_active
except Exception:
    is_premium_active = None

# Cache integration (best-effort wrappers)
try:
    from cache_manager import get_cached_video, save_to_cache, mark_hot_link
except Exception:
    try:
        import cache_manager as _cm

        def get_cached_video(url: str, user_tier: Optional[str] = None):
            try:
                return _cm.cache_manager.get(url)
            except Exception:
                return None

        def save_to_cache(url: str, file_path: Optional[str] = None, title: Optional[str] = None,
                          thumbnail: Optional[str] = None, user_tier: str = "guest"):
            try:
                payload = {"file_path": file_path, "title": title, "thumbnail": thumbnail}
                _cm.cache_manager.set(url, payload, user_type=user_tier)
            except Exception:
                try:
                    _cm.cache_manager.set(url, file_path, user_type=user_tier)
                except Exception:
                    pass

        def mark_hot_link(url: str):
            try:
                if hasattr(_cm.cache_manager, "mark_if_hot"):
                    _cm.cache_manager.mark_if_hot(url)
                else:
                    with getattr(_cm.cache_manager, "lock", threading.Lock()):
                        item = _cm.cache_manager.cache.get(url)
                        if item and hasattr(item, "mark_hot"):
                            item.mark_hot()
            except Exception:
                pass
    except Exception:
        def get_cached_video(url: str, user_tier: Optional[str] = None):
            return None
        def save_to_cache(*args, **kwargs):
            return None
        def mark_hot_link(*args, **kwargs):
            return None

# media_processor (premium media tools) and ai_manager (optional AI postprocessing)
try:
    import media_processor as mp
except Exception as e:
    mp = None
    log_warning(f"[WARN] media_processor not loaded: {e}")

try:
    import ai_manager
except Exception as e:
    ai_manager = None
    log_warning(f"[WARN] ai_manager not loaded: {e}")

# ---------- Configuration ----------
DOWNLOAD_DIR = os.getenv("DOWNLOADS_DIR", "downloads")
Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

_MAX_ATTEMPTS = int(os.getenv("DM_MAX_ATTEMPTS", "3"))
_BACKOFF_BASE = float(os.getenv("DM_BACKOFF_BASE", "1.0"))
_POLL_SLEEP = float(os.getenv("DM_POLL_SLEEP", "0.05"))
_DURATION_PROBE_TIMEOUT = int(os.getenv("DM_DURATION_PROBE_TIMEOUT", "5"))

# ---------- Helpers to find executables ----------
def _find_executable(name: str) -> Optional[str]:
    exe = shutil.which(name)
    if exe:
        return exe
    if os.name == "nt":
        exe2 = shutil.which(f"{name}.exe")
        if exe2:
            return exe2
    return None

def _find_yt_dlp() -> Optional[str]:
    return _find_executable("yt-dlp")

def _find_ffmpeg() -> Optional[str]:
    return _find_executable("ffmpeg")

# ---------- Small utility functions ----------
def _safe_basename(path_or_url: str) -> str:
    return str(path_or_url).split("/")[-1].split("\\")[-1]

def _slugify(text: str, maxlen: int = 60) -> str:
    if not text:
        return "file"
    s = str(text)
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^A-Za-z0-9_\-]", "", s)
    s = s.strip("_-")
    return s[:maxlen] or "file"

def _unique_filename(dest_dir: str, base_name: str) -> str:
    path = os.path.join(dest_dir, base_name)
    if not os.path.exists(path):
        return os.path.abspath(path)
    name, ext = os.path.splitext(base_name)
    for i in range(1, 1000):
        candidate = f"{name}_{i}{ext}"
        candidate_path = os.path.join(dest_dir, candidate)
        if not os.path.exists(candidate_path):
            return os.path.abspath(candidate_path)
    return os.path.abspath(os.path.join(dest_dir, f"{name}_{uuid.uuid4().hex[:6]}{ext}"))

# ---------- Progress store (thread-safe) ----------
PROGRESS: Dict[str, Dict[str, Any]] = {}
_PROG_LOCK = threading.Lock()

def _update_progress(job_id: str, **kwargs):
    with _PROG_LOCK:
        entry = PROGRESS.setdefault(job_id, {
            "status": "queued",
            "percent": None,
            "downloaded_bytes": None,
            "total_bytes": None,
            "filename": None,
            "title": None,
            "thumbnail": None,
            "error": None,
            "updated_at": time.time(),
            "attempt": 0,
            "enhancement": None,
            "ai": None,
        })
        entry.update(kwargs)
        entry["updated_at"] = time.time()

# ---------- Metadata cache ----------
_META_CACHE: Dict[str, Dict[str, Any]] = {}
_META_LOCK = threading.Lock()

def _cache_metadata(url: str, meta: Dict[str, Any]):
    with _META_LOCK:
        _META_CACHE[url] = {"meta": meta, "fetched_at": time.time()}

def _get_cached_metadata(url: str) -> Optional[Dict[str, Any]]:
    with _META_LOCK:
        entry = _META_CACHE.get(url)
        if not entry:
            return None
        if time.time() - entry["fetched_at"] > 3600:
            del _META_CACHE[url]
            return None
        return entry["meta"]

# ---------- Metadata helpers ----------
def probe_duration_seconds(url: str, timeout: int = _DURATION_PROBE_TIMEOUT) -> Optional[int]:
    yt = _find_yt_dlp()
    if not yt:
        return None
    try:
        proc = subprocess.run([yt, "-J", url], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        if proc.returncode != 0 or not proc.stdout:
            return None
        line = proc.stdout.splitlines()[0]
        data = json.loads(line)
        duration = data.get("duration")
        if isinstance(duration, (int, float)):
            return int(duration)
    except Exception:
        return None
    return None

def fetch_metadata(url: str, timeout: int = 10) -> Optional[Dict[str, Any]]:
    cached = _get_cached_metadata(url)
    if cached:
        return cached
    yt = _find_yt_dlp()
    if not yt:
        return None
    try:
        proc = subprocess.run([yt, "-j", url], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        if proc.returncode != 0 or not proc.stdout:
            return None
        first = proc.stdout.splitlines()[0]
        data = json.loads(first)
        _cache_metadata(url, data)
        return data
    except Exception:
        return None

# ---------- Basic audio extraction (free) ----------
def extract_audio_basic(video_path: str, output_dir: Optional[str] = None, fmt: str = "mp3") -> str:
    ffmpeg = _find_ffmpeg()
    if ffmpeg is None:
        raise RuntimeError("ffmpeg not found on PATH")
    p = Path(video_path)
    out_dir = Path(output_dir or p.parent)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = f"{p.stem}_audio.{fmt.lstrip('.')}"
    out_path = str(out_dir.joinpath(out_name))

    # Try stream copy first
    cmd = [ffmpeg, "-y", "-i", str(video_path), "-vn", "-acodec", "copy", out_path]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        if proc.returncode == 0 and os.path.exists(out_path):
            return os.path.abspath(out_path)
    except Exception:
        pass

    # Fallback: re-encode to mp3
    cmd = [ffmpeg, "-y", "-i", str(video_path), "-vn", "-ar", "44100", "-ac", "2", "-b:a", "192k", out_path]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"basic audio extraction failed: {proc.stderr[:1000] if proc.stderr else 'unknown error'}")
    return os.path.abspath(out_path)

# ---------- Media & AI helpers (best-effort) ----------
def _run_media_enhancements(job_id: str, filepath: str, enhancement: Optional[str], target_quality: Optional[str] = None) -> str:
    if not enhancement:
        return filepath
    if mp is None:
        _update_progress(job_id, enhancement="unavailable")
        return filepath

    try:
        enh = (enhancement or "").lower()

        # Smart audio enhancement (premium)
        if enh in ("smart", "smart_audio", "smart+ai"):
            if hasattr(mp, "extract_audio_premium"):
                try:
                    new_path = mp.extract_audio_premium(filepath, enhance=True)
                    if new_path and os.path.exists(new_path):
                        _update_progress(job_id, enhancement="smart")
                        return os.path.abspath(new_path)
                except Exception:
                    _update_progress(job_id, enhancement="smart_failed")
            _update_progress(job_id, enhancement="smart_unavailable")
            return filepath

        # Upscale
        if enh.startswith("upscale_"):
            try:
                _, target = enhancement.split("_", 1)
                mapping = {"1080": "1080p", "1080p": "1080p", "2k": "2k", "2000": "2k", "4k": "4k", "4000": "4k"}
                target_norm = mapping.get(str(target).lower(), None)
                try:
                    target_h = int(str(target).replace("k", "000").replace("K", "000"))
                except Exception:
                    target_h = None

                if hasattr(mp, "upscale_video"):
                    try:
                        if target_norm:
                            try:
                                new_path = mp.upscale_video(filepath, target=target_norm)
                            except TypeError:
                                try:
                                    new_path = mp.upscale_video(filepath, target=target_norm, output_path=None)
                                except TypeError:
                                    new_path = mp.upscale_video(filepath, target_norm)
                        elif target_h:
                            try:
                                new_path = mp.upscale_video(filepath, target_height=target_h)
                            except TypeError:
                                try:
                                    new_path = mp.upscale_video(filepath, target=str(target_h))
                                except Exception:
                                    new_path = None
                        else:
                            new_path = None

                        if new_path and os.path.exists(new_path):
                            _update_progress(job_id, enhancement=f"upscaled_{target_norm or target_h}")
                            return os.path.abspath(new_path)
                    except Exception:
                        _update_progress(job_id, enhancement="upscale_failed")
            except Exception:
                _update_progress(job_id, enhancement="upscale_failed")
            return filepath

        # Generate thumbnail
        if enh in ("thumbnail", "gen_thumbnail") or ("thumbnail" in enh):
            if hasattr(mp, "generate_thumbnail"):
                try:
                    try:
                        thumb = mp.generate_thumbnail(filepath, time_seconds=2)
                    except TypeError:
                        try:
                            thumb = mp.generate_thumbnail(filepath, time_position=2)
                        except TypeError:
                            thumb = mp.generate_thumbnail(filepath, 2)
                    if thumb:
                        _update_progress(job_id, thumbnail=os.path.basename(thumb))
                except Exception:
                    pass
            return filepath

    except Exception:
        _update_progress(job_id, enhancement="failed")
        return filepath

    return filepath

def _run_ai_postprocessing(job_id: str, filepath: str, meta: Optional[Dict[str, Any]] = None, enhancement: Optional[str] = None) -> Dict[str, Any]:
    ai_results: Dict[str, Any] = {}
    if ai_manager is None:
        return ai_results
    hint = (enhancement or "").lower()
    if ("ai" not in hint) and ("meta" not in hint) and ("tags" not in hint) and ("title" not in hint):
        return ai_results

    seed_parts = []
    if isinstance(meta, dict):
        if meta.get("title"):
            seed_parts.append(str(meta.get("title")))
        if meta.get("description"):
            seed_parts.append(str(meta.get("description")))
        if meta.get("alt_description"):
            seed_parts.append(str(meta.get("alt_description")))
    seed_parts.append(os.path.basename(filepath))
    seed_text = "\n\n".join([p for p in seed_parts if p])

    try:
        try:
            caption = ai_manager.generate_caption(seed_text)
            ai_results["caption"] = caption
            _update_progress(job_id, ai={"caption": True})
        except Exception:
            caption = None

        try:
            titles_raw = ai_manager.generate_title(seed_text)
            ai_results["title_suggestions_raw"] = titles_raw
            _update_progress(job_id, ai={"title": True})
        except Exception:
            titles_raw = None

        try:
            tags = ai_manager.generate_hashtags(seed_text, max_hashtags=10)
            ai_results["hashtags"] = tags
            _update_progress(job_id, ai={"hashtags": True})
        except Exception:
            tags = None

        try:
            meta_struct = ai_manager.generate_metadata(seed_text)
            ai_results["metadata"] = meta_struct
            _update_progress(job_id, ai={"metadata": True})
        except Exception:
            meta_struct = None

    except Exception:
        _update_progress(job_id, ai="failed")
        return ai_results

    # write sidecar file (best-effort)
    try:
        sidecar = filepath + ".meta.json"
        sidecar_payload = {
            "generated_at": time.time(),
            "source_meta": meta or {},
            "ai": ai_results
        }
        with open(sidecar, "w", encoding="utf-8") as fh:
            json.dump(sidecar_payload, fh, ensure_ascii=False, indent=2)
        _update_progress(job_id, ai={"sidecar_saved": os.path.basename(sidecar)})
    except Exception:
        pass

    return ai_results

# ---------- Strict premium helper (HARD enforcement) ----------
def _is_strict_premium(user_id: Optional[str], explicit_tier: Optional[str] = None) -> bool:
    """
    Hard enforcement:
    - Returns True only if premium_manager.is_premium_active(user_id) returns True.
    - If premium_manager is not available, no-one is premium.
    """
    if is_premium_active is None:
        return False
    if not user_id:
        return False
    try:
        return bool(is_premium_active(user_id))
    except Exception:
        return False

# ---------- Compatibility wrapper: legacy process_download ----------
def process_download(user_id: str, video_url: str):
    log_info(f"[DownloadManager] Started processing download for user {user_id} - {video_url}")

    try:
        cached = get_cached_video(video_url)
        if cached:
            log_info(f"[DownloadManager] Cache hit for user {user_id} - {video_url}")
            return cached

        # Try simple hook first (downloader.download_video_file)
        try:
            from downloader import download_video_file
            result = download_video_file(video_url)
        except Exception:
            # fallback: use default manager
            mgr = get_default_manager()
            result = mgr.download(video_url)

        log_info(f"[DownloadManager] Download successful for user {user_id} - {video_url}")

        try:
            save_to_cache(video_url, result)
            log_info(f"[DownloadManager] Saved video to cache for user {user_id} - {video_url}")
        except Exception:
            pass

        if insert_download_record:
            try:
                insert_download_record(user_id, video_url)
                log_info(f"[DownloadManager] Recorded download in DB for user {user_id} - {video_url}")
            except Exception:
                pass

        return result

    except Exception as e:
        log_error(f"[DownloadManager] Error downloading video for user {user_id} - {video_url}: {str(e)}")
        raise

# ---------- DownloadManager class ----------
class DownloadManager:
    def __init__(self, download_dir: str = DOWNLOAD_DIR):
        self.download_dir = download_dir
        Path(self.download_dir).mkdir(parents=True, exist_ok=True)
        self._jobs_lock = threading.Lock()
        self._running: Dict[str, subprocess.Popen] = {}

    def _build_cmd(self,
                   url: str,
                   out_template: str,
                   quality: str = "best",
                   audio_only: bool = False,
                   audio_format: Optional[str] = None,
                   audio_quality_tier: Optional[str] = None,
                   mode: Optional[str] = None) -> list:
        yt = _find_yt_dlp()
        if not yt:
            raise RuntimeError("yt-dlp not found. Install it and ensure it's on PATH.")

        cmd = [yt, "--newline", "--no-warnings", "--no-check-certificate", "-o", out_template, "--no-playlist"]
        cmd += ["--retries", "2", "--fragment-retries", "2", "--socket-timeout", "10"]

        if mode == "speed":
            cmd += ["--concurrent-fragments", "5", "--downloader", "ffmpeg"]
        elif mode == "balanced":
            cmd += ["--concurrent-fragments", "3"]

        if audio_only:
            audio_fmt = (str(audio_format).lower() if audio_format else "m4a")
            cmd += ["-x", "--audio-format", audio_fmt]
            if audio_fmt in ("m4a", "aac"):
                if audio_quality_tier == "q2":
                    cmd += ["--postprocessor-args", "-c:a aac -b:a 256k"]
                elif audio_quality_tier == "q1":
                    cmd += ["--postprocessor-args", "-c:a aac -b:a 128k"]
            elif audio_fmt == "mp3":
                if audio_quality_tier == "q2":
                    cmd += ["--postprocessor-args", "-b:a 192k"]
                elif audio_quality_tier == "q1":
                    cmd += ["--postprocessor-args", "-b:a 128k"]

        q = str(quality or "best")
        if q.lower() not in ("best", "auto"):
            try:
                qs = q.lower().replace("p", "")
                if qs.endswith("k"):
                    num = int(qs.replace("k", "")) * 1000
                else:
                    num = int(qs)
                fmt = f"bestvideo[height<={num}]+bestaudio/best"
                cmd += ["-f", fmt]
            except Exception:
                cmd += ["-f", q]
        else:
            cmd += ["-f", "best"]

        cmd.append(url)
        return cmd

    def download(self,
                 url: str,
                 quality: str = "best",
                 audio_only: bool = False,
                 job_id: Optional[str] = None,
                 stop_event: Optional[threading.Event] = None,
                 timeout: Optional[int] = None,
                 progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
                 enforce_max_duration_seconds: Optional[int] = None,
                 audio_format: Optional[str] = None,
                 audio_quality_tier: Optional[str] = None,
                 mode: Optional[str] = None,
                 enhancement: Optional[str] = None,
                 **kwargs) -> str:
        """
        Blocking download call. Returns absolute path to downloaded file.

        Hard premium enforcement:
          - If a requested feature requires premium and strict validation fails, raises RuntimeError.
        """
        if not url:
            raise ValueError("url is required")

        if job_id is None:
            job_id = uuid.uuid4().hex

        _update_progress(job_id, status="queued", percent=0, filename=None, title=None, thumbnail=None, error=None, attempt=0)
        _update_progress(job_id, enhancement=enhancement)

        # Determine user/premium hints
        user_tier_hint = kwargs.get("user_tier") or kwargs.get("tier") or kwargs.get("user_type") or None
        user_id = kwargs.get("user_id") or kwargs.get("user_email") or kwargs.get("email") or None
        is_premium = _is_strict_premium(user_id, user_tier_hint)

        enh = (enhancement or "").lower()

        # PREMIUM GATING (HARD)
        premium_required = False
        if ("ai" in enh) or ("meta" in enh) or ("tags" in enh) or ("title" in enh):
            premium_required = True
        if enh.startswith("smart"):
            premium_required = True
        if enh.startswith("upscale"):
            premium_required = True

        # quality >1080 enforced premium
        try:
            qcheck = str(quality or "").lower().replace("p", "")
            if qcheck.endswith("k"):
                qval = int(qcheck.replace("k", "")) * 1000
            else:
                qval = int(qcheck) if qcheck else None
            if qval and qval > 1080:
                premium_required = True
        except Exception:
            pass

        if premium_required and not is_premium:
            _update_progress(job_id, status="error", error="Premium feature required")
            raise RuntimeError("This feature requires an active premium plan (strict validation)")

        # ---------------------
        # CHECK CACHE (FAST PATH)
        # ---------------------
        try:
            cached_entry = None
            try:
                cached_entry = get_cached_video(url, user_tier=("premium" if is_premium else "guest"))
            except TypeError:
                cached_entry = get_cached_video(url)
            if cached_entry:
                if isinstance(cached_entry, dict):
                    cached_path = cached_entry.get("file_path") or cached_entry.get("path") or cached_entry.get("file")
                    cached_title = cached_entry.get("title")
                    cached_thumb = cached_entry.get("thumbnail")
                else:
                    cached_path = str(cached_entry)
                    cached_title = None
                    cached_thumb = None

                if cached_path and os.path.exists(cached_path):
                    basename = os.path.basename(cached_path)
                    _update_progress(job_id, status="finished", percent=100, filename=basename, title=cached_title, thumbnail=cached_thumb)
                    return os.path.abspath(cached_path)
        except Exception:
            pass

        # quick duration probe
        if enforce_max_duration_seconds is not None:
            dur = probe_duration_seconds(url, timeout=_DURATION_PROBE_TIMEOUT)
            if dur is not None and dur > enforce_max_duration_seconds:
                _update_progress(job_id, status="error", error=f"duration {dur}s exceeds limit", percent=0)
                raise RuntimeError(f"Video duration {dur}s exceeds limit of {enforce_max_duration_seconds}s")

        uid = uuid.uuid4().hex[:8]
        temp_template = os.path.join(self.download_dir, f"vydra_{uid}.%(ext)s")

        try:
            from datetime import datetime, timezone
            created_at_iso = datetime.now(timezone.utc).isoformat()
            started_at_iso = created_at_iso
        except Exception:
            created_at_iso = None
            started_at_iso = None

        attempted = 0
        last_err: Optional[str] = None
        while attempted < _MAX_ATTEMPTS:
            attempted += 1
            _update_progress(job_id, status="downloading", percent=0, attempt=attempted, error=None)
            start_time = time.time()

            meta_result: Dict[str, Any] = {}
            meta_ready = threading.Event()

            def _meta_worker():
                try:
                    meta = fetch_metadata(url, timeout=8)
                    if meta:
                        meta_result.update(meta)
                        _cache_metadata(url, meta)
                        _update_progress(job_id, title=meta.get("title"), thumbnail=meta.get("thumbnail"))
                finally:
                    meta_ready.set()

            threading.Thread(target=_meta_worker, daemon=True).start()

            try:
                cmd = self._build_cmd(url, out_template=temp_template, quality=quality,
                                      audio_only=audio_only, audio_format=audio_format,
                                      audio_quality_tier=audio_quality_tier, mode=mode)
            except Exception as e:
                _update_progress(job_id, status="error", error=str(e))
                raise

            proc = None
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            except Exception as e:
                last_err = str(e)
                _update_progress(job_id, status="error", error=last_err)
                time.sleep(_BACKOFF_BASE * attempted)
                continue

            with self._jobs_lock:
                self._running[job_id] = proc

            percent = None
            filename_guess = None
            downloaded_bytes = None

            progress_re = re.compile(r"(\d{1,3}(?:\.\d+)?)%")
            dest_re = re.compile(r"Destination:\s*(.*)$")
            bytes_re = re.compile(r"of\s+([\d\.]+)\s*(B|KiB|MiB|GiB)")

            try:
                while True:
                    if stop_event and stop_event.is_set():
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        _update_progress(job_id, status="cancelled", error="cancelled by user")
                        raise RuntimeError("Download cancelled")

                    if timeout is not None and (time.time() - start_time) > timeout:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        _update_progress(job_id, status="error", error="timeout")
                        raise RuntimeError("Download timeout")

                    line = ""
                    if proc.stdout is not None:
                        try:
                            line = proc.stdout.readline()
                        except Exception:
                            line = ""
                    if line is None:
                        line = ""
                    if line == "" and proc.poll() is not None:
                        break
                    if not line:
                        time.sleep(_POLL_SLEEP)
                        continue

                    line = line.strip()
                    m = progress_re.search(line)
                    if m:
                        try:
                            percent = float(m.group(1))
                        except Exception:
                            percent = None

                    dm = dest_re.search(line)
                    if dm:
                        filename_guess = dm.group(1).strip()

                    bm = bytes_re.search(line)
                    if bm:
                        try:
                            val = float(bm.group(1))
                            unit = bm.group(2)
                            factor = 1
                            if unit == "KiB":
                                factor = 1024
                            elif unit == "MiB":
                                factor = 1024**2
                            elif unit == "GiB":
                                factor = 1024**3
                            downloaded_bytes = int(val * factor)
                        except Exception:
                            downloaded_bytes = None

                    p = {}
                    if percent is not None:
                        p["percent"] = int(round(percent))
                    if filename_guess:
                        p["filename"] = filename_guess
                    if downloaded_bytes is not None:
                        p["downloaded_bytes"] = downloaded_bytes
                    if p:
                        _update_progress(job_id, status="downloading", **p)
                        if progress_callback:
                            try:
                                progress_callback(PROGRESS[job_id])
                            except Exception:
                                pass

                rc = proc.poll()
                if rc != 0:
                    out = ""
                    try:
                        out = proc.stdout.read() if proc.stdout is not None else ""
                    except Exception:
                        out = ""
                    last_err = f"yt-dlp rc={rc} {out[:1000]}"
                    _update_progress(job_id, status="error", error=last_err)
                    raise RuntimeError(last_err)

                downloaded_path = None
                for fname in os.listdir(self.download_dir):
                    if f"vydra_{uid}." in fname or fname.startswith(f"vydra_{uid}"):
                        downloaded_path = os.path.abspath(os.path.join(self.download_dir, fname))
                        break
                if not downloaded_path:
                    for fname in os.listdir(self.download_dir):
                        if fname.startswith(uid):
                            downloaded_path = os.path.abspath(os.path.join(self.download_dir, fname))
                            break

                if not downloaded_path:
                    _update_progress(job_id, status="error", error="download finished but file not found")
                    raise RuntimeError("Download finished but output file not found")

                meta_ready.wait(timeout=3)
                meta = meta_result if meta_result else fetch_metadata(url, timeout=2)
                title = (meta.get("title") if isinstance(meta, dict) else None) if meta else None
                thumbnail = None
                if meta:
                    thumbnail = meta.get("thumbnail") or (meta.get("thumbnails")[-1].get("url") if isinstance(meta.get("thumbnails"), list) and meta.get("thumbnails") else None)

                slug = _slugify(title or "")[:48] or "vydra"
                ext = os.path.splitext(downloaded_path)[1]
                final_base = f"vydra_{slug}_{uid}{ext}"
                final_full = _unique_filename(self.download_dir, final_base)
                try:
                    os.replace(downloaded_path, final_full)
                except Exception:
                    try:
                        shutil.copy2(downloaded_path, final_full)
                        os.remove(downloaded_path)
                    except Exception:
                        final_full = downloaded_path

                # post-processing: enhancements (premium enforced above)
                try:
                    final_full = _run_media_enhancements(job_id, final_full, enhancement, target_quality=quality)
                except Exception:
                    _update_progress(job_id, enhancement="failed")

                ai_results = {}
                try:
                    if _is_strict_premium(user_id, user_tier_hint) and ai_manager is not None:
                        ai_results = _run_ai_postprocessing(job_id, final_full, meta=meta, enhancement=enhancement)
                        if ai_results:
                            _update_progress(job_id, ai=ai_results)
                except Exception:
                    _update_progress(job_id, ai="failed")

                basename = os.path.basename(final_full)
                _update_progress(job_id, status="finished", percent=100, filename=basename, title=title, thumbnail=thumbnail)
                if progress_callback:
                    try:
                        progress_callback(PROGRESS[job_id])
                    except Exception:
                        pass

                # cache/store/DB (best-effort)
                try:
                    try:
                        save_to_cache(url=url, file_path=os.path.abspath(final_full), title=title, thumbnail=thumbnail, user_tier=("premium" if is_premium else "guest"))
                    except TypeError:
                        try:
                            save_to_cache(url, os.path.abspath(final_full))
                        except Exception:
                            try:
                                save_to_cache({"url": url, "file_path": os.path.abspath(final_full), "title": title, "thumbnail": thumbnail, "user_tier": ("premium" if is_premium else "guest")})
                            except Exception:
                                pass
                except Exception:
                    pass

                try:
                    if kwargs.get("mark_hot") or enhancement == "hot":
                        try:
                            mark_hot_link(url)
                        except Exception:
                            pass
                except Exception:
                    pass

                try:
                    if insert_download_record and DownloadRecord and get_default_db:
                        db = get_default_db()
                        try:
                            db.connect()
                        except Exception:
                            pass

                        user_id_val = kwargs.get("user_id") or None
                        if not user_id_val:
                            user_email = kwargs.get("user_email") or kwargs.get("email") or None
                            if user_email and ensure_user:
                                try:
                                    u = ensure_user(db, user_email, None, True if is_premium else False)
                                    user_id_val = getattr(u, "id", None)
                                except Exception:
                                    user_id_val = None

                        try:
                            size_bytes = os.path.getsize(final_full)
                        except Exception:
                            size_bytes = None

                        caption_val = None
                        hashtags_val = None
                        try:
                            if isinstance(ai_results, dict):
                                caption_val = ai_results.get("caption")
                                hashtags_val = ai_results.get("hashtags")
                                if isinstance(hashtags_val, str):
                                    try:
                                        hashtags_val = json.loads(hashtags_val)
                                    except Exception:
                                        hashtags_val = [hashtags_val]
                        except Exception:
                            pass

                        rec = DownloadRecord(
                            job_id=job_id,
                            user_id=user_id_val,
                            url=url,
                            title=title,
                            filename=basename,
                            file_path=os.path.abspath(final_full),
                            thumbnail=thumbnail,
                            caption=caption_val,
                            hashtags=hashtags_val,
                            size_bytes=size_bytes or 0,
                            status="finished",
                            enhancement_used=bool(enhancement),
                            ai_used=1 if ai_results else 0,
                            created_at=created_at_iso,
                            started_at=started_at_iso,
                            finished_at=(lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())()
                        )
                        try:
                            insert_download_record(db, rec)
                        except Exception:
                            pass

                        try:
                            if mark_ai_usage and isinstance(ai_results, dict) and user_id_val:
                                mark_ai_usage(db, user_id_val, count=1)
                        except Exception:
                            pass
                except Exception:
                    pass

                return os.path.abspath(final_full)

            except Exception as e:
                try:
                    if proc and proc.poll() is None:
                        proc.kill()
                except Exception:
                    pass
                with self._jobs_lock:
                    if job_id in self._running:
                        del self._running[job_id]
                if attempted >= _MAX_ATTEMPTS:
                    err_msg = str(e) if not last_err else last_err
                    _update_progress(job_id, status="error", error=err_msg)
                    raise RuntimeError(err_msg)
                time.sleep(_BACKOFF_BASE * attempted)
                continue
            finally:
                with self._jobs_lock:
                    if job_id in self._running and (self._running[job_id] is proc):
                        del self._running[job_id]

        final_err = last_err or "unknown download failure"
        _update_progress(job_id, status="error", error=final_err)
        raise RuntimeError(final_err)

    def cancel(self, job_id: str) -> bool:
        with self._jobs_lock:
            proc = self._running.get(job_id)
        if not proc:
            return False
        try:
            proc.kill()
            _update_progress(job_id, status="cancelled", error="cancelled by user")
            return True
        except Exception:
            return False

    def get_progress(self, job_id: str) -> Optional[Dict[str, Any]]:
        with _PROG_LOCK:
            v = PROGRESS.get(job_id)
            return dict(v) if v is not None else None

# ---------- Backwards-compatible helpers ----------
_DEFAULT_MANAGER: Optional[DownloadManager] = None
def get_default_manager() -> DownloadManager:
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        _DEFAULT_MANAGER = DownloadManager(DOWNLOAD_DIR)
    return _DEFAULT_MANAGER

def download_video(url: str, quality: str = "best", audio_only: bool = False) -> str:
    mgr = get_default_manager()
    return mgr.download(url, quality=quality, audio_only=audio_only)

__all__ = [
    "download_video", "get_default_manager", "DownloadManager", "PROGRESS",
    "probe_duration_seconds", "fetch_metadata", "extract_audio_basic"
]
