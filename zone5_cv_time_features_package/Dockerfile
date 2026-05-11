# syntax=docker/dockerfile:1

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    PYTHON_BIN=python3 \
    YOLO_AUTOINSTALL=False \
    YOLO_CONFIG_DIR=/tmp/ultralytics \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        libglib2.0-0 \
        libgl1 \
        libgomp1 \
    && python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN chmod +x docker-entrypoint.sh run_*.sh train_model.sh \
    && mkdir -p data model logs /tmp/ultralytics /tmp/matplotlib \
    && chmod 1777 /tmp/ultralytics /tmp/matplotlib

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["bash", "run_live_app.sh"]
