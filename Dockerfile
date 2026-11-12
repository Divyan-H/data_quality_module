# syntax=docker/dockerfile:1.7
# Data Pipeline (DQ) service: the web app is built in the first stage and served by the Python service in the second.
#
#   docker build -t dq-service .
#   docker build --build-arg WITH_LIBREOFFICE=0 -t dq-service:slim .     # no .xls / .xlsb / .ods support, ~400 MB smaller
#   docker run --init -p 8000:8000 -e DQ_API_TOKEN=… -e STRUCTURE_HASH_KEY=… -v dq_data:/data dq-service

# ── stage 1: build the analyst web app ─────────────────────────────────────────────────────────────────────────────
FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ── stage 2: the service ───────────────────────────────────────────────────────────────────────────────────────────
FROM python:3.13-slim-bookworm AS service
ARG WITH_LIBREOFFICE=1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DQ_ENV=production DQ_HOST=0.0.0.0 DQ_PORT=8000 DQ_WORKSPACE=/data/runs DQ_AUDIT_DB=/data/audit.db \
    DQ_LOG_FORMAT=json DQ_SHUTDOWN_GRACE_S=30 STRUCTURE_STORAGE_ROOT=/data/storage HOME=/home/dq

# LibreOffice converts the legacy formats .xls / .xlsb / .ods (Phase A); it is the only system package the image adds.
RUN if [ "$WITH_LIBREOFFICE" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends libreoffice-calc-nogui && rm -rf /var/lib/apt/lists/*; \
    fi \
 && groupadd --system --gid 10001 dq && useradd --system --uid 10001 --gid dq --create-home --home-dir /home/dq dq \
 && mkdir -p /data && chown dq:dq /data

WORKDIR /app
COPY requirements.txt ./
RUN pip install --require-hashes -r requirements.txt
COPY src/ ./src/
COPY config/ ./config/
COPY --from=web /web/dist ./frontend/dist

USER dq
VOLUME ["/data"]
EXPOSE 8000
# Liveness only: the process answers. Readiness (disk, workspace) is GET /api/v1/ready.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status == 200 else 1)"]
# Run with an init process so worker processes are reaped and SIGTERM is forwarded: `docker run --init …`
# (the Compose file sets `init: true`).
CMD ["python", "-m", "src.api"]
