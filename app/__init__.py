from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask
from flask_cors import CORS

from logger_manager import log_event

log_event("SYSTEM", "VYDRA backend module imported (create_app)")


def create_app(config_overrides: dict | None = None) -> Flask:
    """
    Create and configure the Flask app.
    """
    load_dotenv()

    app = Flask(__name__, static_folder=None)
    CORS(app, resources={r"/*": {"origins": "*"}})

    base_dir = os.path.abspath(os.path.dirname(__file__))

    app.config.setdefault(
        "DOWNLOAD_DIR",
        os.getenv("DOWNLOADS_DIR", os.path.join(base_dir, "..", "downloads"))
    )
    app.config.setdefault(
        "POLL_PROGRESS_INTERVAL",
        float(os.getenv("POLL_PROGRESS_INTERVAL", 0.6))
    )
    app.config.setdefault(
        "RECENT_WINDOW_SECONDS",
        int(os.getenv("RECENT_WINDOW_SECONDS", 3 * 60 * 60))
    )
    app.config.setdefault(
        "DOWNLOAD_HISTORY_FILE",
        os.path.abspath(
            os.getenv(
                "DOWNLOAD_HISTORY_FILE",
                os.path.join(base_dir, "..", "download_history.json")
            )
        )
    )

    if isinstance(config_overrides, dict):
        app.config.update(config_overrides)

    app.logger.info("Creating VYDRA Flask app")

    # ✅ ADD HEALTH ROUTE HERE
    @app.route("/health", methods=["GET"])
    def health():
        return {
            "status": "ok",
            "message": "VYDRA backend is running 🚀"
        }

    # Ensure directories exist
    Path(app.config["DOWNLOAD_DIR"]).mkdir(parents=True, exist_ok=True)

    # Register blueprints
    try:
        from app.api.download_api import download_bp
        app.register_blueprint(download_bp, url_prefix="/api/download")
        app.logger.info("Registered blueprint: download_bp -> /api/download")
    except Exception:
        app.logger.exception("Failed to register download_bp")

    try:
        from app.api.job_api import job_bp
        app.register_blueprint(job_bp, url_prefix="/api/job")
        app.logger.info("Registered blueprint: job_bp -> /api/job")
    except Exception:
        app.logger.exception("Failed to register job_bp")

    # Attach Celery if available
    try:
        from app.core.celery_app import celery as _celery
        app.celery = _celery
        app.logger.info("Celery attached to app.")
    except Exception:
        app.logger.debug("Celery not attached.")

    return app


def run_with_workers(host: str = "0.0.0.0", port: int = 8000, debug: bool = False):
    app = create_app()
    app.run(host=host, port=port, debug=debug)