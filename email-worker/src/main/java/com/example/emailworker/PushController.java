package com.example.emailworker;

import com.example.emailworker.model.Order;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.Map;

/**
 * Receives order events via a Pub/Sub PUSH subscription (same pattern as
 * the Python Firestore subscriber) and, for each order, sends one batch
 * confirmation email and writes one log entry.
 *
 * Delivery semantics (same as the Firestore subscriber):
 *   - 200 -> ACKed, Pub/Sub won't redeliver
 *   - 4xx/5xx -> NACKed, Pub/Sub retries, eventually dead-letters after
 *     max_delivery_attempts (configured on the subscription itself)
 *
 * NOTE ON IDEMPOTENCY: unlike the Firestore write (which is a safe
 * overwrite on redelivery), sending an email is not naturally idempotent -
 * a redelivered message could send a duplicate email. For this project's
 * scope we accept that risk (Pub/Sub redelivery is uncommon in steady
 * state); a production version would track processed order_ids (e.g. in
 * a small datastore) and skip re-sending on a repeat delivery.
 */
@RestController
public class PushController {

    private static final Logger log = LoggerFactory.getLogger(PushController.class);

    private final PubSubPushVerifier verifier;
    private final OrderEmailService emailService;
    private final ObjectMapper objectMapper = new ObjectMapper();

    @Value("${pubsub.push.verify-token:true}")
    private boolean verifyPushToken;

    public PushController(PubSubPushVerifier verifier, OrderEmailService emailService) {
        this.verifier = verifier;
        this.emailService = emailService;
    }

    @GetMapping("/healthz")
    public ResponseEntity<Map<String, String>> healthz() {
        return ResponseEntity.ok(Map.of("status", "ok"));
    }

    @PostMapping("/pubsub/push")
    public ResponseEntity<Map<String, String>> pubsubPush(
            @RequestHeader(value = "Authorization", required = false) String authorizationHeader,
            @RequestBody String rawBody
    ) {
        if (verifyPushToken) {
            try {
                verifier.verify(authorizationHeader);
            } catch (SecurityException e) {
                log.warn("Rejected push request: {}", e.getMessage());
                return ResponseEntity.status(HttpStatus.UNAUTHORIZED)
                        .body(Map.of("error", e.getMessage()));
            }
        }

        ObjectNode envelope;
        String messageId = "unknown";
        try {
            envelope = (ObjectNode) objectMapper.readTree(rawBody);
        } catch (Exception e) {
            log.error("Push request body was not valid JSON: {}", e.getMessage());
            // Malformed envelope will never succeed on retry -> ack it away.
            return ResponseEntity.ok(Map.of("status", "bad envelope, acked"));
        }

        if (!envelope.has("message")) {
            log.error("Push request missing Pub/Sub message envelope");
            return ResponseEntity.ok(Map.of("status", "bad envelope, acked"));
        }

        ObjectNode pubsubMessage = (ObjectNode) envelope.get("message");
        if (pubsubMessage.has("messageId")) {
            messageId = pubsubMessage.get("messageId").asText();
        }

        Order order;
        try {
            String dataB64 = pubsubMessage.has("data") ? pubsubMessage.get("data").asText() : "";
            byte[] decoded = Base64.getDecoder().decode(dataB64);
            String json = new String(decoded, StandardCharsets.UTF_8);
            order = objectMapper.readValue(json, Order.class);
        } catch (Exception e) {
            log.error("Message {} has unparseable payload: {}", messageId, e.getMessage());
            // Unlike the envelope, a bad payload here likely indicates a real
            // upstream bug - return 5xx so it eventually reaches the DLQ for
            // inspection instead of silently disappearing.
            return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                    .body(Map.of("error", "Unparseable payload"));
        }

        if (order.getOrderId() == null || order.getOrderId().isBlank()) {
            log.error("Message {} has no order_id", messageId);
            return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                    .body(Map.of("error", "Missing order_id"));
        }

        try {
            emailService.sendOrderConfirmation(order);
        } catch (Exception e) {
            log.error("Failed to send confirmation email for order {}: {}", order.getOrderId(), e.getMessage(), e);
            return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                    .body(Map.of("error", "Email send failed"));
        }

        log.info(
                "Processed order {} from message {} ({} item line(s))",
                order.getOrderId(), messageId,
                order.getItems() == null ? 0 : order.getItems().size()
        );

        return ResponseEntity.ok(Map.of("status", "ok"));
    }
}
