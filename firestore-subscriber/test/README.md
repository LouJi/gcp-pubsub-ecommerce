# End-to-end test: checkout site → Pub/Sub → Firestore subscriber

Covers the happy path (order → Pub/Sub → Firestore), idempotency on
redelivery, and deliberately forcing the dead-letter queue path — all
before building the Java worker on top of the same topic.

## Prerequisites

- `checkout-site` and `firestore-subscriber` are both deployed to Cloud Run
  (privately — no `--allow-unauthenticated`).
- `firestore-writer-sub` exists on `order-events`, with a `deadLetterPolicy`
  pointing at `order-events-dlq`.
- You know the runtime service account the Firestore subscriber deploys as
  (referred to below as `YOUR_SUBSCRIBER_RUNTIME_SA`).

```bash
export PROJECT_ID=YOUR_PROJECT_ID
```

## 1. Verify the subscription is wired correctly (no traffic yet)

```bash
gcloud pubsub subscriptions describe firestore-writer-sub
```

Confirm in the output:
- `pushEndpoint` matches the Firestore subscriber's URL + `/pubsub/push`
- `oidcToken.serviceAccountEmail` is `pubsub-push-invoker@...`
- `deadLetterPolicy` points at `order-events-dlq` with the expected
  `maxDeliveryAttempts`

## 2. Reach the private checkout site

Both services are private, so open a local authenticated tunnel instead of
hitting the Cloud Run URL directly:

```bash
gcloud run services proxy checkout-site --region us-central1 --port=8080
```

Leave that running and open `http://localhost:8080` in your browser.

## 3. Submit a real order through the UI

Fill out the form and submit. You should land on the confirmation page.
Then confirm the publish actually happened:

```bash
gcloud run services logs read checkout-site --region us-central1 --limit=20
```

Look for a line like:

```
Published order <uuid> (...) as message <id>
```

## 4. Confirm the push delivery and Firestore write succeeded

```bash
gcloud run services logs read firestore-subscriber --region us-central1 --limit=20
```

Look for:

```
Wrote order <uuid> (...) from message <id>
```

If you see 401s instead, it's almost always `PUBSUB_PUSH_AUDIENCE` not
matching the service's actual URL — double-check that env var.

## 5. Confirm the Firestore document itself

Easiest as a one-off check: Firestore console → Data → `orders` collection
→ the `order_id` from the logs.

Or from the terminal:

```bash
python3 -c "
from google.cloud import firestore
db = firestore.Client(project='$PROJECT_ID')
doc = db.collection('orders').document('PASTE_ORDER_ID_HERE').get()
print(doc.to_dict())
"
```

## 6. Verify idempotency (optional but worth doing once)

Pub/Sub push delivery is at-least-once, so a message can be redelivered.
Re-publish the same order (same `order_id`) manually and confirm the
Firestore doc's `received_at` timestamp updates but **no second document**
is created.

## 7. Deliberately trigger the DLQ path

This is the one piece you haven't seen fire yet — worth proving out before
the Java worker exists.

**a. Temporarily break the subscriber's Firestore access:**

```bash
gcloud projects remove-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:YOUR_SUBSCRIBER_RUNTIME_SA" \
  --role="roles/datastore.user"
```

**b. Place another test order.** Every push attempt now fails with a 500
(Firestore write error). Watch the retries:

```bash
gcloud run services logs read firestore-subscriber --region us-central1 --limit=30
```

**c. After `maxDeliveryAttempts` is exhausted, confirm the message landed
in the DLQ:**

```bash
gcloud pubsub subscriptions create temp-dlq-check --topic=order-events-dlq
gcloud pubsub subscriptions pull temp-dlq-check --auto-ack --limit=5
gcloud pubsub subscriptions delete temp-dlq-check
```

**d. Restore the permission and confirm normal orders work again:**

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:YOUR_SUBSCRIBER_RUNTIME_SA" \
  --role="roles/datastore.user"
```

Place one more order and repeat steps 3–5 to confirm the pipeline is
healthy again.

## 8. Optional: watch it through Cloud Monitoring

During step 7, the Pub/Sub subscription metric
`subscription/num_undelivered_messages` (Monitoring → Metrics Explorer,
resource type "Pub/Sub Subscription") is worth watching — it climbs while
the subscriber is broken and drops back to zero once access is restored and
the backlog drains or dead-letters.

## Real-world note: retry-then-recover

During initial setup, it's common to hit a couple of configuration issues
in a row before the pipeline is fully wired correctly — for example, a
push-token audience mismatch (401) followed by a missing
`roles/datastore.user` binding (403). If orders are submitted while those
issues are still being fixed, you don't need to manually replay them:

- Pub/Sub keeps retrying a message until it either succeeds or exhausts
  `maxDeliveryAttempts` — it doesn't drop a message just because earlier
  attempts failed.
- Once the underlying config issue (audience, IAM, etc.) is fixed, the
  **next automatic redelivery** of each still-pending message succeeds on
  its own, with no manual intervention needed.
- Because writes are idempotent (`order_id` as the document ID, `set()`
  instead of `add()`), each order still ends up as exactly **one**
  Firestore document, even though it may have been delivered and attempted
  multiple times along the way.

This is a good sanity check to run after any config fix during setup:
confirm the count of documents in the `orders` collection matches the
number of distinct orders submitted, not the number of delivery attempts.

## Summary of what this proves

| Step | Proves |
|---|---|
| 3 | Checkout site correctly publishes to the topic |
| 4 | Pub/Sub push delivery + OIDC verification work end to end |
| 5 | Firestore subscriber correctly parses and writes the order |
| 6 | Redelivery doesn't create duplicate orders |
| 7 | The dead-letter policy actually reroutes failed messages |

