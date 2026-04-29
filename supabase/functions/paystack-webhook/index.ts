// supabase/functions/paystack-webhook/index.ts
/**
 * Paystack webhook — Deno Edge Function
 *
 * Required Function secrets (set in Supabase):
 * - SUPABASE_URL
 * - SUPABASE_SERVICE_ROLE_KEY
 * - PAYSTACK_SECRET_KEY   (sk_test_... or sk_live_...)
 *
 * Behaviour:
 * - Reads raw ArrayBuffer for exact bytes (critical for signature verification)
 * - Verifies signature using Web Crypto HMAC-SHA512
 * - If signature missing/mismatch: attempts Paystack /transaction/verify/{reference} fallback
 * - Idempotent: checks `paystack_events` (preferred) then `payments` to avoid double-processing
 * - Upserts `premium_store` with expiry stacking
 *
 * Notes:
 * - Keep `verify_jwt=false` for this function in config/dashboard.
 * - After full testing, remove or reduce debug logs.
 */

import { serve } from "https://deno.land/std@0.224.0/http/server.ts";
import { createClient } from "@supabase/supabase-js";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const PAYSTACK_SECRET = Deno.env.get("PAYSTACK_SECRET_KEY") ?? "";

if (!SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY || !PAYSTACK_SECRET) {
  console.error("Missing required env vars: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, PAYSTACK_SECRET_KEY");
}

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

