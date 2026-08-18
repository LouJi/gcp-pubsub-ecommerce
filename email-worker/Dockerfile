# --- Build stage ---
FROM maven:3.9-eclipse-temurin-17 AS build

WORKDIR /build

# Copy pom.xml first for better layer caching of dependencies
COPY pom.xml .
RUN mvn -B dependency:go-offline

COPY src ./src
RUN mvn -B clean package -DskipTests

# --- Runtime stage ---
FROM eclipse-temurin:17-jre-jammy

WORKDIR /app
COPY --from=build /build/target/email-worker.jar app.jar

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "java -jar app.jar --server.port=${PORT}"]
