from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    PROJECT_NAME: str = "Nexus Agent"
    DATABASE_URL: str = "sqlite:///./nexus.db"

    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.0-flash"
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"

    # Covernor governance
    APPROVAL_QUORUM: int = 2
    ECDSA_PRIVATE_KEY_PATH: str = ""

    # A-S-FLC defaults
    ASFLC_UNCERTAINTY_DELTA: float = 0.15
    ASFLC_MAX_LOOPS: int = 10
    ASFLC_CONVERGENCE_THRESHOLD: float = 0.01

    # Critic defaults
    CRITIC_MODEL: str = ""
    CRITIC_CHUNK_SIZE: int = 64
    CRITIC_MAX_ROLLBACKS: int = 3

    # Doctrine Lab integration
    DOCTRINE_LAB_URL: str = "http://localhost:8000"
    DOCTRINE_LAB_API_KEY: str = ""


settings = Settings()
