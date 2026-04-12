FROM python:3.13-slim AS base

WORKDIR /app

RUN adduser --disabled-password --gecos "" nexus

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY alembic/ alembic/
COPY app/ app/
COPY scripts/ scripts/

RUN chown -R nexus:nexus /app
USER nexus

EXPOSE 9000

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 9000"]
