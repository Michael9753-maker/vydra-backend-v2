# config.py
"""
VYDRA configuration module.

Usage pattern:
    from config import get_config
    cfg = get_config()
    print(cfg.DOWNLOAD_DIR)

All values read from environment variables with sensible defaults.
This module DOES NOT attempt to connect to any external service.
"""

from __future__ import annotations
import os
import logging
from dataclasses import dataclass, field
from typing import Optional
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Base directory for the project (folder that contains this file)
BASE_DIR = Path(__file__).resolve().parent

# Default names (changeable via environment variables)
_DEFAULT_DOWNLOADS = os.getenv("DOWNLOADS_DIR", str(BASE_DIR / "downloads"))
_DEFAULT_DATA = os.getenv("DATA_DIR", str(BASE_DIR / "data"))
_DEFAULT_HISTORY = os.getenv("HISTORY_DIR", str(Path(_DEFAULT_DOWNLOADS) / "meta"))
_DEFAULT_LOG_DIR = os.getenv("LOG_DIR", str(BASE_DIR / "logs"))

@dataclass
class Config:
    # Environment
    ENV: str = os.getenv("ENV", "development")          # production | staging | development
    DEBUG: bool = os.getenv("DEBUG", "0") in ("1", "true", "True")
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))

    # Paths
    DOWNLOAD_DIR: str = os.getenv("DOWNLOADS_DIR", _DEFAULT_DOWNLOADS)
    DATA_DIR: str = os.getenv("DATA_DIR", _DEFAULT_DATA)
    HISTORY_DIR: str = os.getenv("HISTORY_DIR", _DEFAULT_HISTORY)
    LOG_DIR: str = os.getenv("LOG_DIR", _DEFAULT_LOG_DIR)

    # App limits & defaults
    MAX_CONCURRENT_DOWNLOADS: int = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "2"))
    VIDEO_LENGTH_SECONDS_LIMIT: int = int(os.getenv("VIDEO_LENGTH_SECONDS_LIMIT", str(30 * 60)))  # 30 min
    BATCH_LIMIT: int = int(os.getenv("BATCH_LIMIT", "5"))
    POLL_PROGRESS_INTERVAL: float = float(os.getenv("POLL_PROGRESS_INTERVAL", "0.6"))
    RECENT_WINDOW_SECONDS: int = int(os.getenv("RECENT_WINDOW_SECONDS", str(3 * 60 * 60)))  # 3 hours
    HISTORY_MAX_ITEMS: int = int(os.getenv("HISTORY_MAX_ITEMS", "500"))

    # AI / Spend controls
    ENABLE_SMART_ENHANCEMENT: bool = os.getenv("ENABLE_SMART_ENHANCEMENT", "1") in ("1", "true", "True")
    AI_SPEND_CAP_USD: float = float(os.getenv("AI_SPEND_CAP_USD", "25.0"))

    # External integrations (safe to be absent; load from env when present)
    DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL")
    SUPABASE_URL: Optional[str] = os.getenv("SUPABASE_URL")
    SUPABASE_KEY: Optional[str] = os.getenv("SUPABASE_KEY")
    PAYSTACK_SECRET: Optional[str] = os.getenv("PAYSTACK_SECRET")
    SENDGRID_API_KEY: Optional[str] = os.getenv("SENDGRID_API_KEY")

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE: str = os.getenv("LOG_FILE", str(Path(_DEFAULT_LOG_DIR) / "vydra.log"))
    LOG_MAX_BYTES: int = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))  # 10 MB
    LOG_BACKUP_COUNT: int = int(os.getenv("LOG_BACKUP_COUNT", "5"))

    # Storage backend hints (for future)
    STORAGE_BACKEND: str = os.getenv("STORAGE_BACKEND", "local")  # local | s3 | gcs

    # Internal flag: create folders automatically on get_config()
    _ensure_dirs_on_init: bool = field(default=True, repr=False, compare=False)

    def ensure_dirs(self) -> None:
        """Create all configured folders (idempotent)."""
        for p in (self.DOWNLOAD_DIR, self.DATA_DIR, self.HISTORY_DIR, Path(self.LOG_DIR).parent):
            try:
                Path(p).mkdir(parents=True, exist_ok=True)
            except Exception:
                # best-effort: ignore permission issues, caller should handle it
                pass

    def to_dict(self) -> dict:
        """Return a plain dict of config values (safe for logging; sensitive keys omitted)."""
        d = dict(self.__dict__)
        # hide secrets
        for k in ("SUPABASE_KEY", "PAYSTACK_SECRET", "SENDGRID_API_KEY", "DATABASE_URL"):
            if k in d:
                d[k] = None if d[k] is None else "***REDACTED***"
        return d

# Singleton accessor
_CONFIG_SINGLETON: Optional[Config] = None

def get_config(force_reload: bool = False) -> Config:
    """
    Return a shared Config instance. If force_reload=True, re-create from environment.
    The returned instance has had ensure_dirs() called (creates directories).
    """
    global _CONFIG_SINGLETON
    if _CONFIG_SINGLETON is None or force_reload:
        _CONFIG_SINGLETON = Config()
        if _CONFIG_SINGLETON._ensure_dirs_on_init:
            _CONFIG_SINGLETON.ensure_dirs()
    return _CONFIG_SINGLETON

# Logging helper (call early in app startup)
def configure_logging(level: Optional[str] = None, log_file: Optional[str] = None) -> None:
    """
    Configure root logging: console + rotating file handler.
    level: optional override (e.g. 'DEBUG'). If omitted uses config.LOG_LEVEL.
    log_file: optional override for file path.
    """
    cfg = get_config()
    lvl = (level or cfg.LOG_LEVEL).upper()
    try:
        numeric_level = getattr(logging, lvl)
    except Exception:
        numeric_level = logging.INFO

    # Basic configuration (console)
    root = logging.getLogger()
    root.setLevel(numeric_level)

    # remove existing handlers to avoid duplicates on reload
    for h in list(root.handlers):
        root.removeHandler(h)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console_handler.setFormatter(console_formatter)
    root.addHandler(console_handler)

    # Rotating file handler
    lf = log_file or cfg.LOG_FILE
    try:
        Path(lf).parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(lf, maxBytes=cfg.LOG_MAX_BYTES, backupCount=cfg.LOG_BACKUP_COUNT, encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s]: %(message)s")
        file_handler.setFormatter(file_formatter)
        root.addHandler(file_handler)
    except Exception:
        # If file handler creation fails (permissions etc.), continue with console only
        logging.getLogger(__name__).warning("Could not create file logger at %s", lf)

# Light helper to get a logger pre-configured
def get_logger(name: str):
    configure_logging()  # safe to call multiple times
    return logging.getLogger(name)

# Allow module-level quick access
cfg = get_config()
logger = get_logger("vydra.config")

# -------------------------
# REWARD SYSTEM CONFIG
# -------------------------

REWARDS_LIVE = False

# If this module is run directly, print config summary (safe)
if __name__ == "__main__":
    print("VYDRA Config Summary:")
    c = get_config()
    for k, v in sorted(c.to_dict().items()):
        print(f"  {k}: {v}")
