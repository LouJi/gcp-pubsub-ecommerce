# Email worker (Java)

A Cloud Run service that receives order events via a Pub/Sub **push**
subscription (same pattern as the Python Firestore subscriber) and, for
each order, sends **one batch confirmation email** listing every item
ordered, and writes one log entry. This is the third of four subscriptions
on the `order-events` topic (Firestore writer, its DLQ, this worker, and
this worker's own DLQ).

## Project layout

```
email-worker/
├── Dockerfile
├── pom.xml
├── .dockerignore
└── src/main/
    ├── java/com/example/emailworker/
    │   ├── EmailWorkerApplication.java
    │   ├── PushController.java
    │   ├── PubSubPushVerifier.java
    │   ├── OrderEmailService.java
    │   └── model/
    │       ├── Order.java
    │       └── OrderItem.java
    └── resources/application.properties
```

## Prerequisites

- Java 17 (Temurin/OpenJDK) and Maven 3.9+ installed locally
- The `order-events` topic already exists
- An SMTP-capable email account or relay to send from (Gmail with an App
  Password, SendGrid's SMTP relay, Mailgun, AWS SES SMTP, etc. all work —
  this service just speaks standard SMTP, no vendor-specific API)
- `gcloud` CLI authenticated, project set

```bash
export PROJECT_ID=YOUR_PROJECT_ID
```

## 1. Choose and configure an SMTP provider

You need four values before running this locally or deploying it:

- `SMTP_HOST` — e.g. `smtp.sendgrid.net`, `smtp.gmail.com`
- `SMTP_PORT` — usually `587` (STARTTLS)
- `SMTP_USERNAME` / `SMTP_PASSWORD` — provider-specific credentials (for
  Gmail, this must be an **App Password**, not your regular password)
- `SMTP_FROM_ADDRESS` — the address orders appear to come from

Treat `SMTP_PASSWORD` as a secret — see step 6 for storing it properly in
Cloud Run rather than as a plain env var.

## 2. Build and test locally

```bash
cd email-worker
mvn clean package
```

Run it with push verification disabled and real SMTP credentials, so you
can send a real test email without needing a live Pub/Sub push:

```bash
export GCP_PROJECT_ID=$PROJECT_ID
export VERIFY_PUSH_TOKEN=false
export SMTP_HOST=smtp.your-provider.com
export SMTP_PORT=587
export SMTP_USERNAME=your-smtp-username
export SMTP_PASSWORD=your-smtp-password
export SMTP_FROM_ADDRESS=orders@example.com

java -jar target/email-worker.jar
```

In another terminal, simulate a Pub/Sub push the same way as the Firestore
subscriber's README:

```bash
python3 -c "
import base64, json
order = {
    'order_id': 'test-order-1',
    'timestamp': '2026-08-14T12:00:00Z',
    'user_name': 'Jane Doe',
    'user_email': 'your-real-inbox@example.com',
    'items': [
        {'item_number': 'SKU-001', 'item_name': 'Trail Running Shoes', 'quantity': 2},
        {'item_number': 'SKU-003', 'item_name': 'Packable Rain Jacket', 'quantity': 1}
    ]
}
data = base64.b64encode(json.dumps(order).encode()).decode()
print(json.dumps({'message': {'data': data, 'messageId': 'local-test-1'}}))
" > /tmp/push_body.json

curl -X POST http://localhost:8080/pubsub/push \
  -H "Content-Type: application/json" \
  -d @/tmp/push_body.json
```

Check the inbox at `user_email` for one email listing both items, and
check the console output for the `Processed order ...` log line.

## 3. Create a dedicated runtime service account

Keep this service's permissions scoped to only what it needs — it doesn't
touch Firestore or GCS, so it shouldn't have those roles:

```bash
gcloud iam service-accounts create email-worker-runtime \
  --display-name="Email worker runtime identity"
```

This service account doesn't need any GCP IAM roles for SMTP itself (SMTP
auth is handled by the username/password you provide, not IAM) — it's kept
separate purely so this service's identity is distinct from the Firestore
subscriber's, in case you add GCP-facing permissions later (e.g. reading
the SMTP password from Secret Manager, covered in step 6).

