# models.py
"""
Database models and helper functions for VYDRA.

Design goals:
- Work with sqlite via the Database facade (preferred).
- If SQLAlchemy is installed and DATABASE_URL requests it, support ORM models as an option.
- Provide a plain SQL schema string for sqlite->init_schema usage.
- Provide dataclasses for light-weight usage without DB.
- Provide helpers for inserting/fetching download history and lightweight user ops.

Usage (sqlite default):
    from database import get_default_db
    from models import create_schema, insert_download_record, get_recent_downloads

    db = get_default_db()
    db.connect()
    create_schema(db)
    insert_download_record(db, DownloadRecord(...))
"""

from __future__ import annotations
import json
import time
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta

# Try to detect SQLAlchemy availability (optional)
_SQLALCHEMY_AVAILABLE = True
try:
    import sqlalchemy as sa  # type: ignore
    from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, JSON as SAJSON
    from sqlalchemy.ext.declarative import declarative_base
    from sqlalchemy.orm import relationship
    Base = declarative_base()
except Exception:
    _SQLALCHEMY_AVAILABLE = False
    sa = None
    Base = None

# Plain SQL schema (sqlite-friendly). Keep types generic for portability.
SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE,
    display_name TEXT,
    is_premium INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    used INTEGER DEFAULT 0,
    last_updated TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE,
    user_id INTEGER,
    url TEXT,
    title TEXT,
    filename TEXT,
    file_path TEXT,
    thumbnail TEXT,
    caption TEXT,
    hashtags TEXT,
    size_bytes INTEGER,
    status TEXT,
    enhancement_used INTEGER DEFAULT 0,
    ai_used INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_downloads_user_created ON downloads(user_id, created_at);
