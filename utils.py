# utils.py (UPGRADED)
"""
Utility helpers for VYDRA backend.

Features:
- ensure_folders(folders=None)
- is_valid_url(url)
- is_supported_platform(url) -> ("tiktok","youtube",...) or None
- fetch_metadata_safe(url, timeout=8)
- humanize_time_ago(iso_ts_or_seconds)
- safe_join_path(base, *parts)
- safe_delete(path)
- folder_size_mb(path)
- load_history() / save_history(list)
- save_history_entry(entry)
- cleanup_old_files(folder, hours=3, keep_latest=5)
- cleanup_expired_history(max_age_hours=3, keep_latest=5)
- start_cleanup_scheduler(interval=1800, stop_event=None)
- anti_crash(default=None) decorator
- sanitize_and_truncate_filename(text, maxlen=40)
- extract_hashtags(text)
- recent_downloads_for_user(user_id, max_age_seconds=3*3600)
"""

from __future__ import annotations
import os
import time
import json
import threading
import re
import shutil
import errno
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Callable

# try to import the metadata helper from download_manager (preferred)
try:
    from download_manager import fetch_metadata as _dm_fetch_metadata, probe_duration_seconds as _dm_probe
except Exception:
    _dm_fetch_metadata = None
    _dm_probe = None

# -------------------------
# Configuration / constants
# -------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DOWNLOAD_DIR = os.path.abspath(os.getenv("DOWNLOADS_DIR", os.path.join(BASE_DIR, "downloads")))
HISTORY_DIR = os.path.join(DOWNLOAD_DIR, "meta")
HISTORY_FILE = os.path.join(HISTORY_DIR, "history.json")
DATA_DIR = os.path.abspath(os.path.join(BASE_DIR, "data"))

# History limits
HISTORY_MAX_ITEMS = int(os.getenv("HISTORY_MAX_ITEMS", "500"))
RECENT_WINDOW_SECONDS = int(os.getenv("RECENT_WINDOW_SECONDS", str(3 * 60 * 60)))  # 3 hours default

# locks
_FOLDERS_LOCK = threading.Lock()
_HISTORY_LOCK = threading.Lock()

# ensure folders exist at import time (best-effort)
def ensure_folders(folders: Optional[List[str]] = None) -> None:
    """
    Ensure the given folders exist (creates them). If folders is None, create
    DOWNLOAD_DIR, HISTORY_DIR and DATA_DIR.
    This function is idempotent and thread-safe.
    """
    if folders is None:
        folders = [DOWNLOAD_DIR, HISTORY_DIR, DATA_DIR]
    with _FOLDERS_LOCK:
        for folder in folders:
            try:
                os.makedirs(folder, exist_ok=True)
            except Exception:
                # fallback: try to create parent directories
                try:
                    parent = os.path.dirname(folder)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    os.makedirs(folder, exist_ok=True)
                except Exception:
                    # as last resort ignore - caller should handle permission issues
                    pass

# create base folders on import
ensure_folders()

# -------------------------
# URL validation & platform detection
# -------------------------
_URL_REGEX = re.compile(
    r'^(?:http|https)://'                     # scheme
    r'(?:[\w\-]+\.)+[\w\-]+'                  # domain
    r'(?::\d{1,5})?'                          # optional port
    r'(?:/.*)?$'                              # optional path
, re.IGNORECASE)

def is_valid_url(url: str) -> bool:
    """Return True if the string looks like a valid HTTP/HTTPS URL."""
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    return bool(_URL_REGEX.match(url))

# platform heuristics - return canonical token or None
def is_supported_platform(url: str) -> Optional[str]:
    """
    Heuristically detect supported platforms by URL:
    returns one of: "youtube", "tiktok", "instagram", "facebook", "twitter", "vimeo"
    or None if unknown.
    """
    if not is_valid_url(url):
        return None
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "tiktok.com" in u or "vm.tiktok.com" in u:
        return "tiktok"
    if "instagram.com" in u or "instagr.am" in u:
        return "instagram"
    if "facebook.com" in u or "fb.watch" in u:
        return "facebook"
    if "twitter.com" in u or "x.com" in u or "t.co" in u:
        return "twitter"
    if "vimeo.com" in u:
        return "vimeo"
    return None

# -------------------------
# Safe path helpers
# -------------------------
def safe_join_path(base: str, *parts: str) -> str:
    """
    Safely join path components and ensure resulting path is inside `base`.
    Prevents directory traversal attacks.
    Returns absolute path; raises ValueError if outside base.
    """
    if not base:
        raise ValueError("base path is required")
    base = os.path.abspath(base)
    candidate = os.path.abspath(os.path.join(base, *parts))
    if not candidate.startswith(base.rstrip(os.sep) + os.sep) and candidate != base:
        raise ValueError("resulting path is outside allowed base")
    return candidate

