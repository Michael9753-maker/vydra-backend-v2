from __future__ import annotations

import logging
from typing import Any, Dict

from app.core.celery_app import celery

logger = logging.getLogger(__name__)

try:
    from app.services.downloader import process_download
except Exception as exc:
    process_download = None
    logger.exception("Failed to import app.services.downloader.process_download: %s", exc)


def _call_download_engine(url: str, user_id: str) -> Any:
    """
    Compatibility layer.

    This tries a few common signatures so the task does not break while
    the downloader service is being refactored.
    """
    if process_download is None:
        raise RuntimeError("Download engine is not available")

    candidates = [
        {"user_id": user_id, "video_url": url},
        {"user_id": user_id, "url": url},
        {"url": url, "user_id": user_id},
    ]

    for kwargs in candidates:
        try:
            return process_download(**kwargs)
        except TypeError:
            pass

    for args in ((user_id, url), (url, user_id)):
        try:
            return process_download(*args)
        except TypeError:
            pass

    raise RuntimeError("process_download signature mismatch")


@celery.task(bind=True, name="app.tasks.download_tasks.process_download_task")
def process_download_task(self, url: str, user_id: str) -> Dict[str, Any]:
    job_id = getattr(self.request, "id", None)

    if not url:
        raise ValueError("url is required")
    if not user_id:
        raise ValueError("user_id is required")

    logger.info("Starting download job=%s user_id=%s url=%s", job_id, user_id, url)

    try:
        result = _call_download_engine(url=url, user_id=user_id)

        if isinstance(result, dict):
            result.setdefault("status", "completed")
            result.setdefault("job_id", job_id)
            result.setdefault("user_id", user_id)
            result.setdefault("url", url)
            return result

        return {
            "status": "completed",
            "job_id": job_id,
            "user_id": user_id,
            "url": url,
            "result": result,
        }

    except Exception as exc:
        logger.exception(
            "Download job failed job=%s user_id=%s url=%s",
            job_id,
            user_id,
            url,
        )
        raise RuntimeError(str(exc)) from exc