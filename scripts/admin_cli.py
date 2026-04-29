"""
Admin-only CLI tool for viewing VYDRA system statistics.
Access controlled via ADMIN_EMAIL and ADMIN_PASSWORD_HASH in .env
"""

import os
import getpass
from dotenv import load_dotenv
from werkzeug.security import check_password_hash

from database import get_default_db
from models import get_system_overview


# ----------------------------
# Load environment
# ----------------------------
load_dotenv()

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")


class AdminAuthError(Exception):
    pass


def verify_admin(email: str, password: str) -> None:
    """
    Raise AdminAuthError if credentials are invalid.
    """
    if not ADMIN_EMAIL or not ADMIN_PASSWORD_HASH:
        raise AdminAuthError("Admin credentials not configured")

    if email.strip().lower() != ADMIN_EMAIL.strip().lower():
        raise AdminAuthError("Invalid email")

    if not check_password_hash(ADMIN_PASSWORD_HASH, password):
        raise AdminAuthError("Invalid password")


def get_recent_logs(limit: int = 200):
    log_path = os.path.join("logs", "app.log")

    if not os.path.exists(log_path):
        return ["[NO LOG FILE FOUND]"]

    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    return lines[-limit:]


def main() -> None:
    print("\n=== VYDRA ADMIN SYSTEM STATS ===\n")

    email = input("Admin email: ").strip()
    password = getpass.getpass("Admin password: ")

    try:
        verify_admin(email, password)
    except AdminAuthError as e:
        print(f"\n❌ Unauthorized: {e}")
        return

    db = get_default_db()
    db.connect()

    try:
        stats = get_system_overview(db)
    finally:
        db.close()

    print("\n✅ Access granted\n")
    print(stats)


if __name__ == "__main__":
    main()
