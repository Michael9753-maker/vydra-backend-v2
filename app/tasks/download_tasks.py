from __future__ import annotations

import logging
from typing import Any, Dict
import time
import uuid

logger = logging.getLogger(__name__)

try:
    from app.services.downloader import process_download
except Exception as exc:
    process_download = None
    logger.exception("Failed to import app.services.downloader.process_download: %s", exc)


def _call_download_engine(url: str, user_id: str) -> Any:
    """
    Compatibility layer.
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


def process_download_task(url: str, user_id: str) -> Dict[str, Any]:
    """
    Synchronous version of the Celery task
    """

    # 🔥 Generate job_id manually (since no Celery)
    job_id = str(uuid.uuid4())

    print(f"🚀 TASK STARTED | job_id={job_id}")

    if not url:
        raise ValueError("url is required")
    if not user_id:
        raise ValueError("user_id is required")

    logger.info("Starting download job=%s user_id=%s url=%s", job_id, user_id, url)

    start_time = time.time()

    try:
        print("📥 Calling download engine...")

        result = _call_download_engine(url=url, user_id=user_id)

        print("✅ Download engine returned")

        duration = round(time.time() - start_time, 2)
        print(f"⏱️ Completed in {duration}s")

        if isinstance(result, dict):
            result.setdefault("status", "SUCCESS")
            result.setdefault("job_id", job_id)
            result.setdefault("user_id", user_id)
            result.setdefault("url", url)
            result.setdefault("duration", duration)
            return result

        return {
            "status": "SUCCESS",
            "job_id": job_id,
            "user_id": user_id,
            "url": url,
            "result": result,
            "duration": duration,
        }

    except Exception as exc:
        duration = round(time.time() - start_time, 2)

        print(f"❌ TASK FAILED after {duration}s: {str(exc)}")

        logger.exception(
            "Download job failed job=%s user_id=%s url=%s",
            job_id,
            user_id,
            url,
        )

        return {
            "status": "FAILURE",
            "job_id": job_id,
            "user_id": user_id,
            "url": url,
            "error": str(exc),
            "duration": duration,
        }