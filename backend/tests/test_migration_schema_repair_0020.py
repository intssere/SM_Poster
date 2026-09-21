"""Task #58.1 exact-fingerprint repair certification for legacy revision 0020."""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

import app.db.migration_adoption as migration_adoption
from app.db.migration_adoption import (
    FROZEN_FINGERPRINTS,
    LEGACY_0020_FINGERPRINTS,
    SchemaAdoptionRefused,
    _catalog_contract,
    _fingerprint,
    adopt_preapplied_revision,
    repair_known_legacy_preapplied_revision,
)
from app.models.domain import PinterestPortfolioPlan, PinterestPortfolioPlanItem


POSTGRES_URL = os.getenv("TASK58_CATALOG_URL") or os.getenv("TASK58_POSTGRES_URL")
BACKEND = Path(__file__).resolve().parents[1]
OWNED_0020 = ("pinterest_portfolio_plans", "pinterest_portfolio_plan_items")


def _admin_url(url: str) -> str:
    parsed = make_url(url)
    return parsed.set(database="postgres").render_as_string(hide_password=False)


@pytest.fixture
def isolated_database() -> Iterator[str]:
    if not POSTGRES_URL:
        pytest.skip("TASK58_POSTGRES_URL is required for PostgreSQL repair tests")
    name = f"task581_{uuid4().hex[:16]}"
    admin = sa.create_engine(_admin_url(POSTGRES_URL), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
        parsed = make_url(POSTGRES_URL)
        yield parsed.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=:database AND pid <> pg_backend_pid()"
                ),
                {"database": name},
            )
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def _alembic(url: str, target: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(BACKEND)},
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout + result.stderr


def _failed_alembic(url: str, target: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(BACKEND)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    return result.stdout + result.stderr


def _set_revision(url: str, revision: str) -> None:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE alembic_version SET version_num=:revision"),
                {"revision": revision},
            )
    finally:
        engine.dispose()


def _revision(url: str) -> str:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            return str(
                connection.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _fingerprints(connection) -> dict[str, str]:
    return {
        table: _fingerprint(_catalog_contract(connection, table))
        for table in OWNED_0020
    }


def _create_legacy_tables(url: str, *, items: bool = True) -> None:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            PinterestPortfolioPlan.__table__.create(connection)
            if items:
                PinterestPortfolioPlanItem.__table__.create(connection)
    finally:
        engine.dispose()


def _insert_legacy_plan(connection) -> None:
    connection.execute(sa.text("SET LOCAL session_replication_role = replica"))
    connection.execute(
        PinterestPortfolioPlan.__table__.insert().values(
            id="plan-legacy-row",
            store_id="store-legacy-row",
            month_start=date(2026, 9, 1),
            month_end=date(2026, 9, 30),
            target_pins=1,
            existing_commitments=0,
            planned_active_slots=1,
            reserve_slots=0,
            policy_version="legacy-test",
            input_fingerprint="a" * 64,
            plan_fingerprint="b" * 64,
            status="DRAFT",
            metadata_json={},
        )
    )


def _insert_legacy_item(connection) -> None:
    connection.execute(sa.text("SET LOCAL session_replication_role = replica"))
    connection.execute(
        PinterestPortfolioPlanItem.__table__.insert().values(
            id="item-legacy-row",
            plan_id="missing-plan",
            slot_index=0,
            is_reserve=False,
            planned_date=date(2026, 9, 1),
            product_id="missing-product",
            local_board_id="missing-board",
            board_key_snapshot="legacy-board",
            content_angle_id="missing-angle",
            angle_key_snapshot="legacy-angle",
            seed_keywords=["legacy"],
            selection_score=Decimal("1.000000"),
            selection_metadata={},
            item_fingerprint="c" * 64,
            status="PLANNED",
            publication_id=None,
        )
    )


def test_current_orm_legacy_shape_matches_observed_production_fingerprints(
    isolated_database: str,
) -> None:
    _alembic(isolated_database, "0019")
    _create_legacy_tables(isolated_database)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            assert _fingerprints(connection) == LEGACY_0020_FINGERPRINTS
    finally:
        engine.dispose()


def test_known_empty_legacy_pair_repairs_and_reaches_0027(
    isolated_database: str,
) -> None:
    _alembic(isolated_database, "0019")
    _create_legacy_tables(isolated_database)

    _alembic(isolated_database, "head")

    assert _revision(isolated_database) == "0027"
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            assert {
                table: _fingerprint(_catalog_contract(connection, table))
                for table in OWNED_0020
            } == {
                table: FROZEN_FINGERPRINTS[table]
                for table in OWNED_0020
            }
    finally:
        engine.dispose()


def test_exact_canonical_empty_0020_still_adopts_without_repair(
    isolated_database: str,
) -> None:
    _alembic(isolated_database, "0020")
    _set_revision(isolated_database, "0019")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            assert repair_known_legacy_preapplied_revision(connection, "0020") is False
            assert adopt_preapplied_revision(connection, "0020") is True
    finally:
        engine.dispose()

    _alembic(isolated_database, "head")
    assert _revision(isolated_database) == "0027"


@pytest.mark.parametrize("table", OWNED_0020)
def test_nonempty_known_legacy_pair_refuses_before_repair(
    isolated_database: str,
    table: str,
) -> None:
    _alembic(isolated_database, "0019")
    _create_legacy_tables(isolated_database)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            if table == "pinterest_portfolio_plans":
                _insert_legacy_plan(connection)
            else:
                _insert_legacy_item(connection)
    finally:
        engine.dispose()

    output = _failed_alembic(isolated_database, "head")
    assert "is not empty" in output
    assert _revision(isolated_database) == "0019"


def test_partial_legacy_presence_refuses(isolated_database: str) -> None:
    _alembic(isolated_database, "0019")
    _create_legacy_tables(isolated_database, items=False)
    output = _failed_alembic(isolated_database, "head")
    assert "partial table presence" in output
    assert _revision(isolated_database) == "0019"


def test_mixed_canonical_and_legacy_fingerprints_refuse(
    isolated_database: str,
) -> None:
    _alembic(isolated_database, "0020")
    _set_revision(isolated_database, "0019")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text('DROP TABLE "public"."pinterest_portfolio_plan_items"')
            )
            PinterestPortfolioPlanItem.__table__.create(connection)
    finally:
        engine.dispose()

    output = _failed_alembic(isolated_database, "head")
    assert "not the known legacy repair contract" in output
    assert _revision(isolated_database) == "0019"


