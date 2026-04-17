# ---- Build stage: install deps into a virtual env ----
FROM python:3.13-slim AS builder

WORKDIR /build

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt && \
    /opt/venv/bin/pip install --no-cache-dir gunicorn

# ---- Runtime stage: slim image with only what's needed ----
FROM python:3.13-slim AS runtime

RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 curl && \
    rm -rf /var/lib/apt/lists/*

RUN adduser --disabled-password --gecos "" --home /app nexus

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY alembic.ini .
COPY alembic/ alembic/
COPY app/ app/
COPY scripts/ scripts/

RUN mkdir -p /app/data && chown -R nexus:nexus /app
USER nexus

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://127.0.0.1:9000/health || exit 1

# gunicorn with uvicorn workers: graceful shutdown via SIGTERM,
# configurable workers via GUNICORN_WORKERS env var (default 2).
CMD ["sh", "-c", \
     "gunicorn app.main:app \
       --bind 0.0.0.0:9000 \
       --worker-class uvicorn.workers.UvicornWorker \
       --workers ${GUNICORN_WORKERS:-2} \
       --timeout ${GUNICORN_TIMEOUT:-120} \
       --graceful-timeout ${SHUTDOWN_DRAIN_SECONDS:-30} \
       --access-logfile - \
       --error-logfile -"]
