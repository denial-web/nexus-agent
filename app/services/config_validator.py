"""Startup configuration validation.

Called during lifespan startup. Returns a list of errors (fatal) and
warnings (non-fatal). The caller decides whether to abort on errors.
"""

from __future__ import annotations

import logging
import os

from app.config import Settings

logger = logging.getLogger(__name__)


class ConfigIssue:
    __slots__ = ("level", "message")

    def __init__(self, level: str, message: str) -> None:
        self.level = level  # "error" or "warning"
        self.message = message

    def __repr__(self) -> str:
        return f"ConfigIssue({self.level}: {self.message})"


def validate(settings: Settings) -> list[ConfigIssue]:
    """Run all validation rules and return issues found."""
    issues: list[ConfigIssue] = []
    env = settings.ENVIRONMENT.lower()
    is_prod = env not in ("development", "dev", "test")

    _check_security(settings, is_prod, issues)
    _check_contradictions(settings, issues)
    _check_numeric_bounds(settings, issues)
    _check_timeouts(settings, issues)
    _check_providers(settings, issues)
    _check_database(settings, is_prod, issues)
    _check_multi_worker(settings, issues)

    return issues


def _check_security(settings: Settings, is_prod: bool, issues: list[ConfigIssue]) -> None:
    if not is_prod:
        return

    if not settings.NEXUS_API_KEY.strip():
        issues.append(
            ConfigIssue(
                "error",
                "NEXUS_API_KEY must be set in production. All API endpoints are unauthenticated without it.",
            )
        )

    if not settings.SESSION_SECRET.strip():
        issues.append(
            ConfigIssue(
                "error",
                "SESSION_SECRET must be set in production. "
                'Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"',
            )
        )

    if not settings.APPROVAL_REVIEWERS.strip():
        issues.append(
            ConfigIssue(
                "error",
                "APPROVAL_REVIEWERS must be set in production/beta so approval votes are restricted "
                "to configured reviewer identities.",
            )
        )

    keys = [k.strip() for k in settings.NEXUS_API_KEY.split(",") if k.strip()]
    if len(keys) != len(set(keys)):
        issues.append(
            ConfigIssue(
                "warning",
                "NEXUS_API_KEY contains duplicate keys. Remove duplicates.",
            )
        )

    cors_raw = settings.CORS_ORIGINS.strip()
    if cors_raw:
        origins = [o.strip() for o in cors_raw.split(",") if o.strip()]
        if "*" in origins:
            issues.append(
                ConfigIssue(
                    "warning",
                    "CORS_ORIGINS contains wildcard '*' in production. "
                    "Use explicit origins to prevent cross-site attacks.",
                )
            )
        for origin in origins:
            if origin != "*" and not origin.startswith(("http://", "https://")):
                issues.append(
                    ConfigIssue(
                        "warning",
                        f"CORS origin '{origin}' lacks an http(s):// scheme. Browsers require fully-qualified origins.",
                    )
                )
                break


def _check_contradictions(settings: Settings, issues: list[ConfigIssue]) -> None:
    if settings.MCP_ENABLED and settings.LOCAL_ONLY:
        issues.append(
            ConfigIssue(
                "error",
                "MCP_ENABLED=true and LOCAL_ONLY=true are contradictory. "
                "MCP proxy requires network access to forward tool calls.",
            )
        )

    if settings.OTEL_ENABLED and settings.LOCAL_ONLY:
        issues.append(
            ConfigIssue(
                "warning",
                "OTEL_ENABLED=true with LOCAL_ONLY=true: the OTel exporter will fail to reach the collector endpoint.",
            )
        )

    if settings.WEBHOOKS_ENABLED and settings.LOCAL_ONLY:
        issues.append(
            ConfigIssue(
                "warning",
                "WEBHOOKS_ENABLED=true with LOCAL_ONLY=true: webhook delivery to external URLs will fail.",
            )
        )


