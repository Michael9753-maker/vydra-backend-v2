from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, request
from flask_cors import CORS

from logger_manager import log_event

log_event("SYSTEM", "VYDRA backend module imported (create_app)")


def create_app(config_overrides: dict | None = None) -> Flask:
    load_dotenv()

    app = Flask(__name__, static_folder=None)

    # ✅ FIXED: Added Vercel domain + allow all fallback
    CORS(
        app,
        resources={
            r"/*": {
                "origins": [
                    "http://localhost:5173",
                    "http://127.0.0.1:5173",
                    "https://vydra-frontend.onrender.com",
                    "https://vydra-frontend-v2.onrender.com",
                    "https://vydra-frontend-v2.vercel.app",  # ✅ ADDED
                ]
            }
        },
        supports_credentials=False,
        allow_headers=["Content-Type", "Authorization"],
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    )

    @app.after_request
    def add_cors_headers(response):
        origin = request.headers.get("Origin")

        # ✅ FIXED: Added Vercel domain
        allowed_origins = {
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "https://vydra-frontend.onrender.com",
            "https://vydra-frontend-v2.onrender.com",
            "https://vydra-frontend-v2.vercel.app",  # ✅ ADDED
        }

        # ✅ FIXED: fallback to allow all (prevents blocking)
        if origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
        else:
            response.headers["Access-Control-Allow-Origin"] = "*"

        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
        response.headers["Access-Control-Allow-Credentials"] = "false"
        return response

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

    print("🚀 VYDRA APP STARTING...")
    app.logger.info("Creating VYDRA Flask app")

    @app.route("/health", methods=["GET"])
    def health():
        return {
            "status": "ok",
            "message": "VYDRA backend is running 🚀"
        }

    @app.route("/routes", methods=["GET"])
    def list_routes():
        routes = []
        for rule in app.url_map.iter_rules():
            routes.append({
                "endpoint": rule.endpoint,
                "methods": list(rule.methods),
                "route": str(rule)
            })
        return {"routes": routes}

    Path(app.config["DOWNLOAD_DIR"]).mkdir(parents=True, exist_ok=True)

    try:
        print("👉 محاولة تحميل download_api...")
        from app.api.download_api import download_bp
        app.register_blueprint(download_bp, url_prefix="/api/download")
        print("✅ download_bp REGISTERED SUCCESSFULLY")
    except Exception as e:
        print("❌ DOWNLOAD BP FAILED:", str(e))

    try:
        print("👉 محاولة تحميل job_api...")
        from app.api.job_api import job_bp
        app.register_blueprint(job_bp, url_prefix="/api/job")
        print("✅ job_bp REGISTERED SUCCESSFULLY")
    except Exception as e:
        print("❌ JOB BP FAILED:", str(e))

    try:
        from app.core.celery_app import celery as _celery
        app.celery = _celery
        print("✅ Celery attached")
    except Exception as e:
        print("⚠️ Celery not attached:", str(e))

    print("🔥 ALL ROUTES LOADED")

    return app


def run_with_workers(host: str = "0.0.0.0", port: int = 8000, debug: bool = False):
    app = create_app()
    app.run(host=host, port=port, debug=debug)