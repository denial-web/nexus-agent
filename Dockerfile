FROM python:3.13-slim AS base

WORKDIR /app

RUN adduser --disabled-password --gecos "" nexus

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY alembic/ alembic/
COPY app/ app/
COPY scripts/ scripts/

RUN mkdir -p /app/data && chown -R nexus:nexus /app
USER nexus

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
