"""Reviewed Replit 0031 inventory; not an ORM-derived or runtime-expanded allowlist."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TablePolicy:
    count: int
    category: str
    action: str = "preserve"


# Exact inventory observed 2026-10-03. A changed inventory requires a new review.
SOURCE = {
    "ai_generated_assets": TablePolicy(0, "business"),
    "ai_request_telemetry": TablePolicy(5, "history"),
    "ai_settings": TablePolicy(1, "business_configuration"),
    "audit_logs": TablePolicy(18, "history"),
    "boards": TablePolicy(10, "business"),
    "buffer_pilot_activations": TablePolicy(4, "historical_authorization"),
    "campaigns": TablePolicy(1, "business"),
    "catalog_sync_jobs": TablePolicy(1, "history"),
    "content_angles": TablePolicy(9, "business"),
    "content_revisions": TablePolicy(5, "business"),
    "content_version_selections": TablePolicy(1, "business"),
    "creative_templates": TablePolicy(4, "business"),
    "integration_accounts": TablePolicy(0, "provider_connection"),
    "keyword_clusters": TablePolicy(29, "business"),
    "pin_approvals": TablePolicy(8, "business_history"),
    "pin_concepts": TablePolicy(45, "business"),
    "pin_creatives": TablePolicy(17, "business_media_reference"),
    "pin_drafts": TablePolicy(45, "business"),
    "pin_publications": TablePolicy(9, "business_provider_history"),
    "pinterest_analytics_ingestion_runs": TablePolicy(0, "history"),
    "pinterest_analytics_snapshots": TablePolicy(0, "business_history"),
    "pinterest_autonomous_destination_runs": TablePolicy(3, "history"),
    "pinterest_autonomous_execution_runs": TablePolicy(3, "history"),
    "pinterest_autonomous_generation_runs": TablePolicy(2, "history"),
    "pinterest_autonomous_run_reconciliations": TablePolicy(1, "history"),
    "pinterest_board_provisioning_attempts": TablePolicy(0, "provider_history"),
    "pinterest_board_sections": TablePolicy(0, "provider_snapshot"),
    "pinterest_boards": TablePolicy(33, "provider_snapshot_route"),
    "pinterest_connections": TablePolicy(4, "provider_credential_connection"),
    "pinterest_learning_snapshots": TablePolicy(0, "business_history"),
    "pinterest_oauth_states": TablePolicy(5, "ephemeral", "exclude"),
    "pinterest_optimizer_applications": TablePolicy(1, "business_history"),
    "pinterest_portfolio_plan_items": TablePolicy(163, "business"),
    "pinterest_portfolio_plans": TablePolicy(1, "business"),
    "pinterest_seo_briefs": TablePolicy(1, "business"),
    "product_images": TablePolicy(2997, "business"),
    "product_intelligence": TablePolicy(2997, "business"),
    "product_variants": TablePolicy(2997, "business"),
    "products": TablePolicy(2997, "business"),
    "publication_attempts": TablePolicy(9, "provider_history"),
    "publication_dispatch_authorizations": TablePolicy(13, "historical_authorization"),
    "publication_reconciliation_events": TablePolicy(7, "provider_history"),
    "routine_attempt_boundaries": TablePolicy(3, "execution_history"),
    "routine_dispatch_permits": TablePolicy(4, "historical_authorization"),
    "routine_publishing_control": TablePolicy(1, "paused_control"),
    "routine_publishing_runs": TablePolicy(4, "history"),
    "routine_scheduled_quota_reservations": TablePolicy(0, "historical_quota"),
    "stores": TablePolicy(1, "business"),
}
TARGET_ONLY = {
    "management_readiness_admissions": "0032/0033: empty; never manufacture",
    "management_readiness_outcomes": "0033: empty; never manufacture",
    "routine_autonomous_batches": "0034: empty; never manufacture",
    "routine_autonomous_batch_entries": "0034: empty; never manufacture",
}
EXCLUDED_INFRASTRUCTURE = {
    "public.alembic_version": "target migration bookkeeping, never copy or stamp",
    "_system.*": "Replit infrastructure, never copy",
}
PRESERVED = tuple(name for name, rule in SOURCE.items() if rule.action == "preserve")
AUTHORIZATION_TABLES = (
    "buffer_pilot_activations", "publication_dispatch_authorizations",
    "routine_dispatch_permits",
)
OPAQUE_COLUMNS = {
    "pinterest_connections": ("access_token_ciphertext", "refresh_token_ciphertext"),
    "integration_accounts": ("encrypted_credentials",),
}
FORMAT = "closed-state-replit-0031-railway-0034-v1"

# Exact FK metadata from the production inventory. Historical Phase 0 column
# cloning omits these in the fresh executable target. Do not repair historical
# migrations here; pin both catalogs and enforce these edges in transfer.
# child, column, parent, constraint name, ON DELETE
FOUNDATION_FKS = (
    ("products", "store_id", "stores", "products_store_id_fkey", "CASCADE"),
    ("pin_creatives", "draft_id", "pin_drafts", "pin_creatives_draft_id_fkey", "CASCADE"),
    ("pin_creatives", "source_image_id", "product_images", "pin_creatives_source_image_id_fkey", None),
    ("pin_creatives", "template_id", "creative_templates", "pin_creatives_template_id_fkey", None),
    ("pin_approvals", "draft_id", "pin_drafts", "pin_approvals_draft_id_fkey", "CASCADE"),
    ("pin_publications", "draft_id", "pin_drafts", "pin_publications_draft_id_fkey", "CASCADE"),
    ("pin_publications", "creative_id", "pin_creatives", "pin_publications_creative_id_fkey", None),
    ("pin_publications", "board_id", "boards", "pin_publications_board_id_fkey", None),
)