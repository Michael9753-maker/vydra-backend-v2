# workers/tasks.py
"""
Celery tasks for VYDRA.

This is a skeleton to show how you'll migrate worker logic into Celery tasks.
You already asked to introduce Redis + Celery — when ready, create core/celery_app.py
that constructs a Celery() instance, then import it here and use @celery.task decorators.

Until you wire Celery, this module acts as documentation and a place to move logic.
"""

try:
    # expected later: from core.celery_app import celery
    from core.celery_app import celery  # noqa: F401
except Exception:
    celery = None

from download_manager import get_default_manager

def _get_manager():
    try:
        return get_default_manager()
    except Exception:
        return None

# Example task: perform a download (executed by Celery worker)
if celery is not None:
    @celery.task(name="vydra.download_task")
    def download_task(job_id: str, url: str, user_id: str = None, meta: dict | None = None):
        mgr = _get_manager()
        if mgr is None:
            raise RuntimeError("download manager not available")
        # call manager.download synchronously — it will run inside the Celery worker process
        return mgr.download(url, job_id=job_id, **(meta or {}))
else:
    # fallback no-op that raises to remind you to configure Celery
    def download_task(*args, **kwargs):
        raise RuntimeError("Celery not configured. Implement core.celery_app and set up broker/backend.")
