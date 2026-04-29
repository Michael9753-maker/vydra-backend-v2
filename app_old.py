# app.py (CLEANED — blueprint is canonical)
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import threading
import uuid
import time
import queue
import json
import re
from datetime import datetime, timezone, date
from pathlib import Path

from werkzeug.security import check_password_hash
from dotenv import load_dotenv
from logger_manager import log_event

# referral helpers (added)
from referral import record_referral, has_any_successful_download

# Try to import worker-manager start helper (may be provided by worker_manager.py)
try:
    from worker_manager import start_workers
except Exception:
    start_workers = None

log_event("SYSTEM", "VYDRA backend starting...")

# Load .env variables early
load_dotenv()

from database import get_default_db
from models import get_system_overview

# Download manager functions
from download_manager import (
    get_default_manager,
    probe_duration_seconds as dm_probe,
    fetch_metadata as dm_fetch_metadata,
)

# Register the download API blueprint (centralized API)
try:
    from download_api_blueprint import download_bp
except Exception:
    download_bp = None

# user_limits module (may vary across environments) - import best-effort
try:
    import user_limits_upgraded as ul
except Exception:
    ul = None

# optional global error handler register
try:
    from error_handler import register_flask_handlers
except Exception:
    register_flask_handlers = None

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

from routes.paystack_webhook import paystack_bp
app.register_blueprint(paystack_bp)


# register blueprint if available
if download_bp:
    app.register_blueprint(download_bp, url_prefix="/api")

# register centralized error handlers (best-effort)
if register_flask_handlers:
    try:
        register_flask_handlers(app)
    except Exception:
        # don't let a failure here break startup
        app.logger.exception("register_flask_handlers failed")


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ----------------------------
# Config
# ----------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DOWNLOAD_DIR = os.getenv("DOWNLOADS_DIR", os.path.join(BASE_DIR, "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))  # attempts = 1 + MAX_RETRIES
VIDEO_LENGTH_SECONDS_LIMIT = int(os.getenv("VIDEO_LENGTH_SECONDS_LIMIT", 30 * 60))  # 30 minutes default
BATCH_LIMIT = int(os.getenv("BATCH_LIMIT", 5))
POLL_PROGRESS_INTERVAL = float(os.getenv("POLL_PROGRESS_INTERVAL", 0.6))

# How long an item stays in "recent downloads" list (seconds)
RECENT_WINDOW_SECONDS = int(os.getenv("RECENT_WINDOW_SECONDS", 3 * 60 * 60))  # 3 hours

