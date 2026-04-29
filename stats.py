"""
Stats helpers for VYDRA.

This module exposes two entry points:

- get_admin_system_overview(email, password)
    Admin-only. Uses ADMIN_EMAIL + ADMIN_PASSWORD_HASH from .env.

- get_public_stats_for_user(user_id)
    Public read-only stats used by the frontend (InvitePage).
    Defensive: if referral/visit tables do not exist yet, returns zeros/empty lists.

Note: This module only reads from the DB. It does not modify data.
"""

import os
import logging
from typing import Dict, Any, List
from dotenv import load_dotenv
from werkzeug.security import check_password_hash

from database import get_default_db
from models import get_system_overview  # admin aggregator (already in models)

# Load .env
load_dotenv()

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")

logger = logging.getLogger(__name__)


# ----------------------------
# Admin auth + admin entrypoint
# ----------------------------
class AdminAuthError(Exception):
    pass


def verify_admin(email: str, password: str) -> bool:
    """Return True if admin credentials match, False otherwise."""
    if not ADMIN_EMAIL or not ADMIN_PASSWORD_HASH:
        return False
    if email.strip().lower() != ADMIN_EMAIL.strip().lower():
        return False
    return check_password_hash(ADMIN_PASSWORD_HASH, password)


def get_admin_system_overview(email: str, password: str) -> Dict[str, Any]:
    """
    Returns the same system overview as models.get_system_overview(db)
    after verifying admin credentials.
    Raises AdminAuthError on failure.
    """
    if not verify_admin(email, password):
        raise AdminAuthError("Unauthorized admin access")

    db = get_default_db()
    db.connect()
    try:
        return get_system_overview(db)
    finally:
        db.close()


# ----------------------------
# Public per-user stats (for InvitePage)
# ----------------------------
def _today_iso_date() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date().isoformat()


def _month_iso() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m")


def get_public_stats_for_user(user_id: str) -> Dict[str, Any]:
    """
    Return the exact shape the frontend expects:

    {
      "visitsToday": int,
      "visitsThisMonth": int,
      "totalVisits": int,
      "totalInvites": int,
      "claimedInviteMilestones": [ "invite_10", ... ],
      "claimedVisitMilestones": [ "50k_month", ... ]
    }

    Defensive: if tables are missing, returns zeros/empty lists.
    """
    db = get_default_db()
    db.connect()
    try:
        # Prepare default response
        resp = {
            "visitsToday": 0,
            "visitsThisMonth": 0,
            "totalVisits": 0,
            "totalInvites": 0,
            "claimedInviteMilestones": [],
            "claimedVisitMilestones": [],
        }

        # Visits: optional table `referral_visits` expected columns:
        # id, referrer_id (text), ip, user_agent, created_at
        try:
            today = _today_iso_date()
            month = _month_iso()

            row = db.fetchone(
                "SELECT COUNT(*) FROM referral_visits WHERE referrer_id = ?",
                (user_id,),
            )
            resp["totalVisits"] = int(row[0]) if row else 0

            row = db.fetchone(
                "SELECT COUNT(*) FROM referral_visits WHERE referrer_id = ? AND DATE(created_at) = ?",
                (user_id, today),
            )
            resp["visitsToday"] = int(row[0]) if row else 0

            # month using strftime for sqlite compatibility
            row = db.fetchone(
                "SELECT COUNT(*) FROM referral_visits WHERE referrer_id = ? AND strftime('%Y-%m', created_at) = ?",
                (user_id, month),
            )
            resp["visitsThisMonth"] = int(row[0]) if row else 0
        except Exception as e:
            # table probably missing or other issue — keep zeros
            logger.debug("referral_visits missing or query failed: %s", e)

        # Invites: optional table `referrals` expected columns:
        # id, referrer_id, referred_user_id, created_at
        try:
            row = db.fetchone(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?",
                (user_id,),
            )
            resp["totalInvites"] = int(row[0]) if row else 0
        except Exception as e:
            logger.debug("referrals missing or query failed: %s", e)

        # Claimed milestones: optional `referral_claims` table expected columns:
        # id, referrer_id, milestone_key, claim_type ('invite'|'visit'), created_at
        try:
            rows = db.fetchall(
                "SELECT milestone_key FROM referral_claims WHERE referrer_id = ? AND claim_type = 'invite'",
                (user_id,),
            )
            if rows:
                resp["claimedInviteMilestones"] = [r[0] for r in rows if r and r[0]]
        except Exception as e:
            logger.debug("referral_claims (invite) missing or query failed: %s", e)

        try:
            rows = db.fetchall(
                "SELECT milestone_key FROM referral_claims WHERE referrer_id = ? AND claim_type = 'visit'",
                (user_id,),
            )
            if rows:
                resp["claimedVisitMilestones"] = [r[0] for r in rows if r and r[0]]
        except Exception as e:
            logger.debug("referral_claims (visit) missing or query failed: %s", e)

        return resp
    finally:
        db.close()
