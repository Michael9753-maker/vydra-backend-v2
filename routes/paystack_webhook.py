from flask import Blueprint, request, jsonify
import hmac
import hashlib
import json
import os
import requests
from datetime import datetime, timedelta
from supabase import create_client

paystack_bp = Blueprint("paystack_webhook", __name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def verify_signature(body: bytes, signature: str):
    computed = hmac.new(
        PAYSTACK_SECRET.encode(),
        body,
        hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


@paystack_bp.route("/api/paystack/webhook", methods=["POST"])
def paystack_webhook():
    raw_body = request.data
    signature = request.headers.get("x-paystack-signature")

    if not signature or not verify_signature(raw_body, signature):
        return jsonify({"error": "Invalid signature"}), 401

    payload = json.loads(raw_body)

    if payload.get("event") != "charge.success":
        return jsonify({"status": "ignored"}), 200

    data = payload["data"]
    reference = data["reference"]

    # Prevent duplicate processing
    existing = supabase.table("payments").select("*").eq("reference", reference).execute()
    if existing.data:
        return jsonify({"status": "duplicate"}), 200

    # Verify transaction with Paystack
    verify_res = requests.get(
        f"https://api.paystack.co/transaction/verify/{reference}",
        headers={"Authorization": f"Bearer {PAYSTACK_SECRET}"}
    ).json()

    if not verify_res.get("status"):
        return jsonify({"error": "Verification failed"}), 400

    trx = verify_res["data"]
    metadata = trx.get("metadata", {})

    plan_id = metadata.get("plan_id")
    duration_days = metadata.get("duration_days")
    email = trx["customer"]["email"]

    if not plan_id or not duration_days:
        return jsonify({"error": "Invalid metadata"}), 400

    # Fetch user
    user = supabase.table("users").select("*").eq("email", email).single().execute()
    if not user.data:
        return jsonify({"error": "User not found"}), 404

    user_id = user.data["id"]
    now = datetime.utcnow()

    current_expiry = user.data.get("premium_expires_at")
    if current_expiry:
        current_expiry = datetime.fromisoformat(current_expiry)

    new_expiry = (
        current_expiry + timedelta(days=duration_days)
        if current_expiry and current_expiry > now
        else now + timedelta(days=duration_days)
    )

    # Save payment
    supabase.table("payments").insert({
        "user_id": user_id,
        "reference": reference,
        "amount": trx["amount"] // 100,
        "plan_id": plan_id,
        "duration_days": duration_days,
        "paid_at": now.isoformat()
    }).execute()

    # Update premium
    supabase.table("premium_store").upsert({
        "user_id": user_id,
        "plan": plan_id,
        "started_at": now.isoformat(),
        "expires_at": new_expiry.isoformat(),
        "source": "paystack",
        "metadata": metadata
    }).execute()

    return jsonify({"status": "success"}), 200
