package com.example.emailworker;

import com.example.emailworker.model.Order;
import com.example.emailworker.model.OrderItem;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.mail.SimpleMailMessage;
import org.springframework.mail.javamail.JavaMailSender;
import org.springframework.stereotype.Service;

/**
 * Sends ONE email per order listing every item ordered (batch, not one
 * email per item) - the whole order arrives in a single Pub/Sub message,
 * so there's no aggregation to do; this just formats what's already there.
 */
@Service
public class OrderEmailService {

    private static final Logger log = LoggerFactory.getLogger(OrderEmailService.class);

    private final JavaMailSender mailSender;

    @Value("${app.mail.from-address}")
    private String fromAddress;

    public OrderEmailService(JavaMailSender mailSender) {
        this.mailSender = mailSender;
    }

    public void sendOrderConfirmation(Order order) {
        SimpleMailMessage message = new SimpleMailMessage();
        message.setFrom(fromAddress);
        message.setTo(order.getUserEmail());
        message.setSubject("Order Confirmation - " + order.getOrderId());
        message.setText(buildBody(order));

        mailSender.send(message);

        log.info(
                "Sent order confirmation email for order {} to {} ({} item line(s))",
                order.getOrderId(),
                order.getUserEmail(),
                order.getItems() == null ? 0 : order.getItems().size()
        );
    }

    private String buildBody(Order order) {
        StringBuilder body = new StringBuilder();
        body.append("Hi ").append(order.getUserName()).append(",\n\n");
        body.append("Thanks for your order! Here's what you ordered:\n\n");

        if (order.getItems() != null) {
            for (OrderItem item : order.getItems()) {
                body.append(String.format(
                        "  - %s (%s) x%d%n",
                        item.getItemName(), item.getItemNumber(), item.getQuantity()
                ));
            }
        }

        body.append("\nOrder ID: ").append(order.getOrderId());
        body.append("\n\nThanks for shopping with us!");
        return body.toString();
    }
}
