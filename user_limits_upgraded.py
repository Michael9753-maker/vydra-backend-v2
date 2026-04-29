"""
user_limits_upgraded.py — upgraded with optional DB wiring

Behavior changes (backwards-compatible):
- Keeps the JSON-backed local store exactly as before (no breaking changes).
- Adds an optional, non-mandatory DB wiring layer:
  - call `attach_db(db_instance, models_module=None)` to enable automatic persistence.
  - or pass `db=...` and/or `models=...` to specific functions as kwargs for one-off persistence.
- Automatic DB persistence performed for:
  - `increment_ai` -> persists to `ai_usage` (via models.mark_ai_usage)
  - `commit_success` -> if `download_record` is provided (a dict or models.DownloadRecord), inserts into `downloads` via models.insert_download_record
  - `commit_success` also ensures a user row exists when `user_id` looks like an email (via models.ensure_user)

Implementation notes:
- Nothing changes for callers that only use the public API as before.
- DB wiring is best-effort only: all DB operations are wrapped in try/except so JSON store remains the source of truth when DB is not present or fails.
- The module exposes `attach_db(db, models_module)` and `detach_db()` helpers.

Use this file in place of your current user_limits_upgraded.py (drop-in replacement).
"""

from __future__ import annotations
import os
import json
import threading
from datetime import date, datetime
from typing import Dict, Any, Optional, Union

# persistent store path (override with env var in dev if needed)
STORE_PATH = os.getenv("USAGE_STORE_PATH", "usage_store.json")
_LOCK = threading.Lock()

# defaults
GUEST_DEFAULT_LIMIT = 30
FREE_DEFAULT_LIMIT = 30
PREMIUM_DEFAULT_LIMIT = 50
PREMIUM_AI_LIMIT = 10

# Demo premium users (keep in sync with app.py during dev)
DEMO_PREMIUM_USERS = set(["demo-user@example.com"])

# in-memory store structure
# {
#   "meta": {"last_reset_date": "YYYY-MM-DD"},
#   "users": {
#       user_id: {"date": "YYYY-MM-DD","success":int,"reserved":int,"ai":int}
#   }
# }
_store: Dict[str, Any] = {"meta": {"last_reset_date": date.today().isoformat()}, "users": {}}

# Optional DB wiring (best-effort)
_DB: Optional[object] = None
_MODELS: Optional[object] = None

# -----------------------
# DB wiring helpers
# -----------------------

def attach_db(db_instance: object, models_module: Optional[object] = None) -> None:
    """Attach a database instance and optional models module for persistence.

    - db_instance: instance of Database (from database.get_default_db()) or any object you want to pass through to models functions.
    - models_module: optional explicit import of your models module (if omitted the code will attempt to import 'models' lazily).

    This is optional — if not attached, module functions only use the JSON store.
    """
    global _DB, _MODELS
    _DB = db_instance
    _MODELS = models_module


def detach_db() -> None:
    """Detach any previously attached DB/models."""
    global _DB, _MODELS
    _DB = None
    _MODELS = None


def _get_models() -> Optional[object]:
    """Return the models module (either attached or lazy-imported)."""
    global _MODELS
    if _MODELS is not None:
        return _MODELS
    try:
        import models as _m  # type: ignore
        _MODELS = _m
        return _MODELS
    except Exception:
        return None


def _ensure_user_in_db_if_possible(user_id: str, db: Optional[object] = None) -> Optional[int]:
    """If `user_id` looks like an email and a models/db are available, ensure user exists and return user.id.

    Returns integer user id on success, otherwise None.
    """
    try:
        if not user_id or "@" not in str(user_id):
            return None
        models = _get_models()
        target_db = db or _DB
        if not models or not target_db:
            return None
        # models.ensure_user expects (db, email, display_name=None, is_premium=False)
        try:
            user = models.ensure_user(target_db, email=user_id)
        except TypeError:
            # older/alternate signature (email first) attempt
            try:
                user = models.ensure_user(email=user_id)
            except Exception:
                return None
        if user and getattr(user, "id", None):
            return int(user.id)
    except Exception:
        pass
    return None


def _persist_ai_usage_to_db_if_possible(user_id: str, count: int = 1, db: Optional[object] = None) -> None:
    """Best-effort persist AI usage via models.mark_ai_usage

    Behavior:
    - If user_id is email -> ensure user then call models.mark_ai_usage(db, uid, count)
    - If user_id is integer or numeric string, try to cast and call models.mark_ai_usage(db, uid, count)
    - Swallows all exceptions — JSON store remains authoritative.
    """
    try:
        models = _get_models()
        target_db = db or _DB
        if not models or not target_db:
            return
        uid = None
        # email path
        if isinstance(user_id, str) and "@" in user_id:
            uid = _ensure_user_in_db_if_possible(user_id, db=target_db)
        else:
            # numeric path
            try:
                uid = int(user_id)
            except Exception:
                uid = None
        if uid is None:
            return
        try:
            models.mark_ai_usage(target_db, uid, count)
        except Exception:
            # older signature fallback: mark_ai_usage(db, user_id, count)
            try:
                models.mark_ai_usage(uid, count)
            except Exception:
                pass
    except Exception:
        pass