## 4. Deploy to Cloud Run (first pass, to get a URL)

```bash
gcloud builds submit --tag us-central1-docker.pkg.dev/$PROJECT_ID/containers/email-worker

gcloud run deploy email-worker \
  --image us-central1-docker.pkg.dev/$PROJECT_ID/containers/email-worker \
  --platform managed \
  --region us-central1 \
  --service-account=email-worker-runtime@$PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars="GCP_PROJECT_ID=$PROJECT_ID,PUBSUB_PUSH_SERVICE_ACCOUNT=pubsub-push-invoker@$PROJECT_ID.iam.gserviceaccount.com,SMTP_HOST=smtp.your-provider.com,SMTP_PORT=587,SMTP_FROM_ADDRESS=orders@example.com"
```

(No `--allow-unauthenticated` — same as the Firestore subscriber, this
should stay private and only callable by the `pubsub-push-invoker`
identity.)

## 5. Set the push audience now that the URL exists

Pub/Sub's push token audience defaults to the **full endpoint URL
including the path** — this is the exact issue we hit and fixed on the
Firestore subscriber, so set it correctly the first time here:

```bash
SERVICE_URL=$(gcloud run services describe email-worker \
  --region us-central1 --format='value(status.url)')

gcloud run services update email-worker \
  --region us-central1 \
  --update-env-vars="PUBSUB_PUSH_AUDIENCE=${SERVICE_URL}/pubsub/push"
```

## 6. Store the SMTP password properly (Secret Manager)

Passing `SMTP_PASSWORD` as a plain `--set-env-vars` value works, but it's
visible in `gcloud run services describe` output and in Cloud Console.
Better to use Secret Manager:

```bash
gcloud services enable secretmanager.googleapis.com

echo -n "your-smtp-password" | gcloud secrets create smtp-password --data-file=-

gcloud secrets add-iam-policy-binding smtp-password \
  --member="serviceAccount:email-worker-runtime@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud run services update email-worker \
  --region us-central1 \
  --set-secrets="SMTP_PASSWORD=smtp-password:latest"
```

## 7. Grant IAM roles

The Pub/Sub invoker identity needs permission to call this service too
(the same `pubsub-push-invoker` account used for the Firestore
subscriber — one invoker identity, multiple services):

```bash
gcloud run services add-iam-policy-binding email-worker \
  --region us-central1 \
  --member="serviceAccount:pubsub-push-invoker@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

## 8. Create the push subscription (with its own dead-letter policy)

```bash
gcloud pubsub topics create email-worker-dlq

gcloud pubsub subscriptions create email-worker-sub \
  --topic=order-events \
  --push-endpoint=${SERVICE_URL}/pubsub/push \
  --push-auth-service-account=pubsub-push-invoker@$PROJECT_ID.iam.gserviceaccount.com \
  --dead-letter-topic=email-worker-dlq \
  --max-delivery-attempts=5
```

Accept the prompt to auto-grant Pub/Sub the roles it needs to publish to
the DLQ topic and manage this subscription, same as with the Firestore
subscriber's DLQ setup.

## 9. End-to-end test

Place an order on the checkout site and confirm:

- One email arrives listing every item from that order (not one email
  per item)
- `gcloud run services logs read email-worker --region us-central1` shows
  a `Processed order ...` line

For the DLQ path specifically, see the shared `testing/README.md` — the
same "temporarily break a permission, watch it retry, confirm it lands in
the DLQ" approach applies here (e.g. temporarily point `SMTP_HOST` at a
bad value to force delivery failures).

## Notes on idempotency

Pub/Sub push delivery is at-least-once. The Firestore subscriber handles
redelivery safely because `Firestore.set()` on the same document ID is a
harmless overwrite. **Sending an email is not naturally idempotent** — a
redelivered message could, in principle, result in a duplicate email. For
this project's scope that risk is accepted as-is; a production version
would track processed `order_id`s (e.g. in a small key-value store) and
skip re-sending on a repeat delivery.
