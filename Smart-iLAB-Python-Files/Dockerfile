# Switched from python:3.12.3-alpine — psycopg2-binary wheels are glibc-only, so alpine
# fell back to source compile without the required build deps. Debian slim has glibc.
# For production also pin by digest:
#   docker pull python:3.12-slim && docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
# Then replace with: FROM python:3.12-slim@sha256:<digest>
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Smart i-LAB Python Databridge" \
      org.opencontainers.image.description="MQTT/HA subscribers persisting IoT data to TimescaleDB" \
      org.opencontainers.image.source="https://github.com/SSL-IoT-1"

# Create non-root user.
RUN groupadd -r ssl --gid 1000 && useradd -r -g ssl --uid 1000 --create-home ssl

WORKDIR /pytodb

COPY --chown=ssl:ssl requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=ssl:ssl . .

RUN chmod a+x run.sh

USER ssl

# run.sh is retained for backwards compat; compose.yaml now runs one Python file
# per container (see §6.5 split). CMD here is the single-container fallback.
CMD ["python3", "ESPDevices_to_Database.py"]
