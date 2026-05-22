# §6.3 — multi-stage build: produce a static bundle with Vite, serve via nginx.
# Replaces the previous "vite --host" dev-server-as-prod setup.

FROM node:22-alpine AS build

LABEL org.opencontainers.image.title="Smart i-LAB Digital Twin (build stage)"

WORKDIR /app

COPY --chown=node:node package*.json ./
RUN npm ci

COPY --chown=node:node . .

# VITE_API_URL is read at build time. Operators override via:
#   docker compose build --build-arg VITE_API_URL=https://api.example
ARG VITE_API_URL=http://localhost
ENV VITE_API_URL=${VITE_API_URL}

RUN npm run build

# ---- Runtime image ----
FROM nginx:1.27-alpine

LABEL org.opencontainers.image.title="Smart i-LAB Digital Twin" \
      org.opencontainers.image.description="Static bundle served by nginx" \
      org.opencontainers.image.source="https://github.com/SSL-IoT-1"

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

# Drop privileges. nginx-alpine ships an nginx user; the official image starts
# as root to bind low ports, then forks workers as nginx. Run worker as nginx user.
USER nginx