DOWNLOAD_HISTORY_FILE = os.path.join(BASE_DIR, "download_history.json")
# ensure file exists and is a list
if not os.path.exists(DOWNLOAD_HISTORY_FILE):
    with open(DOWNLOAD_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump([], f)

# For UI/demo fallback
PREMIUM_USERS = ["demo-user@example.com"]

# ----------------------------
# In-memory job & queue storage (thread-safe)
# ----------------------------
JOBS = {}  # job_id -> job dict
JOBS_LOCK = threading.Lock()
JOB_QUEUE = queue.Queue()
WORKERS = []

# ----------------------------
# Utility helpers
# ----------------------------
def gen_job_id():
    return str(uuid.uuid4())


def today_str():
    return date.today().isoformat()


def sanitize_title_for_filename(title: str, maxlen: int = 36) -> str:
    if not title:
        return "file"
    s = str(title)
    s = s.strip()
    # keep letters, numbers, dash and underscore
    s = re.sub(r"[^A-Za-z0-9_\\- ]+", "", s)
    s = re.sub(r"\s+", "-", s)
    s = s.lower()
    return s[:maxlen].strip("-") or "file"


def make_output_basename(orig_filename: str | None, ext: str | None = None) -> str:
    short = uuid.uuid4().hex[:6]
    slug = sanitize_title_for_filename(orig_filename or "vydra")
    ext = (ext or "mp4").lstrip(".")
    return f"vydra_{short}_{slug}.{ext}"


def is_premium_user(user_id: str) -> bool:
    try:
        # user_limits might name it differently; use best-effort call
        if ul is not None and hasattr(ul, "_is_premium"):
            return bool(ul._is_premium(user_id))
        if ul is not None and hasattr(ul, "is_premium"):
            return bool(ul.is_premium(user_id))
    except Exception:
        pass
    return bool(user_id and user_id in PREMIUM_USERS)


def determine_download_mode(user_id: str, requested_quality):
    if not requested_quality:
        return "speed"
    q = requested_quality
    try:
        qn = int(str(q).replace("k", "000").replace("K", "000"))
    except Exception:
        qn = None
    if q in (360, "360", "360p") or qn == 360:
        return "balanced"
    if q in (480, "480", "480p") or qn == 480:
        return "balanced"
    if q in (720, "720", "720p") or qn == 720:
        return "balanced"
    premium_tokens = {"1080", "1080p", 1080, "2K", "2k", "2000", 2000, "4K", "4k", 4000}
    if str(q) in set(map(str, premium_tokens)) or (qn in (1080, 2000, 4000)):
        if is_premium_user(user_id):
            return "quality"
        else:
            return "balanced"
    return "speed"


def clamp_quality_for_user(user_id: str, requested_quality):
    if not requested_quality:
        return None
    q = requested_quality
    try:
        qn = int(str(q).replace("k", "000").replace("K", "000"))
    except Exception:
        qn = None
    premium_allowed = is_premium_user(user_id)
    if not premium_allowed and (qn in (1080, 2000, 4000) or str(q).lower() in ("1080", "1080p", "2k", "4k")):
        return 720
    return q


# ----------------------------
# Job helpers
# ----------------------------
def make_job_record(job_id: str, user_id: str, url: str, meta: dict | None = None):
    now = datetime.now(timezone.utc).isoformat()
    job = {
        "job_id": job_id,
        "user_id": user_id,
        "url": url,
        "status": "queued",
        "attempts": 0,
        "retries_left": MAX_RETRIES,
        "progress": {"percent": 0},
        "file": None,
        "error": None,
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "meta": meta or {}
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    return job


def update_job(job_id: str, patch: dict):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(patch)
            # return a shallow copy to avoid external accidental mutation
            return JOBS[job_id].copy()
    return None


# ----------------------------
# Recent downloads persistence (for frontend recent card)
# ----------------------------
_HISTORY_LOCK = threading.Lock()


def _load_history() -> list:
    try:
        with _HISTORY_LOCK:
            with open(DOWNLOAD_HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception:
        pass
    return []


def _save_history(history_list: list):
    try:
        with _HISTORY_LOCK:
            with open(DOWNLOAD_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history_list, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def save_history_entry(entry: dict):
    hist = _load_history()
    hist.insert(0, entry)  # newest first
    # cap history size to avoid bloat (e.g., 500)
    hist = hist[:500]
    _save_history(hist)


def record_download_history(job_id: str, url: str, user_id: str, status: str, filename: str | None,
                            started_at: str | None, finished_at: str | None, error: str | None):
    """
    Compatibility shim used by download_api_blueprint.
    Stores a minimal history entry (best-effort).
    """
    try:
        entry = {
            "user_id": user_id or "guest",
            "job_id": job_id,
            "title": url,
            "file": filename,
            "thumbnail": None,
            "caption": None,
            "hashtags": [],
            "created_at": finished_at or started_at or datetime.now(timezone.utc).isoformat()
        }
        save_history_entry(entry)
    except Exception:
        app.logger.exception("record_download_history failed for %s", job_id)


def extract_hashtags(text: str | None) -> list:
    if not text:
        return []
    tags = re.findall(r"#\w+", text)
    return tags


def recent_downloads_for_user(user_id: str) -> list:
    now = datetime.now(timezone.utc)
    results = []
    for e in _load_history():
        if e.get("user_id") != user_id:
            continue
        try:
            created = datetime.fromisoformat(e.get("created_at"))
        except Exception:
            continue
        if (now - created).total_seconds() <= RECENT_WINDOW_SECONDS:
            # compute age_seconds
            age_seconds = int((now - created).total_seconds())
            item = {
                "title": e.get("title"),
                "file": e.get("file"),
                "thumbnail": e.get("thumbnail"),
                "hashtags": e.get("hashtags", []),
                "caption": e.get("caption"),
                "created_at": e.get("created_at"),
                "age_seconds": age_seconds
            }
            results.append(item)
    return results


# ----------------------------
# Worker - consumes JOB_QUEUE and uses manager
# ----------------------------
def worker_main(worker_index: int):
    mgr = get_default_manager()
    app.logger.info("Worker %d started", worker_index)

    while True:
        job_id = JOB_QUEUE.get()
        job = None
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if not job:
            JOB_QUEUE.task_done()
            continue

        # bookkeeping before attempt
        job_update = {
            "status": "downloading",
            "started_at": datetime.now(timezone.utc).isoformat(),
            # attempts incremented below
        }
        update_job(job_id, job_update)

        # increment attempts safely
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["attempts"] = JOBS[job_id].get("attempts", 0) + 1

        user_id = job.get("user_id")
        url = job.get("url")
        meta = job.get("meta", {}) or {}
        requested_quality = meta.get("quality")
        mode = meta.get("mode")
        audio_only = meta.get("audio_only", False)
        audio_format = meta.get("audio_format")
        enhancement = meta.get("enhancement")

        success = False
        last_err = None

        for attempt in range(1, 1 + MAX_RETRIES + 1):
            stop_monitor = threading.Event()

            def monitor_loop():
                # publish current progress periodically so frontend polls only job_status
                while not stop_monitor.is_set():
                    try:
                        p = {}
                        try:
                            p = mgr.get_progress(job_id) or {}
                        except Exception:
                            p = {}
                        # read job snapshot under lock to avoid races
                        with JOBS_LOCK:
                            job_snapshot = JOBS.get(job_id, {}).copy()
                        status = p.get("status") or job_snapshot.get("status")
                        percent = p.get("percent")
                        if isinstance(percent, (int, float)):
                            percent = int(round(percent))
                        else:
                            percent = int(job_snapshot.get("progress", {}).get("percent", 0) or 0)
                        file_name = job_snapshot.get("file")
                        update_job(job_id, {"status": status, "progress": {"percent": percent or 0}, "file": file_name})
                    except Exception:
                        pass
                    time.sleep(POLL_PROGRESS_INTERVAL)

            mon = threading.Thread(target=monitor_loop, daemon=True)
            mon.start()

            try:
                download_kwargs = {
                    "job_id": job_id,
                    "timeout": None,
                    "enforce_max_duration_seconds": VIDEO_LENGTH_SECONDS_LIMIT if not audio_only else None,
                    "mode": mode,
                    "quality": requested_quality,
                    "audio_only": bool(audio_only),
                    "audio_format": audio_format,
                    "enhancement": enhancement,
                }
                download_kwargs = {k: v for k, v in download_kwargs.items() if v is not None}

                try:
                    filepath = mgr.download(url, **download_kwargs)
                except TypeError:
                    # fallback for managers that expect positional args
                    filepath = mgr.download(url, job_id=job_id, timeout=None, enforce_max_duration_seconds=(VIDEO_LENGTH_SECONDS_LIMIT if not audio_only else None))

                # manager.download finished: obtain additional metadata from manager or by fetching
                pfinal = {}
                try:
                    pfinal = mgr.get_progress(job_id) or {}
                except Exception:
                    pfinal = {}
                title = pfinal.get("title")
                thumbnail = pfinal.get("thumbnail")
                # attempt to fetch description (caption) and hashtags from metadata if not present
                desc = None
                try:
                    meta_full = dm_fetch_metadata(url, timeout=3)
                    if isinstance(meta_full, dict):
                        desc = meta_full.get("description") or meta_full.get("alt_description") or None
                        if not title:
                            title = meta_full.get("title")
                        if not thumbnail:
                            thumbnail = meta_full.get("thumbnail")
                except Exception:
                    desc = None

                hashtags = extract_hashtags(desc)

                # determine extension
                if audio_only:
                    ext = audio_format or os.path.splitext(filepath)[1].lstrip('.') or "mp3"
                else:
                    ext = os.path.splitext(filepath)[1].lstrip('.') or "mp4"

                nice_basename = make_output_basename(title or os.path.basename(filepath), ext)
                nice_path = os.path.join(DOWNLOAD_DIR, nice_basename)

                # ensure atomic move/rename into downloads folder
                try:
                    if os.path.abspath(os.path.dirname(filepath)) != os.path.abspath(DOWNLOAD_DIR):
                        os.replace(filepath, nice_path)
                    else:
                        # same folder => just rename if needed
                        if os.path.basename(filepath) != nice_basename:
                            os.replace(filepath, nice_path)
                        else:
                            nice_path = filepath
                except Exception:
                    # fallback: leave file as is
                    if os.path.exists(filepath):
                        nice_path = filepath
                        nice_basename = os.path.basename(filepath)
                    else:
                        raise

                # mark finished
                finished_at = datetime.now(timezone.utc).isoformat()
                update_job(job_id, {
                    "status": "finished",
                    "file": os.path.basename(nice_path),
                    "progress": {"percent": 100},
                    "finished_at": finished_at
                })

                # ----------------------------
                # REFERRAL CREDIT (FIRST SUCCESSFUL DOWNLOAD ONLY)
                # ----------------------------
                try:
                    referrer_id = meta.get("referrer_id")
                    if referrer_id and user_id:
                        db = get_default_db()
                        db.connect()
                        try:
                            # only credit if this user has no previous successful download recorded in DB
                            if not has_any_successful_download(db, user_id):
                                record_referral(db, referrer_id, user_id)
                        finally:
                            try:
                                db.close()
                            except Exception:
                                pass
                except Exception:
                    # Log but do not fail the job if referral DB actions fail
                    app.logger.exception("Referral crediting failed for job %s (referrer: %s)", job_id, meta.get("referrer_id"))

                # save history entry for recent downloads (frontend uses this)
                entry = {
                    "user_id": user_id,
                    "job_id": job_id,
                    "title": title or os.path.basename(nice_path),
                    "file": os.path.basename(nice_path),
                    "thumbnail": thumbnail,
                    "caption": (desc or "")[:400],  # cap caption storage
                    "hashtags": hashtags,
                    "created_at": finished_at
                }

                try:
                    save_history_entry(entry)
                except Exception:
                    pass

                # bookkeeping (user limits)
                try:
                    if ul is not None and hasattr(ul, "mark_successful_download"):
                        ul.mark_successful_download(user_id, count=1)
                    if ul is not None and hasattr(ul, "release_reserved_slots"):
                        ul.release_reserved_slots(user_id, count=1)
                except Exception:
                    # best-effort only
                    pass

                success = True
                break

            except Exception as e:
                last_err = str(e)
                app.logger.exception("Job %s attempt %d failed: %s", job_id, attempt, e)
                # set status to retrying if we still have retries left
                status_val = "retrying" if attempt <= MAX_RETRIES else "error"
                update_job(job_id, {"status": status_val, "error": last_err})
                if attempt <= MAX_RETRIES:
                    time.sleep(3 if attempt == 1 else 10)
                else:
                    break

            finally:
                stop_monitor.set()
                try:
                    mon.join(timeout=1)
                except Exception:
                    pass

        if not success:
            update_job(job_id, {"status": "error", "error": last_err, "finished_at": datetime.now(timezone.utc).isoformat()})
            try:
                if ul is not None and hasattr(ul, "release_reserved_slots"):
                    ul.release_reserved_slots(user_id, count=1)
            except Exception:
                pass

        JOB_QUEUE.task_done()


# helper to start worker threads (call from __main__ block to avoid double-starts when using flask reloader)
def start_worker_threads():
    global WORKERS
    if WORKERS:
        return
    for i in range(MAX_CONCURRENT_DOWNLOADS):
        t = threading.Thread(target=worker_main, args=(i + 1,), daemon=True)
        t.start()
        WORKERS.append(t)
    app.logger.info("Started %d worker threads", len(WORKERS))


# ----------------------------
# Job serializer (canonical)
# ----------------------------
def _serialize_job_for_client(job: dict) -> dict:
    # return a trimmed/serializable view for clients
    if not job:
        return {}
    view = {
        "job_id": job.get("job_id"),
        "user_id": job.get("user_id"),
        "url": job.get("url"),
        "status": job.get("status"),
        "progress": job.get("progress", {"percent": 0}),
        "file": job.get("file"),
        "error": job.get("error"),
        "attempts": job.get("attempts", 0),
        "retries_left": job.get("retries_left"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "meta": job.get("meta", {}),
    }
    return view


# ----------------------------
# New: Download job endpoint
# ----------------------------
@app.route("/api/download", methods=["POST"])
def api_download():
    """
    Create a download job and enqueue it.
    Expected JSON body:
    {
      "url": "...",
      "user_id": "optional-user-id",
      "meta": { "quality": 720, "audio_only": false, "enhancement": "smart", ... }
    }
    Returns: { "job_id": "..." } (202)
    """
    try:
        if request.is_json:
            payload = request.get_json()
        else:
            # support form-encoded as fallback
            payload = request.form.to_dict() or {}
    except Exception:
        return jsonify({"error": "invalid request body"}), 400

    url = (payload.get("url") or "").strip()
    user_id = payload.get("user_id") or payload.get("email") or None
    meta = payload.get("meta") or {}

    # if meta is a JSON string (sent from some clients), try to parse
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}

    if not url:
        return jsonify({"error": "missing url"}), 400

    # Basic validation & clamp quality
    if "quality" in meta:
        try:
            meta["quality"] = clamp_quality_for_user(user_id, meta["quality"])
        except Exception:
            pass

    # Create job
    job_id = gen_job_id()
    job = make_job_record(job_id, user_id, url, meta=meta)

    # Optionally reserve slots / check limits using user_limits module (best-effort)
    try:
        if ul is not None and hasattr(ul, "reserve_slot_for_user"):
            # a no-raise attempt to reserve a slot if your user_limits supports that
            try:
                ok = ul.reserve_slot_for_user(user_id, count=1)
                if not ok:
                    # mark job as rejected and return a 429-like response
                    update_job(job_id, {"status": "error", "error": "quota_exceeded"})
                    return jsonify({"error": "quota_exceeded"}), 429
            except Exception:
                pass
    except Exception:
        pass

    # Enqueue
    try:
        JOB_QUEUE.put(job_id)
    except Exception as e:
        app.logger.exception("Failed to enqueue job %s: %s", job_id, e)
        update_job(job_id, {"status": "error", "error": "enqueue_failed"})
        return jsonify({"error": "enqueue_failed"}), 500

    # Return accepted with job id and position
    try:
        position = JOB_QUEUE.qsize()
    except Exception:
        position = None

    return jsonify({"job_id": job_id, "position": position}), 202


# ----------------------------
# New: Job status endpoint
# ----------------------------
@app.route("/api/job_status/<job_id>", methods=["GET"])
def api_job_status(job_id):
    if not job_id:
        return jsonify({"error": "missing job_id"}), 400
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "not_found"}), 404
        view = _serialize_job_for_client(job)
    # attach manager progress if possible
    try:
        mgr = get_default_manager()
        p = mgr.get_progress(job_id) or {}
        view["manager_progress"] = p
    except Exception:
        view["manager_progress"] = {}
    return jsonify({"job": view}), 200


# ----------------------------
# Remaining Endpoints (non-download-specific)
# ----------------------------
@app.route("/api/premium_status/<email>", methods=["GET"])
def premium_status(email):
    try:
        is_prem = False
        if ul is not None and hasattr(ul, "_is_premium"):
            is_prem = bool(ul._is_premium(email))
        elif ul is not None and hasattr(ul, "is_premium"):
            is_prem = bool(ul.is_premium(email))
        else:
            is_prem = (email in PREMIUM_USERS)
    except Exception:
        is_prem = (email in PREMIUM_USERS)
    return jsonify({"email": email, "premium": is_prem})


@app.route("/api/limit_status/<user_id>", methods=["GET"])
def limit_status(user_id):
    if not user_id:
        return jsonify({"error": "missing user_id"}), 400
    try:
        rec = ul.get_usage(user_id) if ul is not None else None
    except Exception:
        rec = None
    if not rec:
        rec = {"date": today_str(), "success": 0, "reserved": 0, "ai": 0}
    is_prem = is_premium_user(user_id)
    download_limit = 50 if is_prem else 30
    ai_limit = 10 if is_prem else 0
    used = int(rec.get("success", 0)) + int(rec.get("reserved", 0))
    remaining = max(0, download_limit - used)
    ai_used = int(rec.get("ai", 0))
    ai_remaining = max(0, ai_limit - ai_used)
    return jsonify({
        "user_id": user_id,
        "is_premium": bool(is_prem),
        "download_limit": download_limit,
        "used": used,
        "remaining": remaining,
        "ai_limit": ai_limit,
        "ai_used": ai_used,
        "ai_remaining": ai_remaining,
        "date": rec.get("date")
    })


@app.route("/api/current_progress/<user_id>", methods=["GET"])
def current_progress(user_id):
    """
    Return the most recent active job (queued/downloading/retrying) for the user.
    Useful for implementing a single progress bar in the frontend.
    """
    candidate = None
    with JOBS_LOCK:
        # iterate jobs newest-first by created_at
        jobs = list(JOBS.values())
    jobs.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    for j in jobs:
        if j.get("user_id") != user_id:
            continue
        if j.get("status") in ("downloading", "queued", "retrying"):
            candidate = j
            break
    if not candidate:
        return jsonify({"active": None})
    resp = dict(candidate)
    try:
        created = datetime.fromisoformat(resp.get("created_at"))
        resp["age_seconds"] = int((datetime.now(timezone.utc) - created).total_seconds())
    except Exception:
        resp["age_seconds"] = 0
    # attach latest manager progress if available
    mgr = None
    try:
        mgr = get_default_manager()
        p = {}
        try:
            p = mgr.get_progress(candidate["job_id"]) or {}
        except Exception:
            p = {}
        resp["manager_progress"] = p
    except Exception:
        resp["manager_progress"] = {}
    return jsonify({"active": resp})


@app.route("/api/recent_downloads/<user_id>", methods=["GET"])
def recent_downloads(user_id):
    """
    Return recent downloads for a user within RECENT_WINDOW_SECONDS.
    Frontend can display thumbnail, caption, hashtags, and file name.
    """
    if not user_id:
        return jsonify({"error": "missing user_id"}), 400
    results = recent_downloads_for_user(user_id)
    return jsonify({"user_id": user_id, "items": results})


@app.route("/downloads/<path:filename>", methods=["GET"])
def serve_file(filename):
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(filepath):
        return "File not found", 404
    return send_file(filepath, as_attachment=True)


# ============================================================
# ADMIN API ENDPOINT (Password + Email protected)
# ============================================================
from admin_auth import verify_admin  # new helper
from models import get_system_overview
from database import get_default_db


@app.route("/admin/system-overview", methods=["POST"])
def admin_system_overview():
    """
    Secure admin-only endpoint.
    Requires JSON:
    {
        "email": "...",
        "password": "..."
    }
    """
    try:
        data = request.json or {}
        email = data.get("email", "").strip()
        password = data.get("password", "").strip()

        if not email or not password:
            return jsonify({
                "status": "error",
                "message": "Email and password are required"
            }), 400

        # Validate admin credentials
        if not verify_admin(email, password):
            return jsonify({
                "status": "error",
                "message": "Unauthorized"
            }), 401

        # Fetch system stats
        db = get_default_db()
        db.connect()

        try:
            overview = get_system_overview(db)
        finally:
            db.close()

        return jsonify({
            "status": "success",
            "data": overview
        }), 200

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"Internal server error: {str(e)}"
        }), 500


@app.post("/admin/logs")
def admin_logs():
    data = request.get_json(force=True)
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()
    limit = int(data.get("limit", 200))

    # Use boolean verify_admin from admin_auth
    from admin_auth import verify_admin
    from admin_stats import get_recent_logs

    if not verify_admin(email, password):
        return jsonify({"error": "Unauthorized"}), 403

    lines = get_recent_logs(limit)
    return jsonify({"status": "success", "data": lines}), 200


# ----------------------------
# --------------------------------------------
# WORKER SYSTEM STARTUP
# --------------------------------------------
try:
    # Try external worker pool (worker_manager.py)
    from worker_manager import start_workers
except Exception:
    start_workers = None


# --------------------------------------------
# APP ENTRYPOINT
# --------------------------------------------
if __name__ == "__main__":
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    # Start workers (external preferred)
    try:
        if callable(start_workers):
            start_workers()
        else:
            start_worker_threads()  # legacy fallback
    except Exception:
        # If external pool fails, fallback to legacy local workers
        try:
            start_worker_threads()
        except Exception:
            app.logger.exception("Failed to start worker threads")

    # Boot the API
    app.run(host="0.0.0.0", port=8000)
