from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from flask import Blueprint, request, jsonify, send_file, current_app

from app.services.usage_service import UsageService
from app.tasks.download_tasks import process_download_task

download_bp = Blueprint("download", __name__)

BASE_DIR = Path(__file__).resolve().parents[2]
DOWNLOAD_DIR = (BASE_DIR / "downloads").resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _normalize_path(path_value: str) -> Path | None:
    if not path_value:
        return None

    try:
        candidate = Path(str(path_value).strip().replace("\\", "/"))
    except Exception:
        return None

    return candidate


def _is_within_download_dir(path_obj: Path) -> bool:
    try:
        path_obj.resolve().relative_to(DOWNLOAD_DIR)
        return True
    except Exception:
        return False


def _extract_filename(file_path: str) -> str:
    if not file_path:
        return ""
    normalized = str(file_path).replace("\\", "/").strip()
    return normalized.split("/")[-1]


def _resolve_download_path(file_path: str) -> Path | None:
    if not file_path:
        return None

    raw = str(file_path).strip().replace("\\", "/")
    path_obj = _normalize_path(raw)

    if path_obj is None:
        return None

    try:
        if path_obj.is_absolute() and path_obj.exists():
            return path_obj.resolve()
    except Exception:
        pass

    try:
        relative_candidate = (DOWNLOAD_DIR / path_obj).resolve()
        if relative_candidate.exists() and _is_within_download_dir(relative_candidate):
            return relative_candidate
    except Exception:
        pass

    filename = _extract_filename(raw)
    if filename:
        candidate = (DOWNLOAD_DIR / filename).resolve()
        if candidate.exists():
            return candidate

    if filename:
        for root, _dirs, files in os.walk(DOWNLOAD_DIR):
            if filename in files:
                candidate = Path(root) / filename
                if candidate.exists():
                    return candidate.resolve()

    return None


def _build_download_url(file_path: str) -> str | None:
    filename = _extract_filename(file_path)
    if not filename:
        return None
    return f"/api/download/file/{quote(filename)}"


@download_bp.route("/", methods=["POST", "OPTIONS"])
def download():
    if request.method == "OPTIONS":
        return "", 200
    data = request.get_json(silent=True)

    if not data:
        return jsonify({"error": "invalid_json"}), 400

    user_id = data.get("user_id", "guest_user")
    url = data.get("url")

    if not user_id or not url:
        return jsonify({"error": "missing_fields"}), 400

    is_premium = False

    # ✅ Check usage limits
    try:
        allowed, used, limit, source = UsageService.check_and_increment_download(
            user_id=user_id,
            is_premium=is_premium,
        )
    except Exception as exc:
        current_app.logger.exception("UsageService failed: %s", exc)
        return jsonify({
            "error": "usage_check_failed",
            "debug": str(exc)
        }), 500

    if not allowed:
        return jsonify(
            {
                "error": "daily_limit_reached",
                "used": used,
                "limit": limit,
                "usage_source": source
            }
        ), 403

    # 🚀 Run download directly (NO CELERY)
    try:
        result = process_download_task(url, user_id)
    except Exception as exc:
        current_app.logger.exception("Download failed: %s", exc)
        return jsonify({"error": "download_failed"}), 500

    # 📁 Resolve file safely
    file_path = result.get("file_path", "")
    resolved_path = _resolve_download_path(file_path)

    if resolved_path is not None:
        result["file_path"] = str(resolved_path)
        result["file_name"] = resolved_path.name

    return jsonify(
        {
            "status": result.get("status", "SUCCESS"),
            "result": result,
            "download_url": _build_download_url(
                result.get("file_path", file_path)
            ) if (result.get("file_path") or file_path) else None,
            "used": used,
            "limit": limit,
            "usage_source": source
        }
    ), 200


@download_bp.route("/file/<path:filename>", methods=["GET"])
def download_file(filename):
    if not filename:
        return jsonify(
            {
                "message": "file_not_found",
                "error": "missing filename",
            }
        ), 404

    try:
        safe_name = Path(str(filename).replace("\\", "/")).name
        candidate = (DOWNLOAD_DIR / safe_name).resolve()

        if candidate.exists() and _is_within_download_dir(candidate):
            return send_file(
                candidate,
                as_attachment=True,
                download_name=candidate.name,
            )

        # 🔍 fallback search
        for root, _dirs, files in os.walk(DOWNLOAD_DIR):
            if safe_name in files:
                found = (Path(root) / safe_name).resolve()
                if found.exists() and _is_within_download_dir(found):
                    return send_file(
                        found,
                        as_attachment=True,
                        download_name=found.name,
                    )

        return jsonify(
            {
                "message": "file_not_found",
                "error": "requested file does not exist",
            }
        ), 404

    except Exception as exc:
        current_app.logger.exception("File download failed: %s", exc)
        return jsonify(
            {
                "message": "file_not_found",
                "error": str(exc),
            }
        ), 404