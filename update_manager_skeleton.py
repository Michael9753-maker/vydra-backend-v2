"""update_manager_git_with_hooks.py
Git-backed silent auto-update manager for VYDRA with pre/post update hooks.

Enhancements added:
- MAX_BACKUPS support and cleanup_old_backups()
- update history JSON log (UPDATE_HISTORY_FILE) with append/truncate
- Telegram and email notifications (optional via env vars)
- Rotate backups into timestamped subfolders instead of overwriting a single backup folder
- Notifications and cleanup are invoked on successful update and on failures
"""

import os
import shutil
import subprocess
import logging
import threading
import time
import json
import smtplib
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import quote_plus
from email.message import EmailMessage


def env(name, default=None):
    return os.environ.get(name, default)

# Core repo/config
REPO_URL = env("REPO_URL")
REPO_BRANCH = env("REPO_BRANCH", "main")
LIVE_DIR = Path(env("LIVE_DIR", "./vydra_live")).resolve()
BACKUP_DIR = Path(env("BACKUP_DIR", "./vydra_backups")).resolve()  # now a backups root
STAGING_DIR = Path(env("STAGING_DIR", "./vydra_staging")).resolve()
MIGRATE_CMD = env("MIGRATE_CMD")
VERIFY_URL = env("VERIFY_URL")
VERIFY_TIMEOUT = int(env("VERIFY_TIMEOUT", "5"))
GIT_CLONE_TIMEOUT = int(env("GIT_CLONE_TIMEOUT", "120"))
LOG_FILE = env("LOG_FILE", "./update_manager_git_with_hooks.log")
PRE_UPDATE_CMD = env("PRE_UPDATE_CMD")
POST_UPDATE_CMD = env("POST_UPDATE_CMD")
PRE_TIMEOUT = int(env("PRE_TIMEOUT", "60"))
POST_TIMEOUT = int(env("POST_TIMEOUT", "60"))

# New optional features
MAX_BACKUPS = int(env("MAX_BACKUPS", "3"))
UPDATE_HISTORY_FILE = Path(env("UPDATE_HISTORY_FILE", str(LIVE_DIR.parent / "update_history.json"))).resolve()
UPDATE_HISTORY_MAX = int(env("UPDATE_HISTORY_MAX", "100"))

# Telegram notification (optional)
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")

# Email notification (optional)
SMTP_HOST = env("SMTP_HOST")
SMTP_PORT = int(env("SMTP_PORT", "587")) if env("SMTP_HOST") else None
SMTP_USER = env("SMTP_USER")
SMTP_PASS = env("SMTP_PASS")
EMAIL_FROM = env("EMAIL_FROM")
EMAIL_TO = env("EMAIL_TO")

# Logging setup
logger = logging.getLogger("update_manager_git_hooks")
logger.setLevel(logging.DEBUG)
fh = logging.FileHandler(LOG_FILE)
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(fh)
sh = logging.StreamHandler()
sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(sh)

# Ensure backup root exists
try:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

# -----------------------
# Utilities: notifications and history
# -----------------------

def _atomic_write_json(path: Path, data: object) -> bool:
    try:
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, str(path))
        return True
    except Exception as e:
        logger.exception("atomic write failed: %s", e)
        return False


def append_update_history(entry: dict) -> None:
    """Append an entry to the update history JSON file and trim to UPDATE_HISTORY_MAX."""
    try:
        UPDATE_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        history = []
        if UPDATE_HISTORY_FILE.exists():
            try:
                with open(UPDATE_HISTORY_FILE, "r", encoding="utf-8") as fh:
                    history = json.load(fh) or []
            except Exception:
                history = []
        history.insert(0, entry)
        history = history[:UPDATE_HISTORY_MAX]
        _atomic_write_json(UPDATE_HISTORY_FILE, history)
    except Exception:
        logger.exception("failed to append update history")


