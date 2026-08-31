"""Add official Terra pricing without changing escalation policy.

Revision ID: 0009
Revises: 0008
"""

from alembic import op
import sqlalchemy as sa


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TERRA_MODEL = "gpt-5.6-terra"
TERRA_PRICING_MARKER = "_openai_terra_pricing_correction"
TERRA_PRICE_FIELDS = ("input_per_1m", "output_per_1m")
TERRA_PRICING = {"input_per_1m": 2.00, "output_per_1m": 12.00}


def _settings_table():
    return sa.table(
        "ai_settings",
        sa.column("id", sa.String(length=36)),
        sa.column("pricing_metadata", sa.JSON()),
    )


def upgrade():
    table = _settings_table()
    bind = op.get_bind()
    rows = bind.execute(sa.select(table)).mappings().all()
    for row in rows:
        pricing = dict(row["pricing_metadata"] or {})
        existing_terra = pricing.get(TERRA_MODEL)
        terra = dict(existing_terra) if isinstance(existing_terra, dict) else {}
        previous_fields = {
            field: {
                "present": field in terra,
                "value": terra.get(field),
            }
            for field in TERRA_PRICE_FIELDS
        }
        terra.update(TERRA_PRICING)
        pricing[TERRA_MODEL] = terra
        pricing[TERRA_PRICING_MARKER] = {
            "previous_fields": previous_fields,
        }
        bind.execute(
            sa.update(table)
            .where(table.c.id == row["id"])
            .values(pricing_metadata=pricing)
        )



def downgrade():
    table = _settings_table()
    bind = op.get_bind()
    rows = bind.execute(sa.select(table)).mappings().all()
    for row in rows:
        pricing = dict(row["pricing_metadata"] or {})
        marker = pricing.pop(TERRA_PRICING_MARKER, None)
        terra_value = pricing.get(TERRA_MODEL)
        terra = dict(terra_value) if isinstance(terra_value, dict) else {}
        previous_fields = (
            marker.get("previous_fields")
            if isinstance(marker, dict)
            else None
        )
        if isinstance(previous_fields, dict):
            for field in TERRA_PRICE_FIELDS:
                previous = previous_fields.get(field)
                if isinstance(previous, dict) and previous.get("present"):
                    terra[field] = previous.get("value")
                else:
                    terra.pop(field, None)
        else:
            for field in TERRA_PRICE_FIELDS:
                terra.pop(field, None)
        if terra:
            pricing[TERRA_MODEL] = terra
        else:
            pricing.pop(TERRA_MODEL, None)
        bind.execute(
            sa.update(table)
            .where(table.c.id == row["id"])
            .values(pricing_metadata=pricing)
        )