def _check_numeric_bounds(settings: Settings, issues: list[ConfigIssue]) -> None:
    if settings.OTEL_SAMPLE_RATE < 0.0 or settings.OTEL_SAMPLE_RATE > 1.0:
        issues.append(
            ConfigIssue(
                "error",
                f"OTEL_SAMPLE_RATE={settings.OTEL_SAMPLE_RATE} is outside [0.0, 1.0].",
            )
        )

    if settings.RATE_LIMIT_RPM < 0:
        issues.append(
            ConfigIssue(
                "error",
                f"RATE_LIMIT_RPM={settings.RATE_LIMIT_RPM} cannot be negative.",
            )
        )

    if settings.MAX_PROMPT_LENGTH < 0:
        issues.append(
            ConfigIssue(
                "error",
                f"MAX_PROMPT_LENGTH={settings.MAX_PROMPT_LENGTH} cannot be negative.",
            )
        )

    if settings.MAX_REQUEST_BODY_BYTES < 0:
        issues.append(
            ConfigIssue(
                "error",
                f"MAX_REQUEST_BODY_BYTES={settings.MAX_REQUEST_BODY_BYTES} cannot be negative.",
            )
        )

    if settings.CORS_MAX_AGE < 0:
        issues.append(
            ConfigIssue(
                "error",
                f"CORS_MAX_AGE={settings.CORS_MAX_AGE} cannot be negative.",
            )
        )

    if settings.APPROVAL_QUORUM < 1:
        issues.append(
            ConfigIssue(
                "warning",
                f"APPROVAL_QUORUM={settings.APPROVAL_QUORUM} is less than 1. "
                "Governance approvals will be auto-granted.",
            )
        )

    if settings.CB_FAILURE_THRESHOLD < 1:
        issues.append(
            ConfigIssue(
                "warning",
                f"CB_FAILURE_THRESHOLD={settings.CB_FAILURE_THRESHOLD} is less than 1. "
                "Circuit breakers will open immediately.",
            )
        )

    if settings.AGENT_MAX_STEPS < 1:
        issues.append(
            ConfigIssue(
                "warning",
                f"AGENT_MAX_STEPS={settings.AGENT_MAX_STEPS} is less than 1. Agent loop will not execute any steps.",
            )
        )

    if settings.WEBHOOK_MAX_RETRIES < 1:
        issues.append(
            ConfigIssue(
                "warning",
                f"WEBHOOK_MAX_RETRIES={settings.WEBHOOK_MAX_RETRIES} is less than 1. "
                "Webhook delivery will never be attempted.",
            )
        )

    if settings.WEBHOOK_BACKOFF_BASE < 0:
        issues.append(
            ConfigIssue(
                "error",
                f"WEBHOOK_BACKOFF_BASE={settings.WEBHOOK_BACKOFF_BASE} cannot be negative.",
            )
        )

    if settings.WEBHOOK_BACKOFF_MAX < settings.WEBHOOK_BACKOFF_BASE:
        issues.append(
            ConfigIssue(
                "warning",
                f"WEBHOOK_BACKOFF_MAX={settings.WEBHOOK_BACKOFF_MAX} is less than "
                f"WEBHOOK_BACKOFF_BASE={settings.WEBHOOK_BACKOFF_BASE}. "
                "Max backoff will effectively equal the base.",
            )
        )

    if settings.WEBHOOK_REQUEST_TIMEOUT <= 0:
        issues.append(
            ConfigIssue(
                "error",
                f"WEBHOOK_REQUEST_TIMEOUT={settings.WEBHOOK_REQUEST_TIMEOUT} must be positive.",
            )
        )

    if settings.WEBHOOK_MAX_CONSECUTIVE_FAILURES < 1:
        issues.append(
            ConfigIssue(
                "warning",
                f"WEBHOOK_MAX_CONSECUTIVE_FAILURES={settings.WEBHOOK_MAX_CONSECUTIVE_FAILURES} "
                "is less than 1. Webhooks will be disabled after first failure.",
            )
        )

    if settings.IDEMPOTENCY_TTL < 1:
        issues.append(
            ConfigIssue(
                "warning",
                f"IDEMPOTENCY_TTL={settings.IDEMPOTENCY_TTL} is less than 1 second. "
                "Cached responses will expire almost immediately.",
            )
        )

    if settings.IDEMPOTENCY_MAX_KEYS < 1:
        issues.append(
            ConfigIssue(
                "error",
                f"IDEMPOTENCY_MAX_KEYS={settings.IDEMPOTENCY_MAX_KEYS} must be at least 1.",
            )
        )


def _check_timeouts(settings: Settings, issues: list[ConfigIssue]) -> None:
    if settings.HEALTH_PROBE_TIMEOUT <= 0:
        issues.append(
            ConfigIssue(
                "error",
                f"HEALTH_PROBE_TIMEOUT={settings.HEALTH_PROBE_TIMEOUT} must be positive.",
            )
        )

    if settings.REQUEST_TIMEOUT_SECONDS < 0:
        issues.append(
            ConfigIssue(
                "error",
                f"REQUEST_TIMEOUT_SECONDS={settings.REQUEST_TIMEOUT_SECONDS} cannot be negative.",
            )
        )

    if settings.SHUTDOWN_DRAIN_SECONDS < 0:
        issues.append(
            ConfigIssue(
                "error",
                f"SHUTDOWN_DRAIN_SECONDS={settings.SHUTDOWN_DRAIN_SECONDS} cannot be negative.",
            )
        )

    gunicorn_timeout = float(os.environ.get("GUNICORN_TIMEOUT", "0"))
    if gunicorn_timeout > 0 and settings.REQUEST_TIMEOUT_SECONDS > gunicorn_timeout:
        issues.append(
            ConfigIssue(
                "warning",
                f"REQUEST_TIMEOUT_SECONDS ({settings.REQUEST_TIMEOUT_SECONDS}s) exceeds "
                f"GUNICORN_TIMEOUT ({gunicorn_timeout}s). Gunicorn will kill workers "
                "before pipeline timeout fires. Set GUNICORN_TIMEOUT >= REQUEST_TIMEOUT_SECONDS.",
            )
        )

    if (
        settings.REQUEST_TIMEOUT_SECONDS > 0
        and settings.SHUTDOWN_DRAIN_SECONDS > 0
        and settings.SHUTDOWN_DRAIN_SECONDS < settings.REQUEST_TIMEOUT_SECONDS
    ):
        issues.append(
            ConfigIssue(
                "warning",
                f"SHUTDOWN_DRAIN_SECONDS ({settings.SHUTDOWN_DRAIN_SECONDS}s) is less than "
                f"REQUEST_TIMEOUT_SECONDS ({settings.REQUEST_TIMEOUT_SECONDS}s). "
                "In-flight requests may be killed before their timeout expires during shutdown.",
            )
        )


