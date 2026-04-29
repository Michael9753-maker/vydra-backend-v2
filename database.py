# database.py
"""
Lightweight database helper for VYDRA.

- Default: uses builtin sqlite3 and stores DB file in DATA_DIR (from config.get_config()).
- Optional: if SQLAlchemy is installed and DATABASE_URL is non-sqlite, it can return an SQLAlchemy
  engine/session (but SQLAlchemy is NOT required).
- Thread-safe simple wrappers for common operations included.

This module purposefully avoids importing application models to prevent circular imports.
Call `init_schema()` (or run migrations) after creating models if you want to create tables.
"""

from __future__ import annotations
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Optional, Any, Iterable, List, Tuple, Dict

# optional SQLAlchemy support (only used if DATABASE_URL requests it and SQLAlchemy is available)
_SQLALCHEMY_AVAILABLE = False
try:
    import sqlalchemy as _sqla  # type: ignore
    from sqlalchemy import create_engine as _create_engine  # type: ignore
    from sqlalchemy.orm import sessionmaker as _sessionmaker  # type: ignore
    _SQLALCHEMY_AVAILABLE = True
except Exception:
    _SQLALCHEMY_AVAILABLE = False

# safe config access without forcing app wiring
try:
    from config import get_config
    _CONFIG = get_config()
except Exception:
    _CONFIG = None

# fallback defaults
DEFAULT_DB_FILENAME = "vydra.sqlite3"
DEFAULT_DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", os.path.join(os.getcwd(), "data")))
DEFAULT_DB_PATH = os.path.join(DEFAULT_DATA_DIR, DEFAULT_DB_FILENAME)

