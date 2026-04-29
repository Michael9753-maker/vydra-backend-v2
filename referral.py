"""
Referral stats for VYDRA.
All DB operations go through models.py / database.py.
"""

from typing import Dict, Any, List
from database import get_default_db

def get_total_referrals(db) -> int:
    """
    Returns total number of referrals in the system.
    Assumes a table `referrals` exists with columns: id, referrer_user_id, referred_user_id, created_at
    """
    row = db.fetchone("SELECT COUNT(*) FROM referrals")
    return int(row[0] if row else 0)

def get_active_referrers(db) -> int:
    """
    Returns number of users who have at least 1 referral.
    """
    row = db.fetchone("SELECT COUNT(DISTINCT referrer_user_id) FROM referrals")
    return int(row[0] if row else 0)

def has_any_successful_download(db, user_id: str) -> bool:
    row = db.fetchone(
        "SELECT 1 FROM download_history WHERE user_id = ? LIMIT 1",
        (user_id,)
    )
    return bool(row)


def record_referral(db, referrer_id: str, referred_id: str) -> bool:
    if referrer_id == referred_id:
        return False
    try:
        db.execute(
            "INSERT OR IGNORE INTO referrals (referrer_user_id, referred_user_id) VALUES (?, ?)",
            (referrer_id, referred_id)
        )
        return True
    except Exception:
        return False

def get_top_referrers(db, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Returns top referrers sorted by number of invites.
    Each entry: {'user_id': ..., 'invite_count': ...}
    """
    rows = db.fetchall(
        "SELECT referrer_user_id, COUNT(*) as cnt FROM referrals GROUP BY referrer_user_id ORDER BY cnt DESC LIMIT ?",
        (limit,)
    )
    return [{"user_id": r[0], "invite_count": r[1]} for r in rows] if rows else []

def get_referral_overview(db) -> Dict[str, Any]:
    """
    Combines all referral stats for admin overview
    """
    return {
        "total_referrals": get_total_referrals(db),
        "active_referrers": get_active_referrers(db),
        "top_referrers": get_top_referrers(db)
    }
