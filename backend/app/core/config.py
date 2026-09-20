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
    public_media_base_url: str | None = None
    buffer_api_key: str | None = Field(default=None, repr=False, exclude=True)
    buffer_api_base: str = "https://api.buffer.com"
    buffer_organization_id: str | None = None
    buffer_pinterest_channel_id: str | None = None
    buffer_publishing_enabled: bool = False
    buffer_single_pin_pilot_enabled: bool = False
    buffer_single_pin_pilot_publication_id: str = ""
    buffer_single_pin_pilot_publication_fingerprint: str = ""
    buffer_single_pin_pilot_request_fingerprint: str = ""
    routine_pinterest_worker_enabled: bool = False
    routine_buffer_dispatch_enabled: bool = False
    routine_pinterest_dry_run: bool = True
    routine_pinterest_batch_size: int = Field(default=1, ge=1, le=25)
    routine_pinterest_daily_write_limit: int = Field(default=1, ge=1, le=25)
    routine_claim_stale_seconds: int = Field(default=900, ge=60, le=86400)
    routine_pinterest_scheduler_enabled: bool = False
    routine_pinterest_scheduler_interval_seconds: int = Field(default=300, ge=60, le=86400)
    routine_autonomous_authorization_enabled: bool = False
    pinterest_portfolio_planner_enabled: bool = False
    pinterest_monthly_pin_target: int = Field(default=150, ge=1, le=10000)
    pinterest_portfolio_max_pins_per_product: int = Field(default=3, ge=1, le=100)
    pinterest_portfolio_max_vendor_share: float = Field(default=0.25, gt=0.0, le=1.0)
    pinterest_portfolio_max_board_share: float = Field(default=0.40, gt=0.0, le=1.0)
    pinterest_portfolio_reserve_percentage: float = Field(default=0.10, ge=0.0, le=1.0)
    pinterest_seo_brief_persistence_enabled: bool = False
    pinterest_seo_max_secondary_keywords: int = Field(default=5, ge=1, le=12)
    pinterest_autonomous_generation_enabled: bool = False
    pinterest_analytics_ingestion_enabled: bool = False
    pinterest_learning_snapshot_persistence_enabled: bool = False
    pinterest_learning_prior_impressions: int = Field(default=500, ge=1, le=1000000)
    pinterest_learning_min_total_publications: int = Field(default=10, ge=1, le=100000)
    pinterest_learning_min_dimension_samples: int = Field(default=3, ge=1, le=10000)
    pinterest_optimizer_enabled: bool = False
    pinterest_optimizer_exploit_share: float = Field(default=0.70, ge=0.0, le=1.0)
    pinterest_write_scope_enabled: bool = False
    pinterest_board_write_scope_enabled: bool = False
    pinterest_board_provisioning_enabled: bool = False
    pinterest_single_pin_pilot_enabled: bool = False
    pinterest_single_pin_pilot_publication_id: str = ""
    pinterest_single_pin_pilot_publication_fingerprint: str = ""
    pinterest_single_pin_pilot_request_fingerprint: str = ""
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
    frontend_return_url: str = "http://localhost:5000/#channels"

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
