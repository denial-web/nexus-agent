from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    PROJECT_NAME: str = "Nexus Agent"
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    DATABASE_URL: str = "sqlite:///./nexus.db"

    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE: int = 1800  # seconds; 0 = disabled
    DB_POOL_PRE_PING: bool = True
    DB_POOL_TIMEOUT: int = 30  # seconds to wait for a connection from pool

    NEXUS_API_KEY: str = ""
    RATE_LIMIT_RPM: int = 30
    MAX_PROMPT_LENGTH: int = 50_000
    MAX_REQUEST_BODY_BYTES: int = 10_485_760  # 10 MB
    REQUEST_TIMEOUT_SECONDS: float = 120.0
    SHUTDOWN_DRAIN_SECONDS: float = 30.0

    # Legacy /api/ route deprecation (RFC 8594); empty = no Sunset header
    API_LEGACY_SUNSET: str = ""  # ISO date, e.g. "2026-12-31"

    # Comma-separated origins; empty = CORS middleware not installed (same-origin only)
    CORS_ORIGINS: str = ""
    CORS_ALLOW_METHODS: str = "GET,POST,PUT,DELETE,OPTIONS"
    CORS_ALLOW_HEADERS: str = "Content-Type,X-API-Key,X-Request-ID,Authorization"
    CORS_MAX_AGE: int = 600  # preflight cache seconds
    EXPOSE_METRICS: bool = False
    HEALTH_PROBE_TIMEOUT: float = 5.0  # per-provider deep-check timeout in seconds

    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_MODEL: str = "deepseek-chat"
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"

    # Local / HuggingFace (Nexus Spin v5.3 — use model_id local:repo/name or nexus-spin-v5.3)
    LOCAL_HF_MODEL_ID: str = ""
    LOCAL_HF_DEVICE: str = "cpu"

    # Covernor governance
    APPROVAL_QUORUM: int = 2
    ECDSA_PRIVATE_KEY_PATH: str = ""

    # A-S-FLC defaults
    ASFLC_UNCERTAINTY_DELTA: float = 0.15
    ASFLC_MAX_LOOPS: int = 10
    ASFLC_CONVERGENCE_THRESHOLD: float = 0.01

    # Multi-model compare
    COMPARE_TIMEOUT_SECONDS: float = 30.0
    COMPARE_MAX_MODELS: int = 5

    # Circuit breaker for LLM providers
    CB_FAILURE_THRESHOLD: int = 5
    CB_RECOVERY_TIMEOUT: float = 30.0
    CB_WINDOW_SECONDS: float = 60.0
    CB_FALLBACK_TO_MOCK: bool = True

    # LLM response cache (exact-match, in-process)
    LLM_CACHE_ENABLED: bool = False
    LLM_CACHE_TTL: float = 300.0
    LLM_CACHE_MAX_ENTRIES: int = 1000

    # Critic defaults
    CRITIC_MODEL: str = ""
    CRITIC_CHUNK_SIZE: int = 64
    CRITIC_MAX_ROLLBACKS: int = 3

    # Doctrine Lab integration
    DOCTRINE_LAB_URL: str = "http://localhost:8000"
    DOCTRINE_LAB_API_KEY: str = ""

    # Data retention (days; 0 = no automatic purge)
    RETENTION_TRACE_DAYS: int = 0
    RETENTION_LABELING_DAYS: int = 0
    RETENTION_APPROVAL_DAYS: int = 0
    RETENTION_CALIBRATION_DAYS: int = 0

    # Dashboard — session signing (always set in production)
    SESSION_SECRET: str = ""
    ENFORCE_DASHBOARD_CSRF: bool = False

    _DEV_SESSION_SECRET: str = "dev-nexus-session-not-for-production"

    def get_session_secret(self) -> str:
        """Return the configured session secret, or dev fallback in dev/test."""
        return self.SESSION_SECRET.strip() or self._DEV_SESSION_SECRET

    # Agentic loop (Phase 8+)
    AGENT_MAX_STEPS: int = 15
    AGENT_WORKSPACE: str = ""  # empty = cwd
    AGENT_SHELL_TIMEOUT: float = 30.0
    AGENT_TOOL_OUTPUT_MAX_CHARS: int = 24_000
    AGENT_REFLECT_ON_SUCCESS: bool = True
    AGENT_CRITIC_EVERY_N_STEPS: int = 1
    AGENT_USE_ASFLC: bool = False
    TAVILY_API_KEY: str = ""
    SERPAPI_API_KEY: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_POLL_TIMEOUT: int = 30
    NEXUS_API_BASE_URL: str = "http://127.0.0.1:9000"

    # Local-only mode: block external LLM/tool network (Phase 10)
    LOCAL_ONLY: bool = False

    # Ollama (OpenAI-compatible API) — use model_id `ollama:your-model`
    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434/v1"
    OLLAMA_API_KEY: str = "ollama"  # OpenAI client requires non-empty dummy key
    OLLAMA_DEFAULT_MODEL: str = "llama3.2"
    OLLAMA_LIST_IN_PROVIDERS: bool = False  # when True, include in get_available_providers()

    # Webhooks
    WEBHOOKS_ENABLED: bool = False
    WEBHOOK_WORKERS: int = 2
    WEBHOOK_MAX_RETRIES: int = 3
    WEBHOOK_BACKOFF_BASE: float = 1.0
    WEBHOOK_BACKOFF_MAX: float = 30.0
    WEBHOOK_REQUEST_TIMEOUT: float = 10.0
    WEBHOOK_MAX_CONSECUTIVE_FAILURES: int = 10

    # Idempotency key support
    IDEMPOTENCY_TTL: int = 86400  # seconds; cached response lifetime
    IDEMPOTENCY_MAX_KEYS: int = 10000  # max keys in in-process store

    # Redis (multi-worker rate limiting, idempotency store)
    REDIS_URL: str = ""

    # OpenTelemetry distributed tracing
    OTEL_ENABLED: bool = False
    OTEL_SERVICE_NAME: str = "nexus-agent"
    OTEL_EXPORTER_ENDPOINT: str = "http://localhost:4318"
    OTEL_SAMPLE_RATE: float = 1.0

    # MCP governance proxy (Phase 11)
    MCP_ENABLED: bool = False
    MCP_BACKENDS_FILE: str = "mcp_backends.json"
    MCP_DEFAULT_POLICY: str = "deny"  # informational; policies live in DB
    MCP_AUDIT_ALL: bool = True  # False = trace only denied/blocked/immune-block

    # Memory / Belief system (Phase 12) — OFF by default to guarantee zero regression
    # See MEMORY_FLAGSHIP_PLAN.md for design. Governed, bitemporal, Beta-confidence beliefs.
    MEMORY_ENABLED: bool = False
    EXTRACTION_MODEL: str = ""  # empty = reuse default provider/model chain
    # Per-entity-type skepticism stakes. Format: "type=threshold,type=threshold"
    # Higher stakes require higher confidence to accept. See app/core/memory/skepticism.py.
    MEMORY_STAKES_THRESHOLDS: str = "identity=0.9,financial=0.85,preference=0.5,state=0.3"
    # Per-entity-type decay profile. Format: "type=half_life". Use "inf" for no decay.
    # Units: d=days, h=hours, m=minutes. See app/core/memory/forgetting.py.
    MEMORY_DECAY_PROFILE: str = "identity=inf,preference=180d,state=4h,context=1h"
    MEMORY_RETRIEVAL_LIMIT: int = 5  # default k for RRF retrieval
    MEMORY_EXTRACTOR_MAX_CHARS: int = 8_000  # cap input to extractor for cost control


settings = Settings()
