"""
supabase_client.py

Singleton Supabase client + helpers that target the `premium_store` table using `user_id`
as the unique identifier.

Assumptions:
- premium_store has at minimum these columns:
    - user_id (text/uuid, UNIQUE, NOT NULL)   <-- used as identifier here
    - is_premium (boolean, NOT NULL)
    - premium_started_at (timestamptz, NOT NULL)
    - premium_expires_at (timestamptz, NOT NULL)
    - created_at (timestamptz, NOT NULL DEFAULT now())
    - (optional) updated_at (timestamptz)
"""

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from supabase import create_client

# ---------------------------
# Configuration (env preferred)
# ---------------------------
SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    "https://piztbguvkzxobttafvyh.supabase.co"
)
SUPABASE_ANON_KEY = os.environ.get(
    "SUPABASE_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBpenRiZ3V2a3p4b2J0dGFmdnloIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjU0ODc2MjAsImV4cCI6MjA4MTA2MzYyMH0.UJ_i16z443kdVUZygL2Vwm38nHS-3oCOXQjYICsKwOM"
)

# ON_CONFLICT_KEY must match the unique column name in premium_store (default: user_id)
ON_CONFLICT_KEY = os.environ.get("ON_CONFLICT_KEY", "user_id")

# Table targeted by these helpers
TABLE = os.environ.get("PREMIUM_TABLE", "premium_store")

# ---------------------------
# Logging
# ---------------------------
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

# ---------------------------
# Singleton client
# ---------------------------
supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

# ---------------------------
# Date helpers
# ---------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _iso_after_days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

def _parse_iso_to_dt(val) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        dt = val
    else:
        try:
            dt = datetime.fromisoformat(val)
        except Exception:
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            except Exception:
                return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

# ---------------------------
# Response normalization
# ---------------------------
def _extract_data(resp) -> Optional[Any]:
    if resp is None:
        return None
    if hasattr(resp, "data"):
        return resp.data
    if isinstance(resp, dict):
        return resp.get("data")
    try:
        j = getattr(resp, "json", lambda: None)()
        if isinstance(j, dict):
            return j.get("data")
    except Exception:
        pass
    return None

# ---------------------------
# Table helpers (user_id variant)
# ---------------------------
def get_premium_record_by_user_id(user_id: str) -> Optional[Dict[str, Any]]:
    """
    Return the row from premium_store matching `user_id`, or None.
    """
    try:
        resp = supabase.table(TABLE).select("*").eq("user_id", user_id).limit(1).execute()
        data = _extract_data(resp)
        if data:
            return data[0]
        return None
    except Exception as e:
        logger.error("get_premium_record_by_user_id error for %s: %s", user_id, e)
        return None

def is_premium_user(user_id: str) -> bool:
    """
    True if user exists, is_premium is truthy, and premium_expires_at > now().
    """
    rec = get_premium_record_by_user_id(user_id)
    if not rec:
        return False
    if not rec.get("is_premium"):
        return False
    expires_val = rec.get("premium_expires_at")
    expires_dt = _parse_iso_to_dt(expires_val)
    if expires_dt is None:
        # If parsing fails, fallback to boolean flag
        return True
    return expires_dt > datetime.now(timezone.utc)

def grant_premium_for_user(user_id: str, days: int = 30) -> Optional[Dict[str, Any]]:
    """
    Grant premium to `user_id` for `days`. Returns the created/updated row or None.
    """
    started_at = _now_iso()
    expires_at = _iso_after_days(days)
    payload = {
        "user_id": user_id,
        "is_premium": True,
        "premium_started_at": started_at,
        "premium_expires_at": expires_at,
    }
    try:
        resp = supabase.table(TABLE).upsert(payload, on_conflict=ON_CONFLICT_KEY).execute()
        data = _extract_data(resp)
        if data:
            return data[0]
        return None
    except Exception as e:
        logger.error("grant_premium_for_user failed for %s: %s", user_id, e)
        return None

def revoke_premium_for_user(user_id: str) -> bool:
    """
    Revoke premium for `user_id` (mark inactive without writing NULLs).
    Uses epoch timestamps for the start/expiry fields to avoid NOT NULL violations.
    Returns True if update succeeded (or assumed succeeded), False on error.
    """
    epoch_iso = datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat()
    payload = {
        "is_premium": False,
        "premium_started_at": epoch_iso,
        "premium_expires_at": epoch_iso,
        # update updated_at if your table has it
        "updated_at": _now_iso(),
    }
    try:
        resp = supabase.table(TABLE).update(payload).eq("user_id", user_id).execute()
        data = _extract_data(resp)
        if data is not None:
            return bool(data)
        # If client does not return data on update, assume success if no exception
        return True
    except Exception as e:
        logger.error("revoke_premium_for_user failed for %s: %s", user_id, e)
        return False

def upsert_premium_record(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Generic upsert into premium_store. `record` must include ON_CONFLICT_KEY (user_id).
    Returns the upserted row or None.
    """
    try:
        resp = supabase.table(TABLE).upsert(record, on_conflict=ON_CONFLICT_KEY).execute()
        data = _extract_data(resp)
        if data:
            return data[0]
        return None
    except Exception as e:
        logger.error("upsert_premium_record failed: %s", e)
        return None

# ---------------------------
# Quick smoke test (module run)
# ---------------------------
if __name__ == "__main__":
    test_user = os.environ.get("VYDRA_TEST_USER_ID", "test-user-1")
    logger.info("Running smoke test against table: %s using ON_CONFLICT_KEY=%s", TABLE, ON_CONFLICT_KEY)
    r = grant_premium_for_user(test_user, days=1)
    logger.info("Grant result: %s", r)
    logger.info("Is premium active? %s", is_premium_user(test_user))