"""

# ----------------------------
# Dataclasses for lightweight usage
# ----------------------------
@dataclass
class User:
    id: Optional[int] = None
    email: Optional[str] = None
    display_name: Optional[str] = None
    is_premium: bool = False
    created_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "display_name": self.display_name,
            "is_premium": int(bool(self.is_premium)),
            "created_at": self.created_at
        }

@dataclass
class DownloadRecord:
    job_id: str
    user_id: Optional[int] = None
    url: Optional[str] = None
    title: Optional[str] = None
    filename: Optional[str] = None
    file_path: Optional[str] = None
    thumbnail: Optional[str] = None
    caption: Optional[str] = None
    hashtags: Optional[List[str]] = None
    size_bytes: Optional[int] = None
    status: str = "queued"
    enhancement_used: bool = False
    ai_used: int = 0
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def to_db_tuple(self):
        return (
            self.job_id,
            self.user_id,
            self.url,
            self.title,
            self.filename,
            self.file_path,
            self.thumbnail,
            self.caption,
            json.dumps(self.hashtags or []),
            self.size_bytes or 0,
            self.status,
            1 if self.enhancement_used else 0,
            int(self.ai_used or 0),
            self.created_at,
            self.started_at,
            self.finished_at
        )

# ----------------------------
# Optional SQLAlchemy ORM models
# ----------------------------
if _SQLALCHEMY_AVAILABLE:
    class UserORM(Base):
        __tablename__ = "users"
        id = Column(Integer, primary_key=True, autoincrement=True)
        email = Column(String, unique=True, nullable=True)
        display_name = Column(String, nullable=True)
        is_premium = Column(Boolean, default=False)
        created_at = Column(DateTime)

        ai_usages = relationship("AIUsageORM", backref="user", cascade="all,delete-orphan")
        downloads = relationship("DownloadORM", backref="user", cascade="all,delete-orphan")

    class AIUsageORM(Base):
        __tablename__ = "ai_usage"
        id = Column(Integer, primary_key=True, autoincrement=True)
        user_id = Column(Integer, nullable=True)
        used = Column(Integer, default=0)
        last_updated = Column(DateTime)

    class DownloadORM(Base):
        __tablename__ = "downloads"
        id = Column(Integer, primary_key=True, autoincrement=True)
        job_id = Column(String, unique=True, nullable=False)
        user_id = Column(Integer, nullable=True)
        url = Column(Text)
        title = Column(Text)
        filename = Column(String)
        file_path = Column(String)
        thumbnail = Column(String)
        caption = Column(Text)
        hashtags = Column(Text)  # store JSON text for portability
        size_bytes = Column(Integer)
        status = Column(String)
        enhancement_used = Column(Boolean, default=False)
        ai_used = Column(Integer, default=0)
        created_at = Column(DateTime)
        started_at = Column(DateTime)
        finished_at = Column(DateTime)

# ----------------------------
# Helper functions (sqlite-friendly)
# ----------------------------
def create_schema(db) -> None:
    """
    Create required tables using the provided Database instance.
    db should be an instance of your Database facade (from database.get_default_db()).
    """
    if db is None:
        raise ValueError("Database instance required")
    # If SQLAlchemy engine in use, create via ORM metadata
    try:
        if _SQLALCHEMY_AVAILABLE and getattr(db, "_use_sqlalchemy", False) and getattr(db, "_engine", None):
            # safe to create via ORM
            Base.metadata.create_all(bind=db._engine)
            return
    except Exception:
        # if ORM creation fails, fall through to SQL script below
        pass

    # fallback to plain SQL executescript (sqlite)
    db.init_schema(SCHEMA_SQL)


def ensure_user(db, email: str, display_name: Optional[str] = None, is_premium: bool = False) -> User:
    """
    Ensure a user exists by email. Returns a User dataclass (with id).
    """
    if not email:
        raise ValueError("email required")
    # try to find existing
    row = db.fetchone("SELECT id, email, display_name, is_premium, created_at FROM users WHERE email = ?", (email,))
    if row:
        uid, em, dn, prem, created = row
        return User(id=uid, email=em, display_name=dn, is_premium=bool(prem), created_at=created)
    # insert
    now = datetime.now(timezone.utc).isoformat()
    db.execute("INSERT INTO users (email, display_name, is_premium, created_at) VALUES (?, ?, ?, ?)",
               (email, display_name, 1 if is_premium else 0, now), commit=True)
    row = db.fetchone("SELECT id, email, display_name, is_premium, created_at FROM users WHERE email = ?", (email,))
    if row:
        uid, em, dn, prem, created = row
        return User(id=uid, email=em, display_name=dn, is_premium=bool(prem), created_at=created)
    # fallback
    return User(email=email, display_name=display_name, is_premium=is_premium, created_at=now)


def insert_download_record(db, rec: DownloadRecord) -> int:
    """
    Insert a DownloadRecord into downloads table. Returns inserted row id (if sqlite) or 0.
    """
    if not rec or not isinstance(rec, DownloadRecord):
        raise ValueError("DownloadRecord instance required")
    sql = """
    INSERT OR REPLACE INTO downloads (
        job_id, user_id, url, title, filename, file_path, thumbnail, caption, hashtags, size_bytes,
        status, enhancement_used, ai_used, created_at, started_at, finished_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    db.execute(sql, rec.to_db_tuple(), commit=True)
    # try to return last row id (sqlite)
    try:
        row = db.fetchone("SELECT id FROM downloads WHERE job_id = ?", (rec.job_id,))
        if row:
            return int(row[0])
    except Exception:
        pass
    return 0


def get_recent_downloads(db, user_id: Optional[int], limit: int = 20, max_age_seconds: int = 3 * 3600) -> List[DownloadRecord]:
    """
    Return recent DownloadRecord objects for the given user_id (or all users if None).
    Filters by created_at within max_age_seconds.
    """
    rows = []
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
    if user_id:
        rows = db.fetchall("SELECT job_id, user_id, url, title, filename, file_path, thumbnail, caption, hashtags, size_bytes, status, enhancement_used, ai_used, created_at, started_at, finished_at FROM downloads WHERE user_id = ? AND (created_at >= ?) ORDER BY created_at DESC LIMIT ?",
                           (user_id, cutoff, limit))
    else:
        rows = db.fetchall("SELECT job_id, user_id, url, title, filename, file_path, thumbnail, caption, hashtags, size_bytes, status, enhancement_used, ai_used, created_at, started_at, finished_at FROM downloads WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                           (cutoff, limit))
    out = []
    for r in rows:
        try:
            job_id, uid, url, title, filename, file_path, thumbnail, caption, hashtags_json, size_bytes, status, enhancement_used, ai_used, created_at, started_at, finished_at = r
            tags = []
            try:
                tags = json.loads(hashtags_json or "[]")
            except Exception:
                tags = []
            rec = DownloadRecord(
                job_id=job_id,
                user_id=uid,
                url=url,
                title=title,
                filename=filename,
                file_path=file_path,
                thumbnail=thumbnail,
                caption=caption,
                hashtags=tags,
                size_bytes=size_bytes,
                status=status or "finished",
                enhancement_used=bool(enhancement_used),
                ai_used=int(ai_used or 0),
                created_at=created_at,
                started_at=started_at,
                finished_at=finished_at
            )
            out.append(rec)
        except Exception:
            continue
    return out


def mark_ai_usage(db, user_id: int, count: int = 1) -> None:
    """
    Increment or create an ai_usage counter for a user. Best-effort.
    """
    try:
        row = db.fetchone("SELECT id, used FROM ai_usage WHERE user_id = ?", (user_id,))
        if row:
            aid, used = row
            new_used = int(used or 0) + int(count)
            db.execute("UPDATE ai_usage SET used = ?, last_updated = ? WHERE id = ?", (new_used, datetime.now(timezone.utc).isoformat(), aid), commit=True)
        else:
            db.execute("INSERT INTO ai_usage (user_id, used, last_updated) VALUES (?, ?, ?)", (user_id, int(count), datetime.now(timezone.utc).isoformat()), commit=True)
    except Exception:
        # non-fatal; caller should handle counting if DB unavailable
        pass

# ----------------------------
# STATS / AGGREGATION HELPERS
# ----------------------------
# These are defensive aggregation helpers intended for the admin-only stats module.
# They must not raise if a table does not exist; instead they return 0 / empty list.

def _today_iso_date() -> str:
    """Return current UTC date string 'YYYY-MM-DD' for DATE(...) comparisons."""
    return datetime.now(timezone.utc).date().isoformat()

def _month_start_iso() -> str:
    """Return ISO datetime for the start of the current month in UTC."""
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat()

def count_total_downloads(db) -> int:
    """Total number of download records (all time)."""
    try:
        row = db.fetchone("SELECT COUNT(*) FROM downloads")
        return int(row[0]) if row else 0
    except Exception:
        return 0

def count_downloads_today(db) -> int:
    """Number of downloads created today (UTC)."""
    try:
        today = _today_iso_date()
        row = db.fetchone("SELECT COUNT(*) FROM downloads WHERE DATE(created_at) = ?", (today,))
        return int(row[0]) if row else 0
    except Exception:
        return 0

def count_users_total(db) -> int:
    """Total number of users."""
    try:
        row = db.fetchone("SELECT COUNT(*) FROM users")
        return int(row[0]) if row else 0
    except Exception:
        return 0

def count_users_today(db) -> int:
    """Number of users created today (UTC)."""
    try:
        today = _today_iso_date()
        row = db.fetchone("SELECT COUNT(*) FROM users WHERE DATE(created_at) = ?", (today,))
        return int(row[0]) if row else 0
    except Exception:
        return 0

def count_users_this_month(db) -> int:
    """Number of users created since the start of this month (UTC)."""
    try:
        start = _month_start_iso()
        row = db.fetchone("SELECT COUNT(*) FROM users WHERE created_at >= ?", (start,))
        return int(row[0]) if row else 0
    except Exception:
        return 0

def count_active_users(db) -> int:
    """
    Count of distinct users who have activity.
    We consider a user 'active' if they appear in downloads as user_id.
    """
    try:
        row = db.fetchone("SELECT COUNT(DISTINCT user_id) FROM downloads WHERE user_id IS NOT NULL")
        return int(row[0]) if row else 0
    except Exception:
        return 0

def count_active_premium_users(db) -> int:
    """
    Count of distinct premium users who have at least one download.
    Uses users.is_premium flag if available.
    """
    try:
        row = db.fetchone("""
            SELECT COUNT(DISTINCT u.id)
            FROM users u
            JOIN downloads d ON d.user_id = u.id
            WHERE COALESCE(u.is_premium, 0) = 1
        """)
        return int(row[0]) if row else 0
    except Exception:
        # Fallback: if users.is_premium not present, try counting users where is_premium column absent gracefully
        try:
            # attempt simpler heuristic: count users marked premium (if column exists)
            row = db.fetchone("SELECT COUNT(*) FROM users WHERE is_premium = 1")
            return int(row[0]) if row else 0
        except Exception:
            return 0

def sum_ai_usage(db) -> int:
    """
    Sum of the 'used' column in ai_usage table. Represents total AI usage count.
    """
    try:
        row = db.fetchone("SELECT COALESCE(SUM(used), 0) FROM ai_usage")
        return int(row[0]) if row else 0
    except Exception:
        return 0

def sum_ai_usage_today(db) -> int:
    """
    Sum of 'used' in ai_usage where last_updated is today (UTC).
    """
    try:
        today = _today_iso_date()
        row = db.fetchone("SELECT COALESCE(SUM(used), 0) FROM ai_usage WHERE DATE(last_updated) = ?", (today,))
        return int(row[0]) if row else 0
    except Exception:
        return 0

def count_total_referrals(db) -> int:
    """
    Total referrals count.
    Returns 0 if referrals table does not exist.
    """
    try:
        row = db.fetchone("SELECT COUNT(*) FROM referrals")
        return int(row[0]) if row else 0
    except Exception:
        return 0

def count_active_referrers(db) -> int:
    """
    Count of distinct referrers. Returns 0 if referrals table missing.
    """
    try:
        row = db.fetchone("SELECT COUNT(DISTINCT referrer_id) FROM referrals")
        return int(row[0]) if row else 0
    except Exception:
        return 0

def get_top_referrers(db, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Return top referrers as list of dicts: [{'referrer_id': id, 'total': n}, ...]
    If referrals table missing, returns empty list.
    """
    try:
        rows = db.fetchall("""
            SELECT referrer_id, COUNT(*) as total
            FROM referrals
            GROUP BY referrer_id
            ORDER BY total DESC
            LIMIT ?
        """, (limit,))
        out = []
        for r in rows:
            try:
                rid, total = r
                out.append({"referrer_id": rid, "total": int(total)})
            except Exception:
                continue
        return out
    except Exception:
        return []

def count_files(db) -> int:
    """Total files (download records)."""
    return count_total_downloads(db)

def sum_storage_bytes(db) -> int:
    """Sum of size_bytes from downloads (returns 0 if column/table missing)."""
    try:
        row = db.fetchone("SELECT COALESCE(SUM(size_bytes), 0) FROM downloads")
        return int(row[0]) if row else 0
    except Exception:
        return 0

def sum_revenue(db) -> float:
    """
    Sum of successful payments. Returns 0.0 if payments table missing.
    Assumes payments table has `amount` and `status` columns.
    """
    try:
        row = db.fetchone("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'success'")
        return float(row[0]) if row else 0.0
    except Exception:
        return 0.0

def sum_revenue_today(db) -> float:
    """
    Sum of today's successful payments. Returns 0.0 if payments table missing.
    """
    try:
        today = _today_iso_date()
        row = db.fetchone("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'success' AND DATE(created_at) = ?", (today,))
        return float(row[0]) if row else 0.0
    except Exception:
        return 0.0

def get_system_overview(db) -> Dict[str, Any]:
    """
    Aggregate all relevant system metrics into a single payload for the admin dashboard.
    Defensive: missing tables return 0 / empty lists.
    """
    try:
        downloads = {
            "total": count_total_downloads(db),
            "today": count_downloads_today(db)
        }

        users = {
            "total": count_users_total(db),
            "today": count_users_today(db),
            "this_month": count_users_this_month(db),
            "active": count_active_users(db),
            "premium_active": count_active_premium_users(db)
        }

        ai = {
            "total_requests": sum_ai_usage(db),
            "today_requests": sum_ai_usage_today(db)
        }

        referrals = {
            "total_referrals": count_total_referrals(db),
            "active_referrers": count_active_referrers(db),
            "top_referrers": get_top_referrers(db, limit=10)
        }

        storage = {
            "total_files": count_files(db),
            # convert bytes -> megabytes estimate for convenience (float)
            "estimated_storage_mb": round(sum_storage_bytes(db) / 1024.0 / 1024.0, 2)
        }

        revenue = {
            "total_revenue": sum_revenue(db),
            "today_revenue": sum_revenue_today(db)
        }

        overview = {
            "downloads": downloads,
            "users": users,
            "ai": ai,
            "referrals": referrals,
            "storage": storage,
            "revenue": revenue
        }
        return overview
    except Exception:
        # If something unexpected occurs, return a defensive empty overview
        return {
            "downloads": {"total": 0, "today": 0},
            "users": {"total": 0, "today": 0, "this_month": 0, "active": 0, "premium_active": 0},
            "ai": {"total_requests": 0, "today_requests": 0},
            "referrals": {"total_referrals": 0, "active_referrers": 0, "top_referrers": []},
            "storage": {"total_files": 0, "estimated_storage_mb": 0.0},
            "revenue": {"total_revenue": 0.0, "today_revenue": 0.0}
        }
    


# ----------------------------
# Exported API
# ----------------------------
__all__ = [
    "SCHEMA_SQL",
    "create_schema",
    "User",
    "DownloadRecord",
    "ensure_user",
    "insert_download_record",
    "get_recent_downloads",
    "mark_ai_usage",
    # stats helpers
    "count_total_downloads",
    "count_downloads_today",
    "count_users_total",
    "count_users_today",
    "count_users_this_month",
    "count_active_users",
    "count_active_premium_users",
    "sum_ai_usage",
    "sum_ai_usage_today",
    "count_total_referrals",
    "count_active_referrers",
    "get_top_referrers",
    "count_files",
    "sum_storage_bytes",
    "sum_revenue",
    "sum_revenue_today",
    "get_system_overview",
    # ORM exports if available
    "UserORM" if _SQLALCHEMY_AVAILABLE else None,
    "DownloadORM" if _SQLALCHEMY_AVAILABLE else None,
]

# Clean up None entries if SQLAlchemy absent
__all__ = [x for x in __all__ if x is not None]
