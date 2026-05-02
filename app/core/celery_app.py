import os

# 🔒 Toggle Celery ON/OFF
USE_CELERY = False


if USE_CELERY:
    from celery import Celery

    def make_celery():
        redis_url = os.getenv("REDIS_URL")

        if not redis_url:
            raise ValueError("REDIS_URL is not set in environment variables")

        if not redis_url.endswith("/0"):
            redis_url = redis_url + "/0"

        celery = Celery(
            "vydra",
            broker=redis_url,
            backend=redis_url,
            include=["app.tasks.download_tasks"],
        )

        celery.conf.update(
            task_serializer="json",
            result_serializer="json",
            accept_content=["json"],
            timezone="UTC",
            enable_utc=True,
            task_track_started=True,
            task_time_limit=3600,
        )

        return celery

    celery = make_celery()

else:
    # 🚫 Celery disabled — placeholder so imports don't break
    celery = None