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

    object_storage_endpoint: str | None = None
    object_storage_bucket: str = "diamond-shelf-pinterest"
    object_storage_access_key: str | None = None
    object_storage_secret_key: str | None = None

    ai_provider: str = "none"
    openai_api_key: str | None = None


@lru_cache

def get_settings() -> Settings:
    return Settings()
