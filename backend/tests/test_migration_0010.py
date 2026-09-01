"""Structural and historical-compatibility coverage for Publication Identity v2."""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0010_is_additive_nullable_and_does_not_backfill_unprovable_identity(monkeypatch):
    migration = _load(
        "migration_0010",
        ROOT / "alembic" / "versions" / "0010_publication_identity_v2.py",
    )
    calls = []
    monkeypatch.setattr(migration.op, "add_column", lambda table, column: calls.append(("add", table, column)))
    monkeypatch.setattr(migration.op, "create_index", lambda *args, **kwargs: calls.append(("index", args, kwargs)))
    monkeypatch.setattr(migration.op, "create_foreign_key", lambda *args, **kwargs: calls.append(("fk", args, kwargs)))

    migration.upgrade()

    assert migration.revision == "0010"
    assert migration.down_revision == "0009"
    added = [(table, column.name, column.nullable) for kind, table, column in calls if kind == "add"]
    assert ("pin_approvals", "revision_id", True) in added
    assert ("pin_approvals", "creative_id", True) in added
    assert ("pin_publications", "approval_id", True) in added
    assert ("pin_publications", "source_image_id", True) in added
    assert ("pin_publications", "destination_url", True) in added
    assert all(kind != "execute" for kind, *_ in calls)
    foreign_keys = [entry for entry in calls if entry[0] == "fk"]
    assert foreign_keys
    assert all(entry[2]["ondelete"] == "RESTRICT" for entry in foreign_keys)


def test_phase0_schema_excludes_later_identity_columns():
    phase0 = _load(
        "migration_0001_for_0010",
        ROOT / "alembic" / "versions" / "0001_phase0_schema.py",
    ).phase0_metadata()
    approvals = phase0.tables["pin_approvals"].c
    publications = phase0.tables["pin_publications"].c
    assert "revision_id" not in approvals
    assert "creative_id" not in approvals
    assert "approval_id" not in publications
    assert "source_image_id" not in publications