def _check_providers(settings: Settings, issues: list[ConfigIssue]) -> None:
    has_cloud_key = any(
        [
            settings.GEMINI_API_KEY.strip(),
            settings.OPENAI_API_KEY.strip(),
            settings.DEEPSEEK_API_KEY.strip(),
        ]
    )
    has_local = bool(settings.LOCAL_HF_MODEL_ID.strip())

    if settings.LOCAL_ONLY and not has_local:
        issues.append(
            ConfigIssue(
                "warning",
                "LOCAL_ONLY=true but LOCAL_HF_MODEL_ID is empty. "
                "Only Ollama models will be available (requires running Ollama instance).",
            )
        )

    if not settings.LOCAL_ONLY and not has_cloud_key and not has_local:
        issues.append(
            ConfigIssue(
                "warning",
                "No LLM provider keys configured (GEMINI_API_KEY, OPENAI_API_KEY, "
                "DEEPSEEK_API_KEY, LOCAL_HF_MODEL_ID all empty). "
                "Pipeline will fall back to mock provider.",
            )
        )


def _check_database(settings: Settings, is_prod: bool, issues: list[ConfigIssue]) -> None:
    if is_prod and settings.DATABASE_URL.startswith("sqlite"):
        issues.append(
            ConfigIssue(
                "warning",
                "Using SQLite in production. SQLite does not support concurrent writes "
                "well and has no replication. Consider PostgreSQL for production.",
            )
        )

    if settings.DB_POOL_SIZE < 1:
        issues.append(
            ConfigIssue(
                "error",
                f"DB_POOL_SIZE={settings.DB_POOL_SIZE} must be at least 1.",
            )
        )

    if settings.DB_MAX_OVERFLOW < 0:
        issues.append(
            ConfigIssue(
                "error",
                f"DB_MAX_OVERFLOW={settings.DB_MAX_OVERFLOW} cannot be negative.",
            )
        )

    if settings.DB_POOL_RECYCLE < 0:
        issues.append(
            ConfigIssue(
                "error",
                f"DB_POOL_RECYCLE={settings.DB_POOL_RECYCLE} cannot be negative.",
            )
        )

    if settings.DB_POOL_TIMEOUT < 0:
        issues.append(
            ConfigIssue(
                "error",
                f"DB_POOL_TIMEOUT={settings.DB_POOL_TIMEOUT} cannot be negative.",
            )
        )


def _check_multi_worker(settings: Settings, issues: list[ConfigIssue]) -> None:
    gunicorn_workers = int(os.environ.get("GUNICORN_WORKERS", "1"))
    if gunicorn_workers <= 1:
        return

    if settings.RATE_LIMIT_RPM > 0 and not settings.REDIS_URL.strip():
        issues.append(
            ConfigIssue(
                "warning",
                f"Running {gunicorn_workers} workers with RATE_LIMIT_RPM={settings.RATE_LIMIT_RPM} "
                "but REDIS_URL is empty. Rate limiting will be per-worker (not shared). "
                "Set REDIS_URL for accurate cross-worker rate limiting.",
            )
        )

    if not settings.REDIS_URL.strip():
        issues.append(
            ConfigIssue(
                "warning",
                f"Running {gunicorn_workers} workers but REDIS_URL is empty. "
                "Idempotency key cache is per-worker (not shared). "
                "Set REDIS_URL for cross-worker idempotency.",
            )
        )

    skip_sched = os.environ.get("NEXUS_SKIP_SCHEDULER", "").strip().lower()
    if skip_sched not in ("1", "true", "yes"):
        issues.append(
            ConfigIssue(
                "warning",
                f"Running {gunicorn_workers} workers but NEXUS_SKIP_SCHEDULER is not set. "
                "Each worker will run its own scheduler instance. Set NEXUS_SKIP_SCHEDULER=1 "
                "on all but one worker, or use a single scheduler process.",
            )
        )
