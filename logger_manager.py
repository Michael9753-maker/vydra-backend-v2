import os
import threading
from datetime import datetime

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "app.log")

_log_lock = threading.Lock()


def _ensure_log_dir():
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR, exist_ok=True)


def log_event(category: str, message: str) -> None:
    """
    Thread-safe logging for VYDRA.
    Appends a timestamped line to logs/app.log
    Format:
    [2025-12-19 14:03:55] [CATEGORY] Message...
    """
    try:
        _ensure_log_dir()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] [{category.upper()}] {message}\n"

        with _log_lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)

    except Exception:
        # Fail-safe: NEVER allow logging failure to break VYDRA
        pass


def log_exception(category: str, error: Exception):
    """
    Captures exception details safely.
    """
    msg = f"{type(error).__name__}: {str(error)}"
    log_event(category, msg)
