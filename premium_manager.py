"""
premium_manager.py
Final compatibility update: use `resp.error` / `resp.data` fields to validate responses from the Supabase client
(compatible with postgrest / supabase-py variations that return APIResponse objects).

Notes:
- Use SUPABASE_KEY (service role) for full permissions during tests.
- Keeps PLAN_DURATIONS, plan storage, upsert behavior.

"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from logger_manager import log_event, log_exception

try:
    from supabase import create_client
except Exception:
    create_client = None

LOG = logging.getLogger("premium_manager")
LOG.setLevel(logging.INFO)

PLAN_DURATIONS = {
    "1-week": 7,
    "1-month": 30,
    "quarter": 90,
    "year": 365,
}

SUPABASE_URL = os.getenv("SUPABASE_URL")

# Accept either SUPABASE_KEY or SUPABASE_ANON_KEY
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
if not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_KEY is required for premium operations")

TABLE_NAME = os.getenv("VYDRA_PREMIUM_TABLE", "premium_store")


# --- Helpers ---------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY (or SUPABASE_ANON_KEY) must be set in the environment")
    if create_client is None:
        raise RuntimeError("supabase client library not available. Install with `pip install supabase`")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _check_response(resp):
    """Normalize Supabase/postgrest response objects and raise on error.

    This function supports client versions that return APIResponse objects
    with `.error` and `.data`, and also handles cases where `status_code`
    is present. Returns `data` on success, raises RuntimeError with error info
    on failure.
    """
    # Prefer explicit error attribute when available
    err = None
    data = None

    if hasattr(resp, "error"):
        err = getattr(resp, "error")

    # Some client versions expose status_code
    if hasattr(resp, "status_code"):
        try:
            status = int(getattr(resp, "status_code"))
        except Exception:
            status = None
        if status is not None and status >= 400:
            err = getattr(resp, "data", err) or err or f"HTTP {status}"

    # Extract data if present
    if hasattr(resp, "data"):
        data = getattr(resp, "data")

    # Some wrappers may provide json() callable
    if data is None and hasattr(resp, "json"):
        try:
            data = resp.json()
        except Exception:
            pass

    if err:
        LOG.error("Supabase API error: %s (data=%s)", err, data)
        raise RuntimeError({"error": err, "data": data})

    return data


# --- Core functions --------------------------------------------------------

def get_premium(user_id: str) -> Optional[Dict[str, Any]]:
    sb = _get_supabase()
    resp = sb.table(TABLE_NAME).select("*").eq("user_id", user_id).limit(1).execute()
    data = _check_response(resp)
    if not data:
        return None
    row = data[0]

    def _parse_ts(v):
        if v is None:
            return None
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v)
            except Exception:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v

    return {
        "user_id": row.get("user_id"),
        "plan": row.get("plan"),
        "started_at": _parse_ts(row.get("started_at")),
        "expires_at": _parse_ts(row.get("expires_at")),
        "source": row.get("source"),
        "metadata": row.get("metadata"),
    }


def is_premium_active(user_id: str) -> bool:
    rec = get_premium(user_id)
    if not rec:
        return False
    expires_at = rec.get("expires_at")
    return expires_at > _now() if expires_at else False


def grant_premium(user_id: str, plan: Optional[str] = None, days: Optional[int] = None,
                  source: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if plan is None and days is None:
        raise ValueError("Either `plan` or `days` must be provided")

    if plan is not None and plan not in PLAN_DURATIONS and days is None:
        raise ValueError(f"Unknown plan: {plan}")

    grant_days = PLAN_DURATIONS.get(plan) if plan else days
    if grant_days is None or grant_days <= 0:
        raise ValueError("`days` must be a positive integer")

    now = _now()
    existing = get_premium(user_id)
    base = existing["expires_at"] if existing and existing.get("expires_at") and existing["expires_at"] > now else now
    new_expires = base + timedelta(days=grant_days)
    started_at = existing.get("started_at") if existing and existing.get("started_at") else now

    payload = {
        "user_id": user_id,
        "plan": plan or "custom",
        "started_at": started_at.isoformat(),
        "expires_at": new_expires.isoformat(),
        "source": source,
        "metadata": metadata or {},
    }

    sb = _get_supabase()
    resp = sb.table(TABLE_NAME).upsert(payload, on_conflict="user_id").execute()
    _check_response(resp)

    return get_premium(user_id)


def extend_premium(user_id: str, days: int, source: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return grant_premium(user_id, plan=None, days=days, source=source, metadata=metadata)


def revoke_premium(user_id: str, immediate: bool = True) -> Dict[str, Any]:
    now = _now()
    sb = _get_supabase()
    if immediate:
        resp = sb.table(TABLE_NAME).update({"expires_at": now.isoformat()}).eq("user_id", user_id).execute()
        _check_response(resp)
        return get_premium(user_id)
    else:
        resp = sb.table(TABLE_NAME).delete().eq("user_id", user_id).execute()
        _check_response(resp)
        return {"user_id": user_id, "deleted": True}


# --- CLI / Quick tests -----------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Simple premium manager CLI (for quick tests)")
    parser.add_argument("action", choices=["grant", "extend", "revoke", "status"], help="action")
    parser.add_argument("user_id", help="user id")
    parser.add_argument("--plan", help="plan string (1-week, 1-month, quarter, year)")
    parser.add_argument("--days", type=int, help="days override")
    parser.add_argument("--no-delete", dest="delete", action="store_false", help="for revoke: do not delete record, only expire")

    args = parser.parse_args()

    if args.action == "status":
        print(get_premium(args.user_id))
        print("active:", is_premium_active(args.user_id))
    elif args.action == "grant":
        rec = grant_premium(args.user_id, plan=args.plan, days=args.days)
        print(rec)
    elif args.action == "extend":
        if not args.days:
            raise SystemExit("--days is required for extend")
        rec = extend_premium(args.user_id, args.days)
        print(rec)
    elif args.action == "revoke":
        rec = revoke_premium(args.user_id, immediate=True)
        print(rec)