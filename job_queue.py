# job_queue.py

import queue
import threading
import time
import uuid

# -------------------------------------------------
# GLOBAL QUEUE (stores job_ids only)
# -------------------------------------------------
JOB_QUEUE = queue.Queue()

# -------------------------------------------------
# GLOBAL JOB STORAGE
# -------------------------------------------------
JOBS = {}
JOBS_LOCK = threading.Lock()


# -------------------------------------------------
# Job helpers
# -------------------------------------------------
def gen_job_id():
    """Generate a unique job ID."""
    return uuid.uuid4().hex[:16]


def make_job_record(job_id, user_id, url, meta=None):
    """Create a new job entry and save it to JOBS."""
    if meta is None:
        meta = {}

    job = {
        "job_id": job_id,
        "user_id": user_id,
        "url": url,
        "meta": meta,
        "status": "queued",
        "result": None,
        "error": None,
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None
    }

    with JOBS_LOCK:
        JOBS[job_id] = job

    return job


def update_job(job_id, fields: dict):
    """Safely modify job entry."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return False
        job.update(fields)
        return True


# -------------------------------------------------
# Serialization for API
# -------------------------------------------------
def _serialize_job_for_client(job: dict):
    """Return safe JSON view of the job."""
    return {
        "job_id": job["job_id"],
        "url": job["url"],
        "user_id": job["user_id"],
        "meta": job.get("meta", {}),
        "status": job["status"],
        "result": job.get("result"),
        "error": job.get("error"),
        "created_at": job["created_at"],
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }
