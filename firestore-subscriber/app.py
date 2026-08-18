"""
Cloud Run subscriber that receives order events via a Pub/Sub PUSH
subscription and writes each order to Firestore.

This service does not poll Pub/Sub. Instead, the Pub/Sub subscription is
configured (in Terraform) to push each message as an HTTP POST to this
service's /pubsub/push endpoint. Cloud Run scales this service up/down
per request like any other HTTP service.

Delivery semantics:
    - Return 200          -> message is ACKed, Pub/Sub won't redeliver it
    - Return 4xx/5xx       -> message is NACKed, Pub/Sub retries it
    - After max_delivery_attempts (set on the subscription) is exceeded,
      Pub/Sub routes the message to the configured dead-letter topic.

Because delivery is at-least-once, the SAME message can arrive more than
once. Writes here are idempotent (order_id is used as the Firestore
document ID with set()), so a redelivery just overwrites identical data
rather than creating a duplicate order.

Environment variables:
    GCP_PROJECT_ID          - GCP project ID (used for Firestore + token audience)
    FIRESTORE_DATABASE_ID   - Firestore database ID (default: "(default)")
    PUBSUB_PUSH_AUDIENCE    - expected OIDC token audience (this service's URL)
    VERIFY_PUSH_TOKEN       - "true"/"false", default "true"
    PORT                     - provided automatically by Cloud Run
"""

import base64
import json
import logging
import os
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from google.cloud import firestore
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
DATABASE_ID = os.environ.get("FIRESTORE_DATABASE_ID", "(default)")
PUSH_AUDIENCE = os.environ.get("PUBSUB_PUSH_AUDIENCE")
VERIFY_PUSH_TOKEN = os.environ.get("VERIFY_PUSH_TOKEN", "true").lower() == "true"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("firestore-subscriber")

app = Flask(__name__)

_db = None
_google_auth_request = google_requests.Request()


def get_db():
    """Lazily create the Firestore client (reused across warm instances)."""
    global _db
    if _db is None:
        if not PROJECT_ID:
            raise RuntimeError("GCP_PROJECT_ID environment variable is not set")
        _db = firestore.Client(project=PROJECT_ID, database=DATABASE_ID)
    return _db


# --------------------------------------------------------------------------
# Push request verification
# --------------------------------------------------------------------------

def verify_push_request(req):
    """
    Verify the OIDC token Pub/Sub attaches to push requests, so this
    endpoint only accepts messages that actually came from Pub/Sub.
    Raises ValueError on any verification failure.
    """
    auth_header = req.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise ValueError("Missing or malformed Authorization header")

    token = auth_header.split(" ", 1)[1]

    if not PUSH_AUDIENCE:
        raise ValueError("PUBSUB_PUSH_AUDIENCE is not configured on the server")

    # This checks the token's signature, expiry, and audience against
    # Google's public keys - it does NOT make an outbound network call
    # to Pub/Sub itself.
    claims = id_token.verify_oauth2_token(
        token, _google_auth_request, audience=PUSH_AUDIENCE
    )

    # Optional extra check: restrict to a specific invoking service account.
    expected_sa = os.environ.get("PUBSUB_PUSH_SERVICE_ACCOUNT")
    if expected_sa and claims.get("email") != expected_sa:
        raise ValueError(f"Unexpected invoking service account: {claims.get('email')}")


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/healthz", methods=["GET"])
def healthz():
    return {"status": "ok"}, 200


@app.route("/pubsub/push", methods=["POST"])
def pubsub_push():
    if VERIFY_PUSH_TOKEN:
        try:
            verify_push_request(request)
        except ValueError as e:
            logger.warning("Rejected push request: %s", e)
            # 401 is NOT retried differently than 500 by Pub/Sub, but it
            # makes the reason obvious in logs.
            return jsonify({"error": str(e)}), 401

    envelope = request.get_json(silent=True)
    if not envelope or "message" not in envelope:
        logger.error("Push request missing Pub/Sub message envelope")
        # Malformed envelope will never succeed on retry -> ack it away
        # so it doesn't clog the subscription. Nothing useful for a DLQ here.
        return jsonify({"error": "Bad envelope"}), 200

    pubsub_message = envelope["message"]
    message_id = pubsub_message.get("messageId", "unknown")

    try:
        data_b64 = pubsub_message.get("data", "")
        raw = base64.b64decode(data_b64).decode("utf-8")
        order = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as e:
        logger.error("Message %s has unparseable payload: %s", message_id, e)
        # A malformed payload will never parse successfully on retry either.
        # Returning 200 here would silently drop it; instead we return 5xx
        # so it exhausts delivery attempts and lands in the DLQ, where it
        # can be inspected instead of silently discarded.
        return jsonify({"error": "Unparseable payload"}), 500

    order_id = order.get("order_id")
    if not order_id:
        logger.error("Message %s has no order_id", message_id)
        return jsonify({"error": "Missing order_id"}), 500

    try:
        write_order(order)
    except Exception:
        logger.exception("Failed to write order %s to Firestore", order_id)
        return jsonify({"error": "Firestore write failed"}), 500

    logger.info(
        "Wrote order %s (%d item line(s)) from message %s",
        order_id,
        len(order.get("items", [])),
        message_id,
    )
    return jsonify({"status": "ok"}), 200


def write_order(order: dict) -> None:
    """
    Persist one order to Firestore. Uses order_id as the document ID and
    set() (not add()) so redelivered messages overwrite identical data
    instead of creating duplicates.
    """
    db = get_db()
    doc_ref = db.collection("orders").document(order["order_id"])
    doc_ref.set(
        {
            "order_id": order["order_id"],
            "user_name": order.get("user_name"),
            "user_email": order.get("user_email"),
            "items": order.get("items", []),
            "order_timestamp": order.get("timestamp"),
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