def send_telegram(message: str) -> bool:
    """Send a simple Telegram message via Bot API. Returns True on success or if not configured."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        text = quote_plus(message)
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage?chat_id={TELEGRAM_CHAT_ID}&text={text}"
        req = Request(url, headers={"User-Agent": "vydra-update-notifier/1.0"})
        with urlopen(req, timeout=10) as resp:
            code = resp.getcode()
            logger.info("Telegram notify status: %s", code)
            return 200 <= code < 300
    except Exception as e:
        logger.exception("Telegram notify failed: %s", e)
        return False


def send_email(subject: str, body: str) -> bool:
    """Send an email notification via SMTP if configured. Returns True on success or False."""
    if not SMTP_HOST or not EMAIL_TO or not EMAIL_FROM:
        return False
    try:
        msg = EmailMessage()
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO
        msg["Subject"] = subject
        msg.set_content(body)

        # TLS connection
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT or 587, timeout=10)
        try:
            server.starttls()
        except Exception:
            pass
        if SMTP_USER and SMTP_PASS:
            server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
        server.quit()
        logger.info("Email notification sent to %s", EMAIL_TO)
        return True
    except Exception as e:
        logger.exception("Email notify failed: %s", e)
        return False


def log_update(result: dict) -> None:
    """Log update result to history and optionally notify via Telegram/Email."""
    try:
        entry = {
            "timestamp": int(time.time()),
            "updated": bool(result.get("updated")),
            "error": result.get("error"),
            "repo_url": result.get("repo_url", REPO_URL),
            "branch": result.get("branch", REPO_BRANCH),
        }
        append_update_history(entry)

        # Notification text
        if entry["updated"]:
            text = f"VYDRA updated successfully. repo={entry['repo_url']} branch={entry['branch']}"
        else:
            text = f"VYDRA update FAILED. error={entry.get('error') or 'unknown'} repo={entry['repo_url']} branch={entry['branch']}"

        # Best-effort notifications
        try:
            send_telegram(text)
        except Exception:
            pass
        try:
            send_email(f"VYDRA update: {'SUCCESS' if entry['updated'] else 'FAILED'}", text)
        except Exception:
            pass
    except Exception:
        logger.exception("log_update failed")

# -----------------------
# Backup rotation & cleanup
# -----------------------

def _timestamp_str():
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime())


def cleanup_old_backups(backup_root: Path, max_backups: int = MAX_BACKUPS) -> None:
    """Keep only the newest `max_backups` directories under backup_root and remove older ones."""
    try:
        if not backup_root.exists():
            return
        entries = [p for p in backup_root.iterdir() if p.is_dir()]
        if not entries:
            return
        # sort by mtime desc (newest first)
        entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        keep = entries[:max_backups]
        to_remove = entries[max_backups:]
        for p in to_remove:
            try:
                shutil.rmtree(p)
                logger.info("Removed old backup: %s", p)
            except Exception:
                logger.exception("Failed to remove old backup: %s", p)
    except Exception:
        logger.exception("cleanup_old_backups failed")

# -----------------------
# Core update flow (with rotated backups)
# -----------------------

def read_version(path: Path) -> str:
    vf = path / "VERSION"
    if not vf.exists():
        return "0.0.0"
    try:
        return vf.read_text(encoding="utf-8-sig").strip()
    except Exception:
        return vf.read_text().strip()


def run_cmd(cmd, cwd=None, timeout=None, shell=True):
    logger.debug("Running command: %s (cwd=%s)", cmd, cwd)
    try:
        result = subprocess.run(cmd, cwd=cwd, shell=shell, check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, text=True)
        logger.debug("Cmd stdout: %s", result.stdout.strip())
        logger.debug("Cmd stderr: %s", result.stderr.strip())
        return True, result.stdout.strip()
    except subprocess.CalledProcessError as e:
        logger.error("Command failed: %s", e)
        logger.error("Stdout: %s", e.stdout)
        logger.error("Stderr: %s", e.stderr)
        return False, e.stderr or str(e)
    except Exception as e:
        logger.exception("Command exception: %s", e)
        return False, str(e)


def clone_or_update_repo(repo_url: str, branch: str, dest: Path, timeout: int = 120) -> bool:
    dest = Path(dest)
    if dest.exists() and (dest / ".git").exists():
        logger.info("Repo exists; fetching & resetting to origin/%s", branch)
        ok, out = run_cmd("git fetch --all", cwd=str(dest), timeout=timeout)
        if not ok:
            return False
        ok, out = run_cmd(f"git reset --hard origin/{branch}", cwd=str(dest), timeout=timeout)
        if not ok:
            return False
        ok, out = run_cmd("git clean -fd", cwd=str(dest), timeout=timeout)
        return ok
    else:
        if dest.exists():
            shutil.rmtree(dest)
        logger.info("Cloning %s (branch=%s) into %s", repo_url, branch, dest)
        ok, out = run_cmd(f"git clone --depth 1 --branch {branch} {repo_url} {str(dest)}", timeout=timeout)
        return ok


def check_for_updates_from_git(live_path: Path, repo_path: Path) -> bool:
    local_v = read_version(live_path)
    remote_v = read_version(repo_path)
    logger.info("Local version: %s - Remote version: %s", local_v, remote_v)
    return remote_v != local_v


def prepare_staging_from_repo(repo_path: Path, staging_path: Path):
    logger.info("Preparing staging area from %s ...", repo_path)
    if staging_path.exists():
        shutil.rmtree(staging_path)
    shutil.copytree(repo_path, staging_path)
    logger.info("Staging prepared at %s", staging_path)


def run_migrations(staging_path: Path, migrate_cmd: str = None) -> bool:
    if not migrate_cmd:
        logger.info("No migration command provided; skipping migrations.")
        return True
    logger.info("Running migrations: %s", migrate_cmd)
    ok, _ = run_cmd(migrate_cmd, cwd=str(staging_path), timeout=300, shell=True)
    return ok


def apply_update(staging_path: Path, live_path: Path, backups_root: Path) -> bool:
    """
    Apply update by:
      - rotating existing live into a timestamped backup under backups_root
      - copying staging -> live
    """
    logger.info("Applying update: rotate live into backups root %s and swap staging -> live", backups_root)
    try:
        backups_root.mkdir(parents=True, exist_ok=True)
        if live_path.exists():
            ts = _timestamp_str()
            dest = backups_root / f"backup_{ts}"
            logger.info("Creating timestamped backup: %s", dest)
            shutil.copytree(live_path, dest)
        # remove live and copy staging into its place
        if live_path.exists():
            shutil.rmtree(live_path)
        shutil.copytree(staging_path, live_path)
        logger.info("Swap complete")
        return True
    except Exception:
        logger.exception("apply_update failed")
        return False


def _get_most_recent_backup(backup_root: Path) -> Optional[Path]:
    try:
        if not backup_root.exists():
            return None
        dirs = [p for p in backup_root.iterdir() if p.is_dir()]
        if not dirs:
            return None
        dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return dirs[0]
    except Exception:
        logger.exception("_get_most_recent_backup failed")
        return None


def rollback(backup_root: Path, live_path: Path) -> bool:
    logger.warning("Rolling back to previous version from backups root %s...", backup_root)
    try:
        recent = _get_most_recent_backup(backup_root)
        if not recent:
            logger.error("No backup available to rollback to.")
            return False
        # remove current live and restore
        if live_path.exists():
            shutil.rmtree(live_path)
        shutil.copytree(recent, live_path)
        logger.info("Rollback complete from %s", recent)
        return True
    except Exception:
        logger.exception("Rollback failed")
        return False


def run_hook(cmd: str, timeout: int = 60):
    if not cmd:
        return True, "no-op"
    logger.info("Running hook: %s", cmd)
    ok, out = run_cmd(cmd, timeout=timeout, shell=True)
    if ok:
        logger.info("Hook succeeded: %s", cmd)
    else:
        logger.error("Hook failed: %s", cmd)
    return ok, out


def run_update_cycle_once(config: dict = None) -> dict:
    cfg = config or {}
    repo_url = cfg.get("repo_url", REPO_URL)
    branch = cfg.get("branch", REPO_BRANCH)
    live = Path(cfg.get("live_dir", str(LIVE_DIR)))
    backups_root = Path(cfg.get("backup_dir", str(BACKUP_DIR)))
    staging = Path(cfg.get("staging_dir", str(STAGING_DIR)))
    repo_clone_dir = Path(cfg.get("repo_clone_dir", str(STAGING_DIR) + "_repo"))
    migrate_cmd = cfg.get("migrate_cmd", MIGRATE_CMD)
    verify_url = cfg.get("verify_url", VERIFY_URL)
    git_timeout = int(cfg.get("git_timeout", GIT_CLONE_TIMEOUT))
    pre_cmd = cfg.get("pre_update_cmd", PRE_UPDATE_CMD)
    post_cmd = cfg.get("post_update_cmd", POST_UPDATE_CMD)
    pre_timeout = int(cfg.get("pre_timeout", PRE_TIMEOUT))
    post_timeout = int(cfg.get("post_timeout", POST_TIMEOUT))

    result = {"updated": False, "error": None, "repo_url": repo_url, "branch": branch}

    if not repo_url:
        result["error"] = "REPO_URL not configured"
        logger.error(result["error"])
        log_update(result)
        return result

    pre_ran = False

    try:
        live.mkdir(parents=True, exist_ok=True)
        backups_root.mkdir(parents=True, exist_ok=True)
        staging.parent.mkdir(parents=True, exist_ok=True)

        ok = clone_or_update_repo(repo_url, branch, repo_clone_dir, timeout=git_timeout)
        if not ok:
            raise RuntimeError("Failed to clone or update repo")

        if not check_for_updates_from_git(live, repo_clone_dir):
            logger.info("No updates found. Exiting update cycle.")
            if repo_clone_dir.exists():
                shutil.rmtree(repo_clone_dir)
            result["updated"] = False
            log_update(result)
            return result

        prepare_staging_from_repo(repo_clone_dir, staging)

        ok = run_migrations(staging, migrate_cmd)
        if not ok:
            raise RuntimeError("Migrations failed")

        # Run pre-update hook (stop service)
        if pre_cmd:
            ok, out = run_hook(pre_cmd, timeout=pre_timeout)
            if not ok:
                raise RuntimeError(f"Pre-update hook failed: {out}")
            pre_ran = True

        # Apply update (rotate backups and swap)
        ok = apply_update(staging, live, backups_root)
        if not ok:
            raise RuntimeError("Apply update failed")

        # Verify update
        ok = verify_update(live, verify_url, timeout=int(cfg.get("verify_timeout", VERIFY_TIMEOUT)))
        if not ok:
            logger.error("Verification failed; attempting rollback")
            rollback(backups_root, live)
            # After rollback, try to restart original service if we stopped it
            if pre_ran and post_cmd:
                logger.info("Attempting to run post-update hook after rollback to restart original service")
                run_hook(post_cmd, timeout=post_timeout)
            raise RuntimeError("Verification failed after update; rollback performed")

        # If verified successfully, run post-update hook to start service
        if post_cmd:
            ok, out = run_hook(post_cmd, timeout=post_timeout)
            if not ok:
                # Post-update failed: try rollback and restart original service
                logger.error("Post-update hook failed; attempting rollback and restart original service")
                rollback(backups_root, live)
                if pre_ran and post_cmd:
                    run_hook(post_cmd, timeout=post_timeout)
                raise RuntimeError(f"Post-update hook failed: {out}")

        # Cleanup repo clone & staging
        if repo_clone_dir.exists():
            shutil.rmtree(repo_clone_dir)
        if staging.exists():
            shutil.rmtree(staging)

        # After successful update: rotate/cleanup old backups & log
        try:
            cleanup_old_backups(backups_root, max_backups=MAX_BACKUPS)
        except Exception:
            logger.exception("post-update cleanup failed")

        logger.info("Update cycle completed successfully")
        result["updated"] = True
        log_update(result)
        return result

    except Exception as e:
        logger.exception("Update cycle failed: %s", e)
        result["error"] = str(e)
        try:
            # rollback to the most recent backup
            if backups_root.exists():
                rollback(backups_root, live)
            # Ensure service restarted if we stopped it
            if pre_ran and post_cmd:
                run_hook(post_cmd, timeout=post_timeout)
        except Exception as e2:
            logger.exception("Rollback/restart also failed: %s", e2)
        # record failure
        log_update(result)
        return result


_scheduler_thread = None
_stop_event = threading.Event()


def _scheduler_loop(interval_seconds: int, config: dict):
    logger.info("Update scheduler started (interval %s seconds)", interval_seconds)
    while not _stop_event.is_set():
        run_update_cycle_once(config=config)
        slept = 0
        while slept < interval_seconds and not _stop_event.is_set():
            time.sleep(1)
            slept += 1
    logger.info("Update scheduler stopped.")


def start_scheduler(interval_seconds: int = 7*24*3600, config: dict = None):
    global _scheduler_thread, _stop_event
    if _scheduler_thread and _scheduler_thread.is_alive():
        logger.warning("Scheduler already running.")
        return
    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, args=(interval_seconds, config or {}), daemon=True)
    _scheduler_thread.start()


def stop_scheduler():
    global _stop_event, _scheduler_thread
    _stop_event.set()
    if _scheduler_thread:
        _scheduler_thread.join(timeout=5)


if __name__ == "__main__":
    import json, sys
    cfg = {}
    if len(sys.argv) > 1:
        try:
            with open(sys.argv[1], "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
        except Exception as e:
            logger.error("Failed to read config file: %s", e)
    result = run_update_cycle_once(config=cfg)
    print(result)
