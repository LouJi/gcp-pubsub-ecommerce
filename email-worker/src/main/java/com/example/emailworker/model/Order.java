package com.example.emailworker.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;

/**
 * Mirrors the order message published by the checkout site:
 *
 * {
 *   "order_id": "uuid",
 *   "timestamp": "iso8601",
 *   "user_name": "...",
 *   "user_email": "...",
 *   "items": [ { "item_number": "...", "item_name": "...", "quantity": 2 }, ... ]
 * }
 *
 * This is the SAME message shape the Firestore subscriber reads - each
 * subscriber just pulls out the fields it needs. This service uses all of
 * them (recipient + full item list for the batch email).
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public class Order {

    @JsonProperty("order_id")
    private String orderId;

    @JsonProperty("timestamp")
    private String timestamp;

    @JsonProperty("user_name")
    private String userName;

    @JsonProperty("user_email")
    private String userEmail;

    @JsonProperty("items")
    private List<OrderItem> items;

    public String getOrderId() {
        return orderId;
    }

    public void setOrderId(String orderId) {
        this.orderId = orderId;
    }

    public String getTimestamp() {
        return timestamp;
    }

    public void setTimestamp(String timestamp) {
        this.timestamp = timestamp;
    }

    public String getUserName() {
        return userName;
    }

    public void setUserName(String userName) {
        this.userName = userName;
    }

    public String getUserEmail() {
        return userEmail;
    }

    public void setUserEmail(String userEmail) {
        this.userEmail = userEmail;
    }

    public List<OrderItem> getItems() {
        return items;
    }

    public void setItems(List<OrderItem> items) {
        this.items = items;
    }
}
