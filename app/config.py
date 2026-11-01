"""Application settings loaded from the environment and an optional local .env file."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    garmin_api_key: str = ""
    garmin_sync_enabled: bool = False
    garmin_sync_interval_seconds: int = 21600
    hevy_api_key: str = ""
    hevy_base_url: str = "https://api.hevyapp.com"
    database_path: str = "./data/fitness.db"
    database_backend: Literal["mongodb", "sqlite"] = "mongodb"
    mongodb_uri: str = "mongodb://127.0.0.1:27018"
    mongodb_database: str = "amoregymbro"
    mongodb_username: str = ""
    mongodb_password: str = ""
    mongodb_auth_source: str = "admin"
    app_password: str = ""
    app_secret: str = ""
    hevy_webhook_authorization: str = ""
    webhook_worker_enabled: bool = True
    sync_enabled: bool = True
    sync_interval_seconds: int = 21600
    hevy_page_delay_seconds: float = 0.25
    secure_cookies: bool = False
    bind_host: str = "127.0.0.1"
    app_port: int = 8787
    app_timezone: str = "America/Toronto"
    openai_api_key: str = ""
    openai_model: str = ""
    research_cache_path: str = "./data/research-cache.json"

    @property
    def garmin_configured(self) -> bool:
        return bool(self.garmin_api_key.strip())

    @property
    def hevy_configured(self) -> bool:
        return bool(self.hevy_api_key.strip())

    @property
    def auth_configured(self) -> bool:
        password = self.app_password.strip()
        secret = self.app_secret.strip()
        if password.lower() in {"", "change-me", "changeme"} or len(password) < 10:
            return False
        if secret.lower() in {"", "change-me", "changeme"} or len(secret) < 16:
            return False
        return True

    @property
    def webhook_configured(self) -> bool:
        """A callback secret is usable only when it is distinct from the Hevy API key."""
        secret = self.hevy_webhook_authorization.strip()
        api_key = self.hevy_api_key.strip()
        if secret.lower() in {"", "change-me", "changeme"} or len(secret) < 16:
            return False
        if api_key and secret == api_key:
            return False
        return True

    @property
    def model_configured(self) -> bool:
        key = self.openai_api_key.strip()
        return key.lower() not in {"", "change-me", "changeme"}

    @property
    def resolved_openai_model(self) -> str:
        """Small current cloud model. Override with OPENAI_MODEL in the local .env."""
        name = self.openai_model.strip()
        return name or "gpt-5.6-luna"

    @property
    def redacted_secrets(self) -> list[str]:
        values = [
            self.garmin_api_key.strip(),
            self.hevy_api_key.strip(),
            self.app_password.strip(),
            self.app_secret.strip(),
            self.hevy_webhook_authorization.strip(),
            self.openai_api_key.strip(),
            self.mongodb_uri.strip(),
            self.mongodb_password.strip(),
        ]
        return [value for value in values if value]