def _persist_download_record_to_db_if_possible(user_id: str, rec: Optional[dict] = None, db: Optional[object] = None) -> None:
    """Best-effort insert a download record into DB using models.insert_download_record.

    - rec may be a dict matching DownloadRecord fields or an instance of models.DownloadRecord.
    - If user_id is email we ensure the user exists and set rec.user_id accordingly.
    - This is optional: if rec is None we do nothing.
    """
    if not rec:
        return
    try:
        models = _get_models()
        target_db = db or _DB
        if not models or not target_db:
            return
        # determine user_id int if email
        uid = None
        if isinstance(user_id, str) and "@" in user_id:
            uid = _ensure_user_in_db_if_possible(user_id, db=target_db)
        else:
            try:
                uid = int(user_id)
            except Exception:
                uid = None

        # If rec already looks like models.DownloadRecord, try inserting directly
        try:
            if hasattr(models, "DownloadRecord") and isinstance(rec, models.DownloadRecord):
                models.insert_download_record(target_db, rec)
                return
        except Exception:
            pass

        # build DownloadRecord dataclass if available in models, otherwise attempt direct SQL via models.insert_download_record
        dd = None
        try:
            if hasattr(models, "DownloadRecord"):
                # map keys safely
                kwargs = {}
                fields = [
                    "job_id", "user_id", "url", "title", "filename", "file_path", "thumbnail",
                    "caption", "hashtags", "size_bytes", "status", "enhancement_used", "ai_used",
                    "created_at", "started_at", "finished_at"
                ]
                for f in fields:
                    if f in rec:
                        kwargs[f] = rec[f]
                # ensure user_id override if we resolved uid
                if uid is not None:
                    kwargs["user_id"] = uid
                # ensure job_id exists
                if "job_id" not in kwargs:
                    kwargs["job_id"] = rec.get("job_id") or f"local:{int(time.time())}"
                dd = models.DownloadRecord(**kwargs)
                models.insert_download_record(target_db, dd)
                return
        except Exception:
            pass

        # last-resort: try calling models.insert_download_record with a tuple-like input (not ideal)
        try:
            models.insert_download_record(target_db, rec)
        except Exception:
            pass
    except Exception:
        pass

# -----------------------
# Internal JSON store helpers
# -----------------------

def _today_str() -> str:
    return date.today().isoformat()


def _atomic_write(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=False)
    try:
        os.replace(tmp, path)
    except Exception:
        pass


def _load_store() -> None:
    global _store
    if os.path.exists(STORE_PATH):
        try:
            with open(STORE_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict) or "users" not in raw:
                raise ValueError("invalid store format")
            _store = raw
        except Exception:
            _store = {"meta": {"last_reset_date": _today_str()}, "users": {}}
            _atomic_write(STORE_PATH, _store)
    else:
        _store = {"meta": {"last_reset_date": _today_str()}, "users": {}}
        _atomic_write(STORE_PATH, _store)


def _maybe_reset_daily_locked() -> None:
    """Must be called with _LOCK held."""
    last = _store.get("meta", {}).get("last_reset_date")
    today = _today_str()
    if last != today:
        _store.setdefault("meta", {})["last_reset_date"] = today
        users = _store.setdefault("users", {})
        for uid, rec in users.items():
            rec["date"] = today
            rec["success"] = 0
            rec["reserved"] = 0
            rec["ai"] = 0


from premium_manager import is_premium_active

def _is_premium(user_id: Optional[str]) -> bool:
    if not user_id:
        return False
    try:
        return is_premium_active(user_id)
    except Exception:
        return user_id in DEMO_PREMIUM_USERS


def _ensure_user_locked(user_id: str) -> Dict[str, Any]:
    users = _store.setdefault("users", {})
    if user_id not in users:
        users[user_id] = {"date": _today_str(), "success": 0, "reserved": 0, "ai": 0}
    rec = users[user_id]
    if rec.get("date") != _today_str():
        rec["date"] = _today_str()
        rec["success"] = 0
        rec["reserved"] = 0
        rec["ai"] = 0
    return rec

# load on import
with _LOCK:
    _load_store()

# -----------------------
# Public API (same names as before, with optional db kwargs)
# -----------------------

def ensure_loaded() -> None:
    """Safe no-op; provided for API compatibility."""
    with _LOCK:
        if not _store or "users" not in _store:
            _load_store()


def get_usage(user_id: str) -> Dict[str, Any]:
    """Return a copy of current usage counters for user (success,reserved,ai,date)."""
    with _LOCK:
        _maybe_reset_daily_locked()
        rec = _ensure_user_locked(user_id)
        return dict(rec)