def safe_delete(path: str) -> bool:
    """
    Safely delete a file if it exists. Returns True when file deleted or not present;
    False if deletion failed.
    """
    try:
        if not path:
            return True
        if os.path.exists(path):
            os.remove(path)
        return True
    except Exception:
        return False

# -------------------------
# Folder size (efficient)
# -------------------------
def folder_size_mb(path: str = DOWNLOAD_DIR) -> float:
    """Return folder size in megabytes (rounded 2 decimals)."""
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        # include nested small directories
                        for root, _, files in os.walk(entry.path):
                            for f in files:
                                try:
                                    total += os.path.getsize(os.path.join(root, f))
                                except Exception:
                                    pass
                except Exception:
                    pass
    except Exception:
        # fallback to os.walk full scan
        try:
            for root, _, files in os.walk(path):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except Exception:
                        pass
        except Exception:
            return 0.0
    return round(total / (1024.0 * 1024.0), 2)

# -------------------------
# History (atomic writes + locking)
# -------------------------
def load_history() -> List[Dict[str, Any]]:
    """Load history list from HISTORY_FILE (always returns a list)."""
    ensure_folders()
    try:
        with _HISTORY_LOCK:
            if not os.path.exists(HISTORY_FILE):
                return []
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception:
        pass
    return []

def save_history(data: List[Dict[str, Any]]) -> bool:
    """Atomically save history list (returns True on success)."""
    ensure_folders()
    try:
        tmp = HISTORY_FILE + ".tmp"
        with _HISTORY_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data[:HISTORY_MAX_ITEMS], f, indent=2, ensure_ascii=False, default=str)
            # atomic replace
            os.replace(tmp, HISTORY_FILE)
        return True
    except Exception:
        # cleanup tmp if present
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False

def save_history_entry(entry: Dict[str, Any]) -> bool:
    """
    Insert an entry at the top of history and persist.
    Entry should include at least: user_id, job_id, title, file, created_at (iso str)
    """
    if not isinstance(entry, dict):
        return False
    try:
        hist = load_history()
        hist.insert(0, entry)
        # cap length
        hist = hist[:HISTORY_MAX_ITEMS]
        return save_history(hist)
    except Exception:
        return False

