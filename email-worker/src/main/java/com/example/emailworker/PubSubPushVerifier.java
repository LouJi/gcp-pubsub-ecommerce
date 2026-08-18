package com.example.emailworker;

import com.google.api.client.googleapis.auth.oauth2.GoogleIdToken;
import com.google.api.client.googleapis.auth.oauth2.GoogleIdTokenVerifier;
import com.google.api.client.googleapis.javanet.GoogleNetHttpTransport;
import com.google.api.client.json.gson.GsonFactory;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.security.GeneralSecurityException;
import java.util.Collections;

/**
 * Verifies the OIDC identity token Pub/Sub attaches to each push request,
 * mirroring the same check the Python Firestore subscriber performs -
 * so this endpoint only accepts messages that actually came from Pub/Sub,
 * authenticated as the configured invoker service account.
 */
@Component
public class PubSubPushVerifier {

    private static final Logger log = LoggerFactory.getLogger(PubSubPushVerifier.class);

    @Value("${pubsub.push.audience:}")
    private String expectedAudience;

    @Value("${pubsub.push.service-account:}")
    private String expectedServiceAccount;

    private GoogleIdTokenVerifier verifier;

    private GoogleIdTokenVerifier getVerifier() throws GeneralSecurityException, java.io.IOException {
        if (verifier == null) {
            if (expectedAudience == null || expectedAudience.isBlank()) {
                throw new IllegalStateException("PUBSUB_PUSH_AUDIENCE is not configured on the server");
            }
            verifier = new GoogleIdTokenVerifier.Builder(
                    GoogleNetHttpTransport.newTrustedTransport(), GsonFactory.getDefaultInstance())
                    .setAudience(Collections.singletonList(expectedAudience))
                    .build();
        }
        return verifier;
    }

    /**
     * @throws SecurityException if the token is missing, invalid, or fails verification
     */
    public void verify(String authorizationHeader) {
        if (authorizationHeader == null || !authorizationHeader.startsWith("Bearer ")) {
            throw new SecurityException("Missing or malformed Authorization header");
        }
        String tokenString = authorizationHeader.substring("Bearer ".length());

        GoogleIdToken idToken;
        try {
            idToken = getVerifier().verify(tokenString);
        } catch (Exception e) {
            throw new SecurityException("Token verification failed: " + e.getMessage(), e);
        }

        if (idToken == null) {
            throw new SecurityException("Token failed signature/audience verification");
        }

        if (expectedServiceAccount != null && !expectedServiceAccount.isBlank()) {
            String email = idToken.getPayload().getEmail();
            if (!expectedServiceAccount.equals(email)) {
                throw new SecurityException("Unexpected invoking service account: " + email);
            }
        }
    }
}