def get_limit_status(user_id: str) -> Dict[str, Any]:
    """Return detailed limit status for UI: used, reserved, limit, remaining, ai_remaining."""
    with _LOCK:
        _maybe_reset_daily_locked()
        rec = _ensure_user_locked(user_id)
        premium = _is_premium(user_id)
        limit = PREMIUM_DEFAULT_LIMIT if premium else FREE_DEFAULT_LIMIT
        used = int(rec.get("success", 0))
        reserved = int(rec.get("reserved", 0))
        remaining = max(0, limit - (used + reserved))
        ai_used = int(rec.get("ai", 0))
        ai_remaining = PREMIUM_AI_LIMIT - ai_used if premium else 0
        return {
            "user_id": user_id,
            "premium": premium,
            "limit": limit,
            "used": used,
            "reserved": reserved,
            "remaining": remaining,
            "ai_used": ai_used,
            "ai_remaining": ai_remaining,
            "date": rec.get("date", _today_str()),
        }


def reserve_slots(user_id: str, count: int = 1) -> bool:
    """Low-level reservation. Returns True if reserved; False on limit exceed."""
    if count <= 0:
        return True
    with _LOCK:
        _maybe_reset_daily_locked()
        rec = _ensure_user_locked(user_id)
        premium = _is_premium(user_id)
        limit = PREMIUM_DEFAULT_LIMIT if premium else FREE_DEFAULT_LIMIT
        if (rec.get("reserved", 0) + rec.get("success", 0) + count) > limit:
            return False
        rec["reserved"] = rec.get("reserved", 0) + count
        _atomic_write(STORE_PATH, _store)
        return True


def release_reserved_slots(user_id: str, count: int = 1) -> None:
    if count <= 0:
        return
    with _LOCK:
        _maybe_reset_daily_locked()
        rec = _ensure_user_locked(user_id)
        rec["reserved"] = max(0, rec.get("reserved", 0) - count)
        _atomic_write(STORE_PATH, _store)


def mark_successful_download(user_id: str, count: int = 1, db: Optional[object] = None, download_record: Optional[Union[dict, object]] = None) -> None:
    """Mark finished download(s). Decrements reserved and increments success atomically.

    Optional DB persistence: pass `db` or call attach_db() earlier. To persist a download row, provide `download_record` dict
    (keys similar to models.DownloadRecord) or a models.DownloadRecord instance.
    """
    if count <= 0:
        return
    with _LOCK:
        _maybe_reset_daily_locked()
        rec = _ensure_user_locked(user_id)
        rec["reserved"] = max(0, rec.get("reserved", 0) - count)
        rec["success"] = rec.get("success", 0) + count
        _atomic_write(STORE_PATH, _store)

    # best-effort DB persistence (non-blocking, swallow failures)
    try:
        target_db = db or _DB
        if not target_db:
            # but still attempt to persist download_record if provided and global _DB unavailable
            if download_record is None:
                return
        _persist_download_record_to_db_if_possible(user_id, download_record, db=target_db)
    except Exception:
        pass


def can_use_ai(user_id: str, count: int = 1) -> bool:
    if count <= 0:
        return True
    if not _is_premium(user_id):
        return False
    with _LOCK:
        _maybe_reset_daily_locked()
        rec = _ensure_user_locked(user_id)
        if (rec.get("ai", 0) + count) > PREMIUM_AI_LIMIT:
            return False
        return True


def increment_ai(user_id: str, count: int = 1, db: Optional[object] = None) -> bool:
    """Increment AI usage. Optionally persists to DB when db attached or passed.

    Returns True if increment applied (and within limit); False otherwise.
    """
    if count <= 0:
        return True
    if not _is_premium(user_id):
        return False
    with _LOCK:
        _maybe_reset_daily_locked()
        rec = _ensure_user_locked(user_id)
        if (rec.get("ai", 0) + count) > PREMIUM_AI_LIMIT:
            return False
        rec["ai"] = rec.get("ai", 0) + count
        _atomic_write(STORE_PATH, _store)

    # persist to DB best-effort
    try:
        _persist_ai_usage_to_db_if_possible(user_id, count=count, db=db or _DB)
    except Exception:
        pass
    return True

# High-level convenience wrappers that match the names we used in app.py
def check_and_reserve(user_id: str, count: int = 1) -> bool:
    """Called before creating/enqueuing jobs. Reserves `count` slots if allowed."""
    return reserve_slots(user_id, count=count)


def commit_success(user_id: str, count: int = 1, db: Optional[object] = None, download_record: Optional[Union[dict, object]] = None) -> None:
    """Called after successful completion of job(s).

    Optional `download_record` will be persisted to DB when possible.
    """
    mark_successful_download(user_id, count=count, db=db, download_record=download_record)


def revert_reservation(user_id: str, count: int = 1) -> None:
    """Called when a reserved job permanently fails or is rejected (e.g. over-length).
    Releases reserved slot(s without counting them as success.
    """
    release_reserved_slots(user_id, count=count)

# Dev helpers
def reset_all_local_counts() -> None:
    with _LOCK:
        _store["users"] = {}
        _store.setdefault("meta", {})["last_reset_date"] = _today_str()
        _atomic_write(STORE_PATH, _store)

__all__ = [
    "ensure_loaded",
    "get_usage",
    "get_limit_status",
    "check_and_reserve",
    "commit_success",
    "revert_reservation",
    "can_use_ai",
    "increment_ai",
    "reset_all_local_counts",
    # DB helpers
    "attach_db",
    "detach_db",
]
