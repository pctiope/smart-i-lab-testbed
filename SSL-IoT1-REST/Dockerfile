# Pin to a specific version tag; for production also pin by digest:
#   docker pull node:22-alpine && docker inspect --format='{{index .RepoDigests 0}}' node:22-alpine
# Then replace with: FROM node:22-alpine@sha256:<digest>
FROM node:22-alpine

LABEL org.opencontainers.image.title="SSL IoT-1 REST API" \
      org.opencontainers.image.description="Express REST API for the Smart i-LAB IoT testbed" \
      org.opencontainers.image.source="https://github.com/SSL-IoT-1"

WORKDIR /RESTapi

# Install deps as root, then drop privileges.
COPY --chown=node:node package*.json ./
RUN npm ci --omit=dev

COPY --chown=node:node . .

USER node

EXPOSE 3000

CMD ["npm", "start"]