# -------------------------
# Metadata fetch wrapper
# -------------------------
def fetch_metadata_safe(url: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
    """
    Try to fetch metadata using download_manager.fetch_metadata if present,
    otherwise attempt a lightweight yt-dlp -j probe (best-effort).
    Returns dict or None.
    """
    if not url or not is_valid_url(url):
        return None

    # prefer download_manager helper if available
    if _dm_fetch_metadata:
        try:
            meta = _dm_fetch_metadata(url, timeout=timeout)
            if isinstance(meta, dict):
                return meta
        except Exception:
            pass

    # fallback: try calling yt-dlp directly if present on PATH
    yt = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if not yt:
        return None
    try:
        proc = None
        import subprocess
        proc = subprocess.run([yt, "-j", url], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        if proc.returncode != 0 or not proc.stdout:
            return None
        first = proc.stdout.splitlines()[0]
        data = json.loads(first)
        return data
    except Exception:
        return None

def probe_duration_safe(url: str, timeout: int = 5) -> Optional[int]:
    """Probe duration (seconds) using download_manager probe if available, else best-effort."""
    if _dm_probe:
        try:
            return _dm_probe(url)
        except Exception:
            return None
    # best-effort using fetch_metadata_safe
    try:
        meta = fetch_metadata_safe(url, timeout=timeout)
        if meta and isinstance(meta.get("duration"), (int, float)):
            return int(meta.get("duration"))
    except Exception:
        pass
    return None

# -------------------------
# Human readable time ago
# -------------------------
def _parse_iso_to_dt(iso_ts: str) -> Optional[datetime]:
    """Parse various ISO formats robustly to an aware datetime in UTC."""
    if not iso_ts:
        return None
    if not isinstance(iso_ts, str):
        return None
    s = iso_ts.strip()
    # handle 'Z' suffix
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        # if naive, assume UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        # try a few common fallbacks
        try:
            # try parsing RFC style
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt
        except Exception:
            return None

def humanize_time_ago(ts: Optional[str | int | float | datetime]) -> str:
    """
    Convert a timestamp (ISO string, epoch seconds, or datetime) to human-friendly
    strings like: "now", "1 minute ago", "5 minutes ago", "2 hours ago".
    """
    try:
        now = datetime.now(timezone.utc)
        if ts is None:
            return "now"
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        elif isinstance(ts, datetime):
            dt = ts.astimezone(timezone.utc)
        else:
            dt = _parse_iso_to_dt(str(ts))
            if dt is None:
                return "now"
        diff = now - dt
        s = int(diff.total_seconds())
        if s < 60:
            return "now"
        if s < 3600:
            m = s // 60
            return f"{m} minute{'s' if m != 1 else ''} ago"
        if s < 24 * 3600:
            h = s // 3600
            return f"{h} hour{'s' if h != 1 else ''} ago"
        d = s // (24 * 3600)
        return f"{d} day{'s' if d != 1 else ''} ago"
    except Exception:
        return "now"

# -------------------------
# Cleanup / retention
# -------------------------
def cleanup_old_files(folder: str = DOWNLOAD_DIR, hours: int = 3, keep_latest: int = 5) -> None:
    """
    Remove files older than `hours` (in the given folder) and keep only the
    `keep_latest` most recent files. Non-recursive for safety.
    """
    try:
        if not os.path.isdir(folder):
            return
        now = time.time()
        files = []
        with os.scandir(folder) as it:
            for e in it:
                try:
                    if e.is_file(follow_symlinks=False):
                        files.append((e.path, e.stat(follow_symlinks=False).st_mtime))
                except Exception:
                    pass
        # sort oldest first
        files.sort(key=lambda x: x[1])
        # delete older-than threshold
        keep = []
        for path, mtime in files:
            age_hours = (now - mtime) / 3600.0
            if age_hours > hours:
                try:
                    os.remove(path)
                except Exception:
                    pass
            else:
                keep.append((path, mtime))
        # ensure keep_latest
        if len(keep) > keep_latest:
            # keep sorted newest-first for trimming
            keep.sort(key=lambda x: x[1], reverse=True)
            extras = keep[keep_latest:]
            for path, _ in extras:
                try:
                    os.remove(path)
                except Exception:
                    pass
    except Exception:
        pass

def cleanup_expired_history(max_age_hours: int = 3, keep_latest: int = 5) -> None:
    """
    Remove history entries older than max_age_hours and delete their associated files.
    Also enforce keep_latest entries per user (global fallback if user_id missing).
    """
    try:
        ensure_folders()
        hist = load_history()
        now = datetime.now(timezone.utc)
        new_hist = []
        # filter by max_age_hours
        for h in hist:
            try:
                ts = h.get("finished_at") or h.get("created_at")
                if not ts:
                    new_hist.append(h)
                    continue
                dt = _parse_iso_to_dt(ts)
                if dt is None:
                    new_hist.append(h)
                    continue
                if (now - dt).total_seconds() > max_age_hours * 3600:
                    # delete file if present
                    fp = h.get("file_path") or (os.path.join(DOWNLOAD_DIR, h.get("file")) if h.get("file") else None)
                    if fp:
                        try:
                            safe_delete(fp)
                        except Exception:
                            pass
                    # skip adding to new_hist (expired)
                    continue
                new_hist.append(h)
            except Exception:
                new_hist.append(h)

        # group by user and enforce keep_latest per user
        by_user: Dict[str, List[Dict[str, Any]]] = {}
        for h in new_hist:
            uid = h.get("user_id") or "guest"
            by_user.setdefault(uid, []).append(h)

        final = []
        for uid, items in by_user.items():
            # sort newest first
            items_sorted = sorted(items, key=lambda x: x.get("finished_at") or x.get("created_at") or "", reverse=True)
            keep = items_sorted[:keep_latest]
            final.extend(keep)
            extras = items_sorted[keep_latest:]
            for e in extras:
                fp = e.get("file_path") or (os.path.join(DOWNLOAD_DIR, e.get("file")) if e.get("file") else None)
                if fp:
                    try:
                        safe_delete(fp)
                    except Exception:
                        pass

        # persist final
        save_history(final)
    except Exception:
        pass

# -------------------------
# Background scheduler
# -------------------------
def start_cleanup_scheduler(interval: int = 1800, max_age_hours: int = 3, keep_latest: int = 5, stop_event: Optional[threading.Event] = None) -> threading.Thread:
    """
    Start a daemon thread that periodically runs cleanup_expired_history and cleanup_old_files.
    Returns the Thread object. If stop_event is provided, the loop will stop when stop_event.is_set().
    """
    def _loop():
        while True:
            if stop_event and stop_event.is_set():
                break
            try:
                cleanup_expired_history(max_age_hours=max_age_hours, keep_latest=keep_latest)
                cleanup_old_files(DOWNLOAD_DIR, hours=max_age_hours, keep_latest=keep_latest)
            except Exception:
                pass
            # wait with early exit check
            for _ in range(max(1, int(interval))):
                if stop_event and stop_event.is_set():
                    return
                time.sleep(1)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t

# -------------------------
# Anti-crash decorator
# -------------------------
def anti_crash(default: Any = None, logger: Optional[Callable[[str], None]] = None):
    """
    Decorator to wrap critical functions so they never raise to caller.
    On exception, returns `default` and (optionally) logs via `logger` callable.
    Example:
        @anti_crash(default=[])
        def load_history(...): ...
    """
    def _decorator(fn):
        def _wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                try:
                    if logger:
                        logger(f"anti_crash: {fn.__name__} failed: {e}")
                    else:
                        # best-effort log to stderr
                        print(f"anti_crash: {fn.__name__} failed: {e}")
                except Exception:
                    pass
                return default
        _wrapped.__name__ = fn.__name__
        return _wrapped
    return _decorator

# -------------------------
# Filename helpers & small utils
# -------------------------
def sanitize_and_truncate_filename(text: Optional[str], maxlen: int = 40) -> str:
    """Produce a short slug suitable for UI; fall back to 'file' when empty."""
    if not text:
        return "file"
    s = str(text)
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    # keep letters, numbers, dash and underscore
    s = re.sub(r"[^A-Za-z0-9 _\-]", "", s)
    s = s.replace(" ", "-").lower()
    if not s:
        return "file"
    if len(s) <= maxlen:
        return s
    # try to keep start and meaningful end
    head = s[:maxlen//2].rstrip("-_")
    tail = s[-(maxlen//2 - 3):].lstrip("-_")
    return (head + "..." + tail)[:maxlen]

_hashtag_re = re.compile(r"#\w+")
def extract_hashtags(text: Optional[str]) -> List[str]:
    if not text or not isinstance(text, str):
        return []
    return _hashtag_re.findall(text)

def recent_downloads_for_user(user_id: str, max_age_seconds: int = RECENT_WINDOW_SECONDS) -> List[Dict[str, Any]]:
    """
    Return recent (not expired) history entries for a user with computed 'age_seconds'
    and truncated fields suitable for UI.
    """
    now = datetime.now(timezone.utc)
    out = []
    try:
        hist = load_history()
        for e in hist:
            try:
                if e.get("user_id") != user_id:
                    continue
                ts = e.get("finished_at") or e.get("created_at")
                dt = _parse_iso_to_dt(ts) if ts else None
                if not dt:
                    continue
                age = int((now - dt).total_seconds())
                if age > max_age_seconds:
                    continue
                # prepare compact item
                item = {
                    "title": sanitize_and_truncate_filename(e.get("title") or e.get("file") or "", maxlen=48),
                    "file": e.get("file"),
                    "file_path": e.get("file_path"),
                    "thumbnail": e.get("thumbnail"),
                    "caption": (e.get("caption") or "")[:200],
                    "hashtags": e.get("hashtags") or [],
                    "created_at": e.get("created_at"),
                    "age_seconds": age
                }
                out.append(item)
            except Exception:
                continue
    except Exception:
        pass
    return out

# -------------------------
# System status (debug)
# -------------------------
def log_system_status() -> Dict[str, Any]:
    """
    Small snapshot for debugging/health checks.
    """
    try:
        hist = load_history()
        size = folder_size_mb(DOWNLOAD_DIR)
        return {
            "history_count": len(hist),
            "download_folder_size_mb": size,
            "download_dir": DOWNLOAD_DIR,
            "history_file": HISTORY_FILE
        }
    except Exception:
        return {}

# Expose public API
__all__ = [
    "ensure_folders",
    "is_valid_url",
    "is_supported_platform",
    "fetch_metadata_safe",
    "probe_duration_safe",
    "humanize_time_ago",
    "safe_join_path",
    "safe_delete",
    "folder_size_mb",
    "load_history",
    "save_history",
    "save_history_entry",
    "cleanup_old_files",
    "cleanup_expired_history",
    "start_cleanup_scheduler",
    "anti_crash",
    "sanitize_and_truncate_filename",
    "extract_hashtags",
    "recent_downloads_for_user",
    "log_system_status",
]

# If run directly, quick smoke test
if __name__ == "__main__":
    print("UTILS quick test:")
    ensure_folders()
    print("DOWNLOAD_DIR:", DOWNLOAD_DIR)
    print("HISTORY_FILE:", HISTORY_FILE)
    print("Valid URL check:", is_valid_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
    print("Platform:", is_supported_platform("https://www.tiktok.com/@user/video/123"))
    print("Humanize now:", humanize_time_ago(datetime.now(timezone.utc).isoformat()))
    print("Folder size MB:", folder_size_mb(DOWNLOAD_DIR))
    print("Load history length:", len(load_history()))