/** compute hex HMAC-SHA512 of Uint8Array using Web Crypto */
async function computeHmacSha512Hex(secret: string, bytes: Uint8Array) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-512" },
    false,
    ["sign"]
  );
  const sigBuf = await crypto.subtle.sign("HMAC", key, bytes);
  return Array.from(new Uint8Array(sigBuf)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** verify transaction with Paystack API */
async function verifyTransactionWithPaystack(reference: string) {
  const res = await fetch(`https://api.paystack.co/transaction/verify/${encodeURIComponent(reference)}`, {
    headers: { Authorization: `Bearer ${PAYSTACK_SECRET}`, Accept: "application/json" },
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Paystack verify failed HTTP ${res.status}: ${body}`);
  }
  return await res.json();
}

/** process a verified transaction object (trxData should resemble Paystack transaction data) */
async function processTransaction(trxData: any) {
  const reference = trxData.reference;
  const metadata = trxData.metadata || {};
  const user_id = metadata.user_id;
  const duration_days = Number(metadata.duration_days || metadata.duration || 0);
  const plan = metadata.plan_id || metadata.plan || null;
  const amountNaira = Number(trxData.amount || 0) / 100;

  if (!reference || !user_id || !duration_days || duration_days <= 0) {
    console.warn("processTransaction: missing required fields", { reference, user_id, duration_days });
    return;
  }

  // 1) Try to use paystack_events table for idempotency if available
  try {
    // Insert into paystack_events (this will fail if table missing, handled below)
    const { data: insertEvt, error: insertEvtErr } = await supabase
      .from("paystack_events")
      .insert({ reference, processed_at: new Date().toISOString() }, { returning: "minimal" });

    // If insertEvtErr exists and indicates constraint (duplicate) then already processed
    if (insertEvtErr) {
      // If duplicate key or other error, fallback to checking payments table
      console.info("paystack_events insert error (will fallback to payments check):", insertEvtErr.message ?? insertEvtErr);
    } else {
      // Insert succeeded — safe to process
      console.info("paystack_events: recorded reference", reference);
    }
  } catch (err) {
    // Table probably doesn't exist or some other error — we'll fallback to payments table check
    console.info("paystack_events insert threw; falling back to payments check", err);
  }

  // 2) Idempotency fallback: check payments table for existing reference
  const { data: existingPayments, error: existingErr } = await supabase
    .from("payments")
    .select("id")
    .eq("reference", reference)
    .limit(1);

  if (existingErr) {
    console.error("Error checking payments table for idempotency:", existingErr);
    // don't throw — just stop processing for safety
    return;
  }
  if (existingPayments && Array.isArray(existingPayments) && existingPayments.length > 0) {
    console.info("Payment already processed (payments table):", reference);
    return;
  }

  // 3) Read user's existing premium expiry and compute stacking
  const { data: premiumRow, error: pErr } = await supabase
    .from("premium_store")
    .select("expires_at")
    .eq("user_id", user_id)
    .limit(1);

  if (pErr) {
    console.error("Error reading premium_store:", pErr);
    // continue but we'll use current date as base
  }

  const now = new Date();
  let baseDate = now;
  if (premiumRow && Array.isArray(premiumRow) && premiumRow.length > 0) {
    const row = premiumRow[0];
    if (row?.expires_at) {
      const existingExpiry = new Date(row.expires_at);
      if (existingExpiry > now) baseDate = existingExpiry;
    }
  }

  const newExpiry = new Date(baseDate.getTime() + duration_days * 24 * 60 * 60 * 1000);

  // 4) Insert into payments (audit)
  const { error: insertPaymentErr } = await supabase.from("payments").insert({
    reference,
    user_id,
    amount: amountNaira,
    plan,
    duration_days,
    paid_at: now.toISOString(),
    source: "paystack",
    metadata: trxData.metadata || {},
  });

  if (insertPaymentErr) {
    console.error("Failed to insert payment record:", insertPaymentErr);
    return;
  }

  // 5) Upsert premium_store
  const { error: upsertErr } = await supabase
    .from("premium_store")
    .upsert({
      user_id,
      plan,
      started_at: now.toISOString(),
      expires_at: newExpiry.toISOString(),
      source: "paystack",
      metadata: trxData.metadata || {},
    }, { onConflict: "user_id" });

  if (upsertErr) {
    console.error("Failed to upsert premium_store:", upsertErr);
    return;
  }

  console.info("Processed payment:", reference, "user:", user_id, "newExpiry:", newExpiry.toISOString(), "amount:", amountNaira);
}

serve(async (req) => {
  try {
    if (req.method !== "POST") {
      return new Response("Method not allowed", { status: 405 });
    }

    // immediate header log (helps debug)
    try {
      console.log("HEADERS:", JSON.stringify(Object.fromEntries(req.headers.entries())));
    } catch (_) {}

    // read raw bytes
    const arrayBuf = await req.arrayBuffer();
    const bodyBytes = new Uint8Array(arrayBuf);

    console.log("Raw body length:", bodyBytes.length);

    const signature = req.headers.get("x-paystack-signature") ?? null;
    console.log("Signature present:", !!signature);

    if (!PAYSTACK_SECRET) {
      console.error("PAYSTACK_SECRET_KEY not set");
      return new Response("Server misconfigured", { status: 500 });
    }

    // If signature missing: try fallback verify (best-effort) else ACK with 200 to avoid retries
    if (!signature) {
      console.warn("Unsigned webhook received — attempting fallback verify if reference present");
      try {
        const payloadText = new TextDecoder().decode(bodyBytes);
        const payloadJson = JSON.parse(payloadText);
        console.log("FULL EVENT (unsigned):", payloadJson); // debug

        const ref = payloadJson?.data?.reference;
        if (ref) {
          try {
            const verifyJson = await verifyTransactionWithPaystack(ref);
            if (verifyJson?.status && verifyJson.data?.status === "success") {
              console.info("Fallback verify confirmed success for", ref);
              await processTransaction(verifyJson.data);
              return new Response("OK", { status: 200 });
            } else {
              console.warn("Fallback verify did not show success for", ref);
            }
          } catch (err) {
            console.warn("Fallback verify error:", err);
          }
        }
      } catch (err) {
        console.warn("Unable to parse unsigned payload", err);
      }

      // Ack to avoid Paystack retries
      return new Response("OK", { status: 200 });
    }

    // compute HMAC of raw bytes
    const computedHex = await computeHmacSha512Hex(PAYSTACK_SECRET, bodyBytes);
    const headerHex = (signature || "").toLowerCase();

    console.log("Computed HMAC:", computedHex, "Header HMAC:", headerHex);

    // If mismatch: attempt Paystack verify fallback; if still can't confirm -> ack and stop
    if (computedHex !== headerHex) {
      console.warn("HMAC mismatch — attempting fallback verify by reference");
      try {
        const payloadText = new TextDecoder().decode(bodyBytes);
        const payloadJson = JSON.parse(payloadText);
        console.log("FULL EVENT (mismatch):", payloadJson); // debug

        const ref = payloadJson?.data?.reference;
        if (ref) {
          try {
            const verifyJson = await verifyTransactionWithPaystack(ref);
            if (verifyJson?.status && verifyJson.data?.status === "success") {
              console.info("Fallback verify succeeded for", ref);
              await processTransaction(verifyJson.data);
              return new Response("OK", { status: 200 });
            } else {
              console.warn("Fallback verify did not confirm success for", ref);
            }
          } catch (err) {
            console.warn("Fallback verify failed:", err);
          }
        }
      } catch (err) {
        console.warn("Could not parse payload for fallback verify", err);
      }

      // ack and stop (do not return 401)
      return new Response("OK", { status: 200 });
    }

    // signature verified
    const text = new TextDecoder().decode(bodyBytes);
    const payload = JSON.parse(text);

    // full event log (temporary for debugging)
    console.log("FULL EVENT (verified):", payload);

    if (payload.event !== "charge.success") {
      console.info("Ignoring non-charge event:", payload.event);
      return new Response("OK", { status: 200 });
    }

    // process the successful charge
    const trxData = payload.data;
    await processTransaction(trxData);

    return new Response("OK", { status: 200 });
  } catch (err) {
    console.error("Unhandled webhook error:", err);
    // always ACK to avoid repeated retries from Paystack
    return new Response("OK", { status: 200 });
  }
});
