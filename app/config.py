from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    PROJECT_NAME: str = "Nexus Agent"
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    DATABASE_URL: str = "sqlite:///./nexus.db"

    NEXUS_API_KEY: str = ""
    RATE_LIMIT_RPM: int = 30
    MAX_PROMPT_LENGTH: int = 50_000

    # Comma-separated origins; empty = CORS middleware not installed (same-origin only)
    CORS_ORIGINS: str = ""
    EXPOSE_METRICS: bool = False

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


settings = Settings()
