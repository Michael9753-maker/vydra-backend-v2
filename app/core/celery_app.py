from celery import Celery


def make_celery():
    celery = Celery(
        "vydra",
        broker="redis://localhost:6379/0",
        backend="redis://localhost:6379/0",
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