class Database:
    """
    A small database facade.
    - If DATABASE_URL is not provided or points to sqlite, uses sqlite3.
    - If DATABASE_URL is a non-sqlite URL and SQLAlchemy is installed, will create an SQLAlchemy engine.
    - Methods:
        connect()
        get_conn() -> contextmanager for sqlite3 connection
        get_cursor() -> contextmanager for cursor
        execute(sql, params=(), commit=False)
        fetchall(sql, params=())
        fetchone(sql, params=())
        init_schema(sql_text)
        health_check()
        close()
    """

    def __init__(self, database_url: Optional[str] = None):
        cfg_db = None
        if _CONFIG is not None:
            cfg_db = getattr(_CONFIG, "DATABASE_URL", None)
        self.database_url = database_url or cfg_db
        self._use_sqlalchemy = False
        self._engine = None
        self._Session = None
        self._sqlite_conn = None  # single shared sqlite connection (thread-safe via lock)
        self._lock = threading.RLock()
        # ensure data dir exists if using sqlite file
        self._sqlite_path = None
        if not self.database_url:
            # default to file in data dir
            PathDir = DEFAULT_DATA_DIR if DEFAULT_DATA_DIR else os.getcwd()
            os.makedirs(PathDir, exist_ok=True)
            self._sqlite_path = os.path.abspath(os.path.join(PathDir, DEFAULT_DB_FILENAME))
        else:
            # parse url; simple detection for sqlite
            if self.database_url.startswith("sqlite://") or self.database_url.endswith(".sqlite") or self.database_url.endswith(".db") or "sqlite" in (self.database_url or ""):
                # extract file path for sqlite:///<path>
                if self.database_url.startswith("sqlite:///"):
                    self._sqlite_path = self.database_url.replace("sqlite:///", "", 1)
                else:
                    # make a best-effort filename if a plain path was given
                    # (e.g., /path/to/vydra.db)
                    path = self.database_url.replace("sqlite://", "")
                    self._sqlite_path = path or self._sqlite_path or DEFAULT_DB_PATH
            else:
                # non-sqlite DB URL
                self._sqlite_path = None
                # if SQLAlchemy available, prefer it
                if _SQLALCHEMY_AVAILABLE:
                    self._use_sqlalchemy = True

        self.connected = False

    def connect(self, timeout: float = 5.0) -> bool:
        """
        Connect to the selected backend.
        Returns True on success, False on failure.
        This method is idempotent.
        """
        with self._lock:
            if self.connected:
                return True
            if self._use_sqlalchemy and _SQLALCHEMY_AVAILABLE and self.database_url:
                try:
                    # create an engine and sessionmaker
                    self._engine = _create_engine(self.database_url, future=True, pool_pre_ping=True)
                    self._Session = _sessionmaker(bind=self._engine, future=True)
                    self.connected = True
                    return True
                except Exception:
                    # fallback to sqlite if SQLAlchemy engine creation fails
                    self._use_sqlalchemy = False

            # default sqlite path
            try:
                db_path = self._sqlite_path or DEFAULT_DB_PATH
                parent = os.path.dirname(db_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                # use check_same_thread=False so connection can be shared across threads
                conn = sqlite3.connect(db_path, timeout=timeout, check_same_thread=False)
                # enable WAL mode for better concurrency
                try:
                    conn.execute("PRAGMA journal_mode=WAL;")
                    conn.execute("PRAGMA foreign_keys = ON;")
                except Exception:
                    pass
                self._sqlite_conn = conn
                self.connected = True
                return True
            except Exception:
                self.connected = False
                return False

    # ---------------------------
    # SQLite helpers (context managers)
    # ---------------------------
    @contextmanager
    def get_conn(self):
        """
        Context manager yielding a sqlite3.Connection if sqlite is used,
        or raising if SQLAlchemy engine is active (use get_session instead).
        """
        if self._use_sqlalchemy:
            raise RuntimeError("SQLAlchemy engine in use: use get_session() instead.")
        if not self.connected:
            ok = self.connect()
            if not ok:
                raise RuntimeError("Failed to connect to sqlite database.")
        try:
            yield self._sqlite_conn
        finally:
            # keep connection open; do not close here
            pass

    @contextmanager
    def get_cursor(self):
        """
        Yield a cursor from the shared sqlite connection. Commits are controlled by caller.
        """
        with self._lock:
            if not self.connected:
                ok = self.connect()
                if not ok:
                    raise RuntimeError("DB not connected")
            cur = self._sqlite_conn.cursor()
            try:
                yield cur
            finally:
                try:
                    cur.close()
                except Exception:
                    pass

    # ---------------------------
    # SQLAlchemy helpers (if enabled)
    # ---------------------------
    def get_session(self):
        """
        Return a SQLAlchemy session if SQLAlchemy is enabled. Caller must close session.
        """
        if not self._use_sqlalchemy:
            raise RuntimeError("SQLAlchemy support is not enabled on this Database instance.")
        if not self.connected:
            ok = self.connect()
            if not ok:
                raise RuntimeError("Failed to connect SQLAlchemy engine.")
        return self._Session()

    # ---------------------------
    # Common convenience helpers
    # ---------------------------
    def execute(self, sql: str, params: Optional[Iterable[Any]] = None, commit: bool = False) -> None:
        """
        Execute SQL (no result). If commit=True, commit afterwards.
        For sqlite backend only.
        """
        if self._use_sqlalchemy:
            # use raw execution via engine
            with self.get_session() as session:
                session.execute(_sqla.text(sql), params or {})
                if commit:
                    session.commit()
            return

        with self.get_cursor() as cur:
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            if commit:
                try:
                    self._sqlite_conn.commit()
                except Exception:
                    pass

    def fetchall(self, sql: str, params: Optional[Iterable[Any]] = None) -> List[Tuple]:
        """Return list of rows for the given query."""
        if self._use_sqlalchemy:
            with self.get_session() as session:
                res = session.execute(_sqla.text(sql), params or {})
                return [tuple(r) for r in res.fetchall()]
        with self.get_cursor() as cur:
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            rows = cur.fetchall()
            return rows

    def fetchone(self, sql: str, params: Optional[Iterable[Any]] = None) -> Optional[Tuple]:
        """Return a single row or None."""
        if self._use_sqlalchemy:
            with self.get_session() as session:
                res = session.execute(_sqla.text(sql), params or {})
                row = res.fetchone()
                return tuple(row) if row else None
        with self.get_cursor() as cur:
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            return cur.fetchone()

    def init_schema(self, sql_text: str) -> None:
        """
        Run schema SQL (can contain multiple statements). Safe to call multiple times.
        Example: init_schema(open('schema.sql').read())
        """
        if not sql_text:
            return
        # sqlite executescripts for multi-statement scripts
        if self._use_sqlalchemy:
            with self.get_session() as session:
                session.execute(_sqla.text(sql_text))
                session.commit()
            return
        with self.get_conn() as conn:
            try:
                conn.executescript(sql_text)
                conn.commit()
            except Exception:
                # fallback to executing line-by-line
                cur = conn.cursor()
                for stmt in filter(None, [s.strip() for s in sql_text.split(";")]):
                    try:
                        cur.execute(stmt)
                    except Exception:
                        pass
                try:
                    conn.commit()
                except Exception:
                    pass

        db.execute("""
        CREATE TABLE IF NOT EXISTS reward_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            reward_key TEXT NOT NULL,
            reward_type TEXT NOT NULL,
            claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, reward_key)
        );
        """)

    def health_check(self) -> bool:
        """
        Quick check that DB is usable.
        """
        try:
            if not self.connected:
                ok = self.connect()
                if not ok:
                    return False
            if self._use_sqlalchemy:
                # simple query
                try:
                    with self.get_session() as session:
                        session.execute(_sqla.text("SELECT 1"))
                    return True
                except Exception:
                    return False
            else:
                with self.get_cursor() as cur:
                    cur.execute("SELECT 1")
                    _ = cur.fetchone()
                return True
        except Exception:
            return False

    def close(self) -> None:
        """Close DB resources (best-effort)."""
        with self._lock:
            try:
                if self._sqlite_conn:
                    try:
                        self._sqlite_conn.close()
                    except Exception:
                        pass
                    self._sqlite_conn = None
            except Exception:
                pass
            try:
                if self._engine:
                    try:
                        self._engine.dispose()
                    except Exception:
                        pass
                    self._engine = None
            except Exception:
                pass
            self.connected = False

# Module-level convenience singleton
_DEFAULT_DB: Optional[Database] = None

def get_default_db(force_new: bool = False) -> Database:
    global _DEFAULT_DB
    if _DEFAULT_DB is None or force_new:
        # attempt to use config if available
        dburl = None
        try:
            if _CONFIG is not None:
                dburl = getattr(_CONFIG, "DATABASE_URL", None)
        except Exception:
            dburl = None
        _DEFAULT_DB = Database(database_url=dburl)
    return _DEFAULT_DB

# Quick usage example (not executed on import)
if __name__ == "__main__":
    db = get_default_db()
    ok = db.connect()
    print("Connected:", ok)
    print("Health:", db.health_check())
    # create a simple table for testing
    try:
        db.init_schema("""
        CREATE TABLE IF NOT EXISTS samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        db.execute("INSERT INTO samples (name) VALUES (?)", ("hello",), commit=True)
        print("Rows:", db.fetchall("SELECT id, name, created_at FROM samples"))
    finally:
        db.close()
