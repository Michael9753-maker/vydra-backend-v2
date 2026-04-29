import os
from dotenv import load_dotenv
from werkzeug.security import check_password_hash

load_dotenv()

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")

def verify_admin(email: str, password: str) -> bool:
    """
    Centralized admin email + password verification.
    """
    if not ADMIN_EMAIL or not ADMIN_PASSWORD_HASH:
        return False
    if email.strip().lower() != ADMIN_EMAIL.strip().lower():
        return False
    return check_password_hash(ADMIN_PASSWORD_HASH, password)
