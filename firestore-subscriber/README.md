# Firestore subscriber

A Cloud Run service that receives order events via a Pub/Sub **push**
subscription and writes each order to Firestore. This is one of four
subscriptions on the `order-events` topic (the others being this service's
DLQ, the Java email/log worker, and its DLQ).

## Project layout

```
firestore-subscriber/
├── Dockerfile
├── requirements.txt
├── .dockerignore
└── app.py
```

## Why push instead of pull?

Cloud Run scales to zero and only runs while handling a request — it can't
sit in a loop pulling from a subscription. A **push subscription** flips
that: Pub/Sub itself POSTs each message to this service's `/pubsub/push`
endpoint, which is a natural fit for Cloud Run's request-driven model.

## Prerequisites

- The `order-events` topic already exists (created for the checkout site).
- Firestore is enabled in Native mode for the project.
- `gcloud` CLI authenticated with `gcloud auth login`, project set via
  `gcloud config set project YOUR_PROJECT_ID`.

## 1. Enable Firestore (if not already)

```bash
gcloud services enable firestore.googleapis.com
gcloud firestore databases create --location=us-central1
```

(Skip the `databases create` step if a Firestore database already exists
for this project.)

## 2. Local setup

```bash
cd firestore-subscriber
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
gcloud auth application-default login
```

## 3. Run locally with push verification disabled

Verifying the Pub/Sub OIDC token only makes sense once the service has a
real HTTPS URL, so for local testing, turn verification off and POST a
fake push envelope by hand:

```bash
export GCP_PROJECT_ID=YOUR_PROJECT_ID
export VERIFY_PUSH_TOKEN=false

python3 app.py
```

In another terminal, simulate a Pub/Sub push (the `data` field is a
base64-encoded order JSON):

```bash
python3 -c "
import base64, json
order = {
    'order_id': 'test-order-1',
    'timestamp': '2026-08-11T12:00:00Z',
    'user_name': 'Jane Doe',
    'user_email': 'jane@example.com',
    'items': [{'item_number': 'SKU-001', 'item_name': 'Trail Running Shoes', 'quantity': 2}]
}
data = base64.b64encode(json.dumps(order).encode()).decode()
print(json.dumps({'message': {'data': data, 'messageId': 'local-test-1'}}))
" > /tmp/push_body.json

curl -X POST http://localhost:8080/pubsub/push \
  -H "Content-Type: application/json" \
  -d @/tmp/push_body.json
```

Check the Firestore console (or `gcloud firestore` commands) for a new
`orders/test-order-1` document.

## 4. Create a dedicated invoker service account

Pub/Sub needs its own identity to authenticate its push requests to this
service:

```bash
gcloud iam service-accounts create pubsub-push-invoker \
  --display-name="Pub/Sub push invoker"
```

## 5. Deploy to Cloud Run

Deploy first (without the push subscription — it doesn't exist yet) so you
get a stable service URL:

```bash
gcloud builds submit --tag gcr.io/$PROJECT_ID/firestore-subscriber

gcloud run deploy firestore-subscriber \
  --image gcr.io/$PROJECT_ID/firestore-subscriber \
  --platform managed \
  --region us-central1 \
  --no-allow-unauthenticated \
  --set-env-vars GCP_PROJECT_ID=$PROJECT_ID,PUBSUB_PUSH_SERVICE_ACCOUNT=pubsub-push-invoker@$PROJECT_ID.iam.gserviceaccount.com

SERVICE_URL=$(gcloud run services describe firestore-subscriber \
  --region us-central1 --format='value(status.url)')

gcloud run services update firestore-subscriber \
  --region us-central1 \
  --set-env-vars GCP_PROJECT_ID=$PROJECT_ID,PUBSUB_PUSH_SERVICE_ACCOUNT=pubsub-push-invoker@$PROJECT_ID.iam.gserviceaccount.com,PUBSUB_PUSH_AUDIENCE=$SERVICE_URL
```

(`PUBSUB_PUSH_AUDIENCE` is only known after the first deploy, hence the
two-step `set-env-vars`.)

## 6. Grant IAM roles

The invoker service account needs to be allowed to call this Cloud Run
service:

```bash
gcloud run services add-iam-policy-binding firestore-subscriber \
  --region us-central1 \
  --member="serviceAccount:pubsub-push-invoker@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

The Cloud Run service's own runtime identity needs Firestore write access:

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:YOUR_RUNTIME_SERVICE_ACCOUNT" \
  --role="roles/datastore.user"
```

## 7. Create the push subscription (with a dead-letter policy)

This is the piece that actually connects Pub/Sub to this service. It also
wires up the DLQ — messages that fail delivery `max-delivery-attempts`
times get routed to `order-events-dlq` instead of retrying forever.

```bash
# DLQ topic (if not already created)
gcloud pubsub topics create order-events-dlq

gcloud pubsub subscriptions create firestore-writer-sub \
  --topic=order-events \
  --push-endpoint=$SERVICE_URL/pubsub/push \
  --push-auth-service-account=pubsub-push-invoker@$PROJECT_ID.iam.gserviceaccount.com \
  --dead-letter-topic=order-events-dlq \
  --max-delivery-attempts=5
```

Pub/Sub also needs permission to publish to the DLQ topic and to
acknowledge messages on your subscription on your behalf — `gcloud` prompts
you to grant these automatically the first time you create a dead-letter
subscription; accept the prompt (or grant `roles/pubsub.publisher` on the
DLQ topic and `roles/pubsub.subscriber` on the subscription to the Pub/Sub
service agent manually).

## 8. End-to-end test

Place an order on the checkout site and confirm a matching document shows
up in the `orders` collection in Firestore.

## Notes

- This service intentionally has no knowledge of the Java email worker or
  its DLQ — it only knows about Firestore.
- Setting `VERIFY_PUSH_TOKEN=false` is for local testing only; leave it
  `true` (the default) in Cloud Run.
