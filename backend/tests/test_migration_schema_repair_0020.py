"""Task #58.1 exact-fingerprint repair certification for legacy revision 0020."""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

import app.db.migration_adoption as migration_adoption
from app.core.config import get_settings
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
REAL_FINGERPRINT = _fingerprint


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


@contextmanager
def _database_environment(url: str):
    old_database_url = os.environ.get("DATABASE_URL")
    old_cwd = Path.cwd()
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    os.chdir(BACKEND)
    try:
        yield
    finally:
        os.chdir(old_cwd)
        if old_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old_database_url
        get_settings.cache_clear()


def _alembic_in_process(url: str, target: str) -> None:
    # Running in-process is deliberate: tests can replace only the fingerprint
    # function while Alembic executes the real 0020 migration and all later
    # revisions against an isolated PostgreSQL database.
    with _database_environment(url):
        command.upgrade(Config(str(BACKEND / "alembic.ini")), target)


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


def _real_fingerprints(connection) -> dict[str, str]:
    return {
        table: REAL_FINGERPRINT(_catalog_contract(connection, table))
        for table in OWNED_0020
    }


def _create_proxy_legacy_tables(url: str, *, items: bool = True) -> None:
    """Create disposable tables used only to exercise the repair control flow.

    Their real fingerprints are intentionally *not* accepted by production
    code. Tests alias exactly these isolated fingerprints to the two observed
    production hashes inside the pytest process.
    """
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            PinterestPortfolioPlan.__table__.create(connection)
            if items:
                PinterestPortfolioPlanItem.__table__.create(connection)
    finally:
        engine.dispose()


def _alias_proxy_to_production_fingerprints(url: str, monkeypatch) -> dict[str, str]:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            proxy = _real_fingerprints(connection)
    finally:
        engine.dispose()

    aliases = {
        proxy[table]: LEGACY_0020_FINGERPRINTS[table]
        for table in OWNED_0020
    }

    def aliased_fingerprint(contract):
        real = REAL_FINGERPRINT(contract)
        return aliases.get(real, real)

    monkeypatch.setattr(
        migration_adoption,
        "_fingerprint",
        aliased_fingerprint,
    )
    return proxy


def _insert_proxy_plan(connection) -> None:
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


def _insert_proxy_item(connection) -> None:
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


def test_only_observed_production_fingerprints_are_allowlisted() -> None:
    assert LEGACY_0020_FINGERPRINTS == {
        "pinterest_portfolio_plans":
            "9a91ae0ff2d722c7e12c23e236698cfd71259308e1ef3a42e4782075754129bf",
        "pinterest_portfolio_plan_items":
            "3085a01cf1385f9ed5cab605f236c7eceba49fab8f7ae4d6eac5e63e63f61274",
    }


def test_exact_production_fingerprint_pair_repairs_and_reaches_current_head(
    isolated_database: str,
    monkeypatch,
) -> None:
    _alembic(isolated_database, "0019")
    _create_proxy_legacy_tables(isolated_database)
    _alias_proxy_to_production_fingerprints(isolated_database, monkeypatch)

    _alembic_in_process(isolated_database, "head")

    assert _revision(isolated_database) == "0029"
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            assert _real_fingerprints(connection) == {
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
    assert _revision(isolated_database) == "0029"


@pytest.mark.parametrize("table", OWNED_0020)
def test_nonempty_exact_legacy_pair_refuses_before_repair(
    isolated_database: str,
    monkeypatch,
    table: str,
) -> None:
    _alembic(isolated_database, "0019")
    _create_proxy_legacy_tables(isolated_database)
    _alias_proxy_to_production_fingerprints(isolated_database, monkeypatch)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            if table == "pinterest_portfolio_plans":
                _insert_proxy_plan(connection)
            else:
                _insert_proxy_item(connection)
            with pytest.raises(SchemaAdoptionRefused, match="is not empty"):
                repair_known_legacy_preapplied_revision(connection, "0020")
            transaction.rollback()
    finally:
        engine.dispose()


def test_partial_legacy_presence_refuses(isolated_database: str) -> None:
    _alembic(isolated_database, "0019")
    _create_proxy_legacy_tables(isolated_database, items=False)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            with pytest.raises(SchemaAdoptionRefused, match="partial table presence"):
                repair_known_legacy_preapplied_revision(connection, "0020")
    finally:
        engine.dispose()


def test_mixed_canonical_and_exact_legacy_fingerprints_refuse(
    isolated_database: str,
    monkeypatch,
) -> None:
    _alembic(isolated_database, "0020")
    _set_revision(isolated_database, "0019")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            canonical_plan = REAL_FINGERPRINT(
                _catalog_contract(connection, "pinterest_portfolio_plans")
            )
            connection.execute(
                sa.text('DROP TABLE "public"."pinterest_portfolio_plan_items"')
            )
            PinterestPortfolioPlanItem.__table__.create(connection)
            proxy_item = REAL_FINGERPRINT(
                _catalog_contract(connection, "pinterest_portfolio_plan_items")
            )
        assert canonical_plan == FROZEN_FINGERPRINTS["pinterest_portfolio_plans"]

        def mixed_fingerprint(contract):
            real = REAL_FINGERPRINT(contract)
            if real == proxy_item:
                return LEGACY_0020_FINGERPRINTS["pinterest_portfolio_plan_items"]
            return real

        monkeypatch.setattr(
            migration_adoption,
            "_fingerprint",
            mixed_fingerprint,
        )
        with engine.begin() as connection:
            with pytest.raises(
                SchemaAdoptionRefused,
                match="not the known legacy repair contract",
            ):
                repair_known_legacy_preapplied_revision(connection, "0020")
    finally:
        engine.dispose()


def test_unknown_drift_refuses(isolated_database: str, monkeypatch) -> None:
    _alembic(isolated_database, "0019")
    _create_proxy_legacy_tables(isolated_database)
    _alias_proxy_to_production_fingerprints(isolated_database, monkeypatch)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    'ALTER TABLE "public"."pinterest_portfolio_plans" '
                    "ADD COLUMN unexpected_drift integer"
                )
            )
            with pytest.raises(
                SchemaAdoptionRefused,
                match="not the known legacy repair contract",
            ):
                repair_known_legacy_preapplied_revision(connection, "0020")
    finally:
        engine.dispose()


def test_external_dependency_refuses_instead_of_cascading(
    isolated_database: str,
    monkeypatch,
) -> None:
    _alembic(isolated_database, "0019")
    _create_proxy_legacy_tables(isolated_database)
    _alias_proxy_to_production_fingerprints(isolated_database, monkeypatch)
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
            with pytest.raises(
                SchemaAdoptionRefused,
                match="unexpected external dependencies",
            ):
                repair_known_legacy_preapplied_revision(connection, "0020")
    finally:
        engine.dispose()


def test_locked_presence_is_rechecked_before_repair(
    isolated_database: str,
    monkeypatch,
) -> None:
    _alembic(isolated_database, "0019")
    _create_proxy_legacy_tables(isolated_database)
    _alias_proxy_to_production_fingerprints(isolated_database, monkeypatch)
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
    monkeypatch,
) -> None:
    _alembic(isolated_database, "0019")
    _create_proxy_legacy_tables(isolated_database)
    proxy_before = _alias_proxy_to_production_fingerprints(
        isolated_database,
        monkeypatch,
    )
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
            assert _real_fingerprints(connection) == proxy_before
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
