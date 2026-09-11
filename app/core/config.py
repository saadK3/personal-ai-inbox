from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables and .env."""

    app_name: str = "Personal AI Inbox"
    environment: str = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/personal_inbox"
    discord_bot_token: SecretStr | None = None
    discord_allowed_user_id: int | None = None
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5.6-luna"
    openai_vision_model: str = "gpt-5.6-luna"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dimensions: int = 1536
    openai_transcription_model: str = "gpt-4o-mini-transcribe"
    enrichment_max_attempts: int = 3
    storage_dir: Path = Path("./data")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        env_ignore_empty=True,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
