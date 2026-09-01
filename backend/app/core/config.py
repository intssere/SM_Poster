from functools import lru_cache
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    app_env: str = "development"
    database_url: str = Field(
        validation_alias=AliasChoices("DATABASE_URL", "REPLIT_DB_URL")
    )
    app_secret_key: str = Field(
        default="",
        validation_alias=AliasChoices("APP_SECRET_KEY", "SESSION_SECRET"),
    )
    publishing_enabled: bool = False
    auth_disabled: bool = False
    admin_username: str | None = Field(default=None, validation_alias=AliasChoices("ADMIN_USERNAME", "AUTH_ADMIN_USERNAME"))
    admin_password_hash: str | None = Field(default=None, validation_alias=AliasChoices("ADMIN_PASSWORD_HASH", "AUTH_ADMIN_PASSWORD_HASH"))
    auth_session_ttl_seconds: int = 3600
    auth_allowed_origins: str = ""

    shopify_shop: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SHOPIFY_SHOP", "SHOPIFY_SHOP_DOMAIN"),
    )
    shopify_client_id: str | None = None
    shopify_client_secret: str | None = None
    shopify_access_token: str | None = None
    shopify_api_version: str = "2026-07"

    pinterest_client_id: str | None = None
    pinterest_client_secret: str | None = None
    pinterest_redirect_uri: str | None = None
    pinterest_api_base: str = "https://api.pinterest.com/v5"
    pinterest_token_encryption_key: str | None = None

    object_storage_endpoint: str | None = None
    object_storage_bucket: str = "diamond-shelf-pinterest"
    object_storage_access_key: str | None = None
    object_storage_secret_key: str | None = None

    ai_provider: str = "none"
    openai_api_key: str | None = None

    @property
    def is_exposed(self) -> bool:
        import os
        return self.app_env.lower() in {"production", "prod", "replit"} or bool(os.getenv("REPLIT_DEPLOYMENT")) or bool(os.getenv("REPLIT_DEV_DOMAIN"))

    @property
    def allowed_origins(self) -> list[str]:
        import os
        configured = [item.strip().rstrip("/") for item in self.auth_allowed_origins.split(",") if item.strip()]
        if configured:
            return configured
        domain = os.getenv("REPLIT_DEV_DOMAIN")
        if domain:
            return [f"https://{domain.rstrip('/')}" ]
        return ["http://localhost:5000", "http://127.0.0.1:5000"]


@lru_cache

def get_settings() -> Settings:
    return Settings()
