"""Regression coverage for the reversible Terra pricing correction."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import create_engine, select


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0009_openai_terra_pricing.py"
)


def load_migration():
    spec = importlib.util.spec_from_file_location("migration_0009", MIGRATION_PATH)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def settings_table(metadata):
    return sa.Table(
        "ai_settings",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("pricing_metadata", sa.JSON(), nullable=False),
    )


def test_upgrade_adds_official_terra_pricing_without_overwriting_unrelated_metadata(monkeypatch):
    migration = load_migration()
    metadata = sa.MetaData()
    table = settings_table(metadata)
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    connection = engine.connect()
    original_pricing = {
        "gpt-5.6-luna": {"input_per_1m": 0.20, "output_per_1m": 1.20},
        "gpt-5.6-terra": {
            "pricing_status": "explicit_configuration_required",
            "operator_note": "keep this",
        },
        "custom-model": {"input_per_1m": 9.0, "output_per_1m": 10.0},
        "_operator_metadata": {"owner": "operator"},
    }
    connection.execute(
        table.insert().values(
            id="settings",
            pricing_metadata=original_pricing,
        )
    )
    connection.commit()
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

    migration.upgrade()

    updated = connection.execute(select(table.c.pricing_metadata)).scalar_one()
    assert updated["gpt-5.6-luna"] == original_pricing["gpt-5.6-luna"]
    assert updated["custom-model"] == original_pricing["custom-model"]
    assert updated["_operator_metadata"] == original_pricing["_operator_metadata"]
    assert updated["gpt-5.6-terra"] == {
        **original_pricing["gpt-5.6-terra"],
        "input_per_1m": 2.00,
        "output_per_1m": 12.00,
    }
    assert migration.TERRA_PRICING_MARKER in updated
    connection.close()
    engine.dispose()


def test_downgrade_restores_only_previous_terra_price_fields_and_preserves_operator_metadata(monkeypatch):
    migration = load_migration()
    metadata = sa.MetaData()
    table = settings_table(metadata)
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    connection = engine.connect()
    original_pricing = {
        "gpt-5.6-terra": {
            "input_per_1m": 1.50,
            "operator_note": "keep this",
        },
        "custom-model": {"input_per_1m": 9.0, "output_per_1m": 10.0},
    }
    connection.execute(
        table.insert().values(id="settings", pricing_metadata=original_pricing)
    )
    connection.commit()
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

    migration.upgrade()
    migration.downgrade()

    restored = connection.execute(select(table.c.pricing_metadata)).scalar_one()
    assert restored == original_pricing
    connection.close()
    engine.dispose()