def test_unknown_legacy_drift_refuses(isolated_database: str) -> None:
    _alembic(isolated_database, "0019")
    _create_legacy_tables(isolated_database)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    'ALTER TABLE "public"."pinterest_portfolio_plans" '
                    "ADD COLUMN unexpected_drift integer"
                )
            )
    finally:
        engine.dispose()

    output = _failed_alembic(isolated_database, "head")
    assert "not the known legacy repair contract" in output
    assert _revision(isolated_database) == "0019"


def test_external_dependency_refuses_instead_of_cascading(
    isolated_database: str,
) -> None:
    _alembic(isolated_database, "0019")
    _create_legacy_tables(isolated_database)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    'CREATE TABLE "legacy_external_dependency" ('
                    "id integer PRIMARY KEY, "
                    'plan_id varchar(36) REFERENCES "pinterest_portfolio_plans"(id)'
                    ")"
                )
            )
    finally:
        engine.dispose()

    output = _failed_alembic(isolated_database, "head")
    assert "unexpected external dependencies" in output
    assert _revision(isolated_database) == "0019"


def test_locked_presence_is_rechecked_before_repair(
    isolated_database: str,
    monkeypatch,
) -> None:
    _alembic(isolated_database, "0019")
    _create_legacy_tables(isolated_database)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()

            def mutate_during_lock(bind, _owned):
                bind.execute(
                    sa.text('DROP TABLE "public"."pinterest_portfolio_plan_items"')
                )

            monkeypatch.setattr(
                migration_adoption,
                "_lock_owned_tables",
                mutate_during_lock,
            )
            with pytest.raises(SchemaAdoptionRefused, match="changed while locking"):
                repair_known_legacy_preapplied_revision(connection, "0020")
            transaction.rollback()
    finally:
        engine.dispose()


def test_failed_post_repair_contract_check_rolls_back_legacy_pair(
    isolated_database: str,
) -> None:
    _alembic(isolated_database, "0019")
    _create_legacy_tables(isolated_database)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            assert repair_known_legacy_preapplied_revision(connection, "0020") is True
            connection.execute(
                sa.text(
                    'CREATE TABLE "public"."pinterest_portfolio_plans" '
                    "(id varchar(36) PRIMARY KEY)"
                )
            )
            connection.execute(
                sa.text(
                    'CREATE TABLE "public"."pinterest_portfolio_plan_items" '
                    "(id varchar(36) PRIMARY KEY)"
                )
            )
            with pytest.raises(SchemaAdoptionRefused):
                adopt_preapplied_revision(connection, "0020")
            transaction.rollback()

        with engine.begin() as connection:
            assert _fingerprints(connection) == LEGACY_0020_FINGERPRINTS
    finally:
        engine.dispose()


def test_non_postgresql_does_not_gain_legacy_repair_behavior() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "CREATE TABLE pinterest_portfolio_plans "
                    "(id VARCHAR(36) PRIMARY KEY)"
                )
            )
            connection.execute(
                sa.text(
                    "CREATE TABLE pinterest_portfolio_plan_items "
                    "(id VARCHAR(36) PRIMARY KEY)"
                )
            )
            assert repair_known_legacy_preapplied_revision(connection, "0020") is False
            with pytest.raises(SchemaAdoptionRefused, match="requires PostgreSQL"):
                adopt_preapplied_revision(connection, "0020")
    finally:
        engine.dispose()
