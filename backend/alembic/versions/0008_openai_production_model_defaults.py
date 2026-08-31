"""Set controlled OpenAI model defaults and Luna pricing metadata.

Revision ID: 0008
Revises: 0007
"""

from alembic import op
import sqlalchemy as sa


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None
MODEL_UPGRADE_MARKER = "_openai_production_model_upgrade"


def _settings_table():
    return sa.table(
        "ai_settings",
        sa.column("id", sa.String(length=36)),
        sa.column("hosted_model", sa.String(length=120)),
        sa.column("image_model", sa.String(length=120)),
        sa.column("pricing_metadata", sa.JSON()),
    )


def upgrade():
    table = _settings_table()
    bind = op.get_bind()
    rows = bind.execute(sa.select(table)).mappings().all()
    for row in rows:
        pricing = dict(row["pricing_metadata"] or {})
        pricing.setdefault(
            "gpt-5.6-luna",
            {"input_per_1m": 0.20, "output_per_1m": 1.20},
        )
        pricing.setdefault(
            "gpt-5.6-terra",
            {"pricing_status": "explicit_configuration_required"},
        )
        marker = dict(pricing.get(MODEL_UPGRADE_MARKER) or {})
        values = {"pricing_metadata": pricing}
        # Only migrate untouched legacy defaults. Explicit operator choices
        # must not be overwritten by a model upgrade.
        if row["hosted_model"] == "gpt-4o-mini":
            values["hosted_model"] = "gpt-5.6-luna"
            marker["hosted_model_from"] = "gpt-4o-mini"
        if row["image_model"] == "gpt-image-1":
            values["image_model"] = "gpt-image-2"
            marker["image_model_from"] = "gpt-image-1"
        if marker:
            pricing[MODEL_UPGRADE_MARKER] = marker
        bind.execute(
            sa.update(table).where(table.c.id == row["id"]).values(**values)
        )


def downgrade():
    table = _settings_table()
    bind = op.get_bind()
    rows = bind.execute(sa.select(table)).mappings().all()
    for row in rows:
        pricing = dict(row["pricing_metadata"] or {})
        marker = dict(pricing.pop(MODEL_UPGRADE_MARKER, {}) or {})
        pricing.pop("gpt-5.6-luna", None)
        pricing.pop("gpt-5.6-terra", None)
        values = {"pricing_metadata": pricing}
        if row["hosted_model"] == "gpt-5.6-luna" and marker.get("hosted_model_from") == "gpt-4o-mini":
            values["hosted_model"] = marker["hosted_model_from"]
        if row["image_model"] == "gpt-image-2" and marker.get("image_model_from") == "gpt-image-1":
            values["image_model"] = marker["image_model_from"]
        bind.execute(
            sa.update(table).where(table.c.id == row["id"]).values(**values)
        )