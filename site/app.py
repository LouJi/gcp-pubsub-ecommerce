"""
Cloud Run web front-end (publisher) for the order-processing pipeline.

Serves a simple checkout page with 3 fixed items. When the user checks out,
this app publishes ONE Pub/Sub message per ORDER (not per item). The message
contains the full list of items ordered, so every subscriber that reads it
sees the whole order in one shot:

    - Python Firestore subscriber writes the order (and its items)
    - Java worker sends ONE batch email listing every item + a log entry
    - Each of those has its own dead-letter subscription for failed delivery

All four subscriptions read from the SAME topic. Each subscriber pulls only
the fields it needs from the shared message shape.

Environment variables (set these in Cloud Run):
    GCP_PROJECT_ID   - GCP project ID that owns the Pub/Sub topic
    PUBSUB_TOPIC_ID  - Pub/Sub topic name (not the full path)
    PORT             - provided automatically by Cloud Run (defaults to 8080)
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

from flask import Flask, render_template, request, url_for, flash
from google.cloud import pubsub_v1
from google.api_core.exceptions import GoogleAPIError

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
TOPIC_ID = os.environ.get("PUBSUB_TOPIC_ID", "order-events")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Fixed catalog. item_number is what gets sent downstream as the stable ID.
ITEMS = [
    {"item_number": "SKU-001", "name": "Trail Running Shoes", "price": 89.99},
    {"item_number": "SKU-002", "name": "Insulated Water Bottle", "price": 24.50},
    {"item_number": "SKU-003", "name": "Packable Rain Jacket", "price": 64.00},
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("checkout-publisher")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

# --------------------------------------------------------------------------
# Pub/Sub client (created once, reused across requests / warm instances)
# --------------------------------------------------------------------------

_publisher = None
_topic_path = None


def get_publisher():
    """Lazily create the Pub/Sub publisher client and topic path."""
    global _publisher, _topic_path
    if _publisher is None:
        if not PROJECT_ID:
            raise RuntimeError("GCP_PROJECT_ID environment variable is not set")
        _publisher = pubsub_v1.PublisherClient()
        _topic_path = _publisher.topic_path(PROJECT_ID, TOPIC_ID)
    return _publisher, _topic_path


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", items=ITEMS)


@app.route("/healthz", methods=["GET"])
def healthz():
    """Basic liveness check for Cloud Run / load balancer health checks."""
    return {"status": "ok"}, 200


@app.route("/checkout", methods=["POST"])
def checkout():
    form = request.form

    user_name = (form.get("user_name") or "").strip()
    user_email = (form.get("user_email") or "").strip()

    errors = []

    if not user_name:
        errors.append("Name is required.")
    if not user_email or not EMAIL_RE.match(user_email):
        errors.append("A valid email address is required.")

    # Collect quantities for each item from the form
    order_items = []
    for item in ITEMS:
        raw_qty = form.get(f"qty_{item['item_number']}", "0").strip()
        try:
            qty = int(raw_qty) if raw_qty else 0
        except ValueError:
            errors.append(f"Quantity for {item['name']} must be a whole number.")
            continue

        if qty < 0:
            errors.append(f"Quantity for {item['name']} cannot be negative.")
        elif qty > 0:
            order_items.append(
                {
                    "item_number": item["item_number"],
                    "item_name": item["name"],
                    "quantity": qty,
                }
            )

    if not order_items:
        errors.append("Select a quantity greater than 0 for at least one item.")

    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("index.html", items=ITEMS, form=form), 400

    # All good -> publish ONE message for the whole order
    order_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    message = {
        "order_id": order_id,
        "timestamp": timestamp,
        "user_name": user_name,
        "user_email": user_email,
        "items": order_items,
    }
    data = json.dumps(message).encode("utf-8")

    try:
        publisher, topic_path = get_publisher()
    except RuntimeError as e:
        logger.exception("Publisher not configured")
        flash(str(e), "error")
        return render_template("index.html", items=ITEMS, form=form), 500

    try:
        # Attributes let subscriptions filter without decoding the payload.
        future = publisher.publish(
            topic_path,
            data=data,
            order_id=order_id,
            item_count=str(len(order_items)),
        )
        message_id = future.result(timeout=10)
    except GoogleAPIError:
        logger.exception("Failed to publish order %s", order_id)
        flash("Your order could not be submitted. Please try again.", "error")
        return render_template("index.html", items=ITEMS, form=form), 502

    logger.info(
        "Published order %s (%d item line(s)) as message %s",
        order_id,
        len(order_items),
        message_id,
    )

    return render_template(
        "confirmation.html",
        order_id=order_id,
        user_name=user_name,
        user_email=user_email,
        items=order_items,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
