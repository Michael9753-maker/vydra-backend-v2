from flask import Blueprint, jsonify, request
from app.core.celery_app import celery
import uuid

job_bp = Blueprint("job", __name__)


@job_bp.route("/<job_id>", methods=["GET", "OPTIONS"])
def get_job(job_id):
    # Handle CORS preflight
    if request.method == "OPTIONS":
        return "", 200

    # Validate job_id
    try:
        uuid.UUID(job_id)
    except ValueError:
        return jsonify({"error": "invalid job id"}), 400

    task = celery.AsyncResult(job_id)
    state = task.state

    if state in ["PENDING", "RECEIVED"]:
        return jsonify({"status": "pending"}), 200

    if state in ["STARTED", "RETRY"]:
        return jsonify({"status": "processing"}), 200

    if state == "SUCCESS":
        return jsonify({
            "status": "completed",
            "result": task.result
        }), 200

    if state == "FAILURE":
        return jsonify({
            "status": "failed",
            "error": str(task.result)
        }), 500

    if state == "REVOKED":
        return jsonify({"status": "cancelled"}), 200

    return jsonify({"status": "unknown"}), 200