from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from flask import Blueprint, current_app, jsonify, request, send_file

from app.services.downloader import process_download
from app.services.usage_service import UsageService


download_bp = Blueprint("download", __name__)

BASE_DIR = Path(__file__).resolve().parents[2]
DOWNLOAD_DIR = (BASE_DIR / "downloads").resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _normalize_path(path_value: str) -> Path | None:
    if not path_value:
        return None

    try:
        return Path(str(path_value).strip().replace("\\", "/"))
    except Exception:
        return None


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
    base = request.host_url.rstrip("/")
    return f"{base}/api/download/file/{quote(filename)}"


@download_bp.route("", methods=["POST"], strict_slashes=False)
@download_bp.route("/", methods=["POST"], strict_slashes=False)
def create_download():
    started_at = time.time()
    data = request.get_json(silent=True) or {}

    user_id = str(data.get("user_id") or "guest_user").strip() or "guest_user"
    url = str(data.get("url") or "").strip()
    job_id = str(data.get("job_id") or uuid.uuid4()).strip()

    if not url:
        return jsonify({"error": "missing_fields", "missing": ["url"]}), 400

    meta = data.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}

    is_premium = False

    try:
        allowed, used, limit, source = UsageService.check_and_increment_download(
            user_id=user_id,
            is_premium=is_premium,
        )
    except Exception as exc:
        current_app.logger.exception("UsageService failed: %s", exc)
        return jsonify(
            {
                "job_id": job_id,
                "error": "usage_check_failed",
                "debug": str(exc),
            }
        ), 500

    if not allowed:
        return jsonify(
            {
                "job_id": job_id,
                "status": "BLOCKED",
                "error": "daily_limit_reached",
                "used": used,
                "limit": limit,
                "usage_source": source,
            }
        ), 403

    try:
        result = process_download(url=url, user_id=user_id, job_id=job_id, **meta)
    except Exception as exc:
        current_app.logger.exception("Download failed: %s", exc)
        return jsonify(
            {
                "job_id": job_id,
                "status": "ERROR",
                "error": "download_failed",
                "debug": str(exc),
                "used": used,
                "limit": limit,
                "usage_source": source,
            }
        ), 500

    if not isinstance(result, dict):
        result = {
            "job_id": job_id,
            "status": "SUCCESS",
            "result": result,
        }

    status = str(result.get("status") or "SUCCESS").upper()
    file_path = str(result.get("file_path") or "")
    resolved_path = _resolve_download_path(file_path)

    if resolved_path is not None:
        result["file_path"] = str(resolved_path)
        result["file_name"] = resolved_path.name

    payload = {
        "success": status == "SUCCESS",
        "job_id": job_id,
        "status": status,
        "message": result.get("message")
        or ("Download completed successfully" if status == "SUCCESS" else "Download finished"),
        "download_url": _build_download_url(result.get("file_path", file_path))
        if (result.get("file_path") or file_path)
        else None,
        "file_name": result.get("file_name") or _extract_filename(file_path),
        "used": used,
        "limit": limit,
        "usage_source": source,
        "duration_seconds": round(time.time() - started_at, 2),
    }

    if "debug" in result:
        payload["debug"] = result.get("debug")

    if "cookie_mode" in result:
        payload["cookie_mode"] = result.get("cookie_mode")

    if "extractor" in result:
        payload["extractor"] = result.get("extractor")

    if "title" in result:
        payload["title"] = result.get("title")

    return jsonify(payload), 200


@download_bp.route("/file/<path:filename>", methods=["GET"], strict_slashes=False)
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
