# set_admin_password.py
import os
from getpass import getpass
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from stats import get_admin_system_overview, AdminAuthError

# Load existing .env
load_dotenv()

# Ask user to enter a new password
password = getpass("Enter your new admin password: ")
confirm = getpass("Confirm password: ")
if password != confirm:
    print("Passwords do not match. Exiting.")
    exit(1)

# Generate hash
new_hash = generate_password_hash(password)
print("New password hash generated.")

# Update .env file
env_path = ".env"
lines = []
if os.path.exists(env_path):
    with open(env_path, "r") as f:
        lines = f.readlines()

updated = False
with open(env_path, "w") as f:
    for line in lines:
        if line.startswith("ADMIN_PASSWORD_HASH="):
            f.write(f"ADMIN_PASSWORD_HASH={new_hash}\n")
            updated = True
        else:
            f.write(line)
    if not updated:
        # add line if it didn't exist
        f.write(f"ADMIN_PASSWORD_HASH={new_hash}\n")

print("`.env` updated with new hash.")

# Test login
email = os.getenv("ADMIN_EMAIL")
try:
    data = get_admin_system_overview(email, password)
    print("Login successful! Here are your stats:")
    print(data)
except AdminAuthError:
    print("Login failed. Something went wrong.")
