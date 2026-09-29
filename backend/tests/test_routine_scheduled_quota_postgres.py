"""Real PostgreSQL concurrency contracts for scheduled quota reservations."""
from __future__ import annotations

import getpass
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.models.routine_publishing import (
    RoutinePublishingControl,
    RoutineScheduledQuotaReservation,
)
from app.services.routine_scheduled_quotas import (
    ScheduledQuotaError,
    ScheduledQuotaLimits,
    assess_scheduled_quota,
    reserve_scheduled_quota,
)


def _schema_name() -> str:
    return f"routine_quota_{uuid4().hex}"


@pytest.fixture(scope="module")
def local_postgres_url() -> Iterator[sa.engine.URL]:
    """Start an isolated, disposable, Unix-socket-only PostgreSQL cluster."""
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if not initdb or not pg_ctl:
        pytest.skip("local PostgreSQL initdb and pg_ctl binaries are required")

    with tempfile.TemporaryDirectory(prefix="routine-quota-postgres-") as temp_dir:
        root = Path(temp_dir)
        data_dir = root / "data"
        socket_dir = root / "socket"
        socket_dir.mkdir(mode=0o700)
        log_path = root / "postgres.log"
        user = getpass.getuser()
        try:
            subprocess.run(
                [
                    initdb,
                    "-D",
                    str(data_dir),
                    "-U",
                    user,
                    "--auth-local=trust",
                    "--auth-host=reject",
                    "--no-instructions",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("could not initialize the disposable local PostgreSQL cluster")

        options = (
            "-c listen_addresses='' "
            f"-c unix_socket_directories={shlex.quote(str(socket_dir))} "
            "-c port=55493 -c unix_socket_permissions=0700"
        )
        started = False
        try:
            try:
                subprocess.run(
                    [
                        pg_ctl,
                        "-D",
                        str(data_dir),
                        "-l",
                        str(log_path),
                        "-o",
                        options,
                        "-w",
                        "start",
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                started = True
            except (OSError, subprocess.CalledProcessError):
                # A failed start may still have spawned a backend; attempt an
                # immediate stop before reporting the unavailable local server.
                subprocess.run(
                    [pg_ctl, "-D", str(data_dir), "-m", "immediate", "-w", "stop"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                pytest.skip("could not start the disposable local PostgreSQL cluster")

            yield sa.engine.URL.create(
                "postgresql+psycopg",
                username=user,
                database="postgres",
                query={"host": str(socket_dir), "port": "55493"},
            )
        finally:
            if started:
                subprocess.run(
                    [pg_ctl, "-D", str(data_dir), "-m", "immediate", "-w", "stop"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )


@pytest.fixture
def isolated_postgres(
    local_postgres_url: sa.engine.URL,
) -> Iterator[tuple[sa.Engine, str, dict[str, sa.Table]]]:
    schema = _schema_name()
    admin = sa.create_engine(local_postgres_url, isolation_level="AUTOCOMMIT")
    schema_created = False
    try:
        try:
            with admin.connect() as connection:
                connection.execute(sa.text("SELECT 1"))
                connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
                schema_created = True
        except sa.exc.SQLAlchemyError:
            pytest.skip("disposable local PostgreSQL is unavailable or cannot create an isolated schema")

        engine = sa.create_engine(
            local_postgres_url,
            pool_pre_ping=True,
            execution_options={"schema_translate_map": {None: schema}},
        )
        stub_metadata = sa.MetaData(schema=schema)
        stub_tables = {
            name: sa.Table(
                name,
                stub_metadata,
                sa.Column("id", sa.String(36), primary_key=True),
            )
            for name in (
                "pin_publications",
                "pinterest_portfolio_plans",
                "pinterest_portfolio_plan_items",
                "products",
                "boards",
            )
        }
        try:
            # The quota ORM tables have foreign keys to these otherwise-unused
            # records. Create only their minimal key tables, never the full app schema.
            stub_metadata.create_all(engine)
            Base.metadata.create_all(
                engine,
                tables=[
                    RoutinePublishingControl.__table__,
                    RoutineScheduledQuotaReservation.__table__,
                ],
            )
            with engine.begin() as connection:
                connection.execute(
                    RoutinePublishingControl.__table__.insert().values(
                        id="default", state="PAUSED"
                    )
                )
            yield engine, schema, stub_tables
        finally:
            engine.dispose()
    finally:
        if schema_created:
            with admin.connect() as connection:
                connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def _identity(
    label: str,
    *,
    day: date = date(2026, 4, 10),
    product: str | None = None,
    vendor: str | None = None,
    board: str | None = None,
) -> dict:
    return {
        "publication_id": f"pub-{label}",
        "plan_id": f"plan-{label}",
        "plan_item_id": f"item-{label}",
        "product_id": product or f"product-{label}",
        "vendor_key": vendor or f"vendor-{label}",
        "board_id": board or f"board-{label}",
        "scheduled_for": day,
    }


def _seed_references(
    engine: sa.Engine,
    stub_tables: dict[str, sa.Table],
    *identities: dict,
) -> None:
    field_table = {
        "publication_id": "pin_publications",
        "plan_id": "pinterest_portfolio_plans",
        "plan_item_id": "pinterest_portfolio_plan_items",
        "product_id": "products",
        "board_id": "boards",
    }
    with engine.begin() as connection:
        for identity in identities:
            for field, table_name in field_table.items():
                connection.execute(
                    pg_insert(stub_tables[table_name])
                    .values(id=identity[field])
                    .on_conflict_do_nothing(index_elements=["id"])
                )


def _reserve(db: Session, identity: dict, limits: ScheduledQuotaLimits):
    return reserve_scheduled_quota(db, **identity, limits=limits)


def _limits_for(dimension: str) -> ScheduledQuotaLimits:
    return ScheduledQuotaLimits(
        daily=1 if dimension == "daily" else 20,
        monthly=1 if dimension == "monthly" else 20,
        product=1 if dimension == "product" else 20,
        vendor=1 if dimension == "vendor" else 20,
        board=1 if dimension == "board" else 20,
    )


def _wait_for_control_row_lock(engine: sa.Engine, pid: int, contender_future) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with engine.connect() as monitor:
            wait_type = monitor.execute(
                sa.text(
                    "SELECT wait_event_type FROM pg_stat_activity "
                    "WHERE pid = :pid AND query ILIKE '%routine_publishing_control%'"
                ),
                {"pid": pid},
            ).scalar_one_or_none()
        if wait_type == "Lock":
            return True
        if contender_future.done():
            return False
        time.sleep(0.025)
    return False


@pytest.mark.parametrize(
    ("dimension", "first", "second", "expected_error"),
    [
        (
            "daily",
            _identity("first"),
            _identity("second"),
            "SCHEDULED_QUOTA_DAILY_LIMIT",
        ),
        (
            "monthly",
            _identity("first"),
            _identity("second", day=date(2026, 4, 11)),
            "SCHEDULED_QUOTA_MONTHLY_LIMIT",
        ),
        (
            "product",
            _identity("first"),
            _identity("second", day=date(2026, 4, 11), product="product-first"),
            "SCHEDULED_QUOTA_PRODUCT_LIMIT",
        ),
        (
            "vendor",
            _identity("first", vendor="Acme"),
            _identity("second", day=date(2026, 4, 11), vendor=" acme "),
            "SCHEDULED_QUOTA_VENDOR_LIMIT",
        ),
        (
            "board",
            _identity("first", board="board-shared"),
            _identity("second", day=date(2026, 4, 11), board="board-shared"),
            "SCHEDULED_QUOTA_BOARD_LIMIT",
        ),
    ],
)
def test_postgres_control_row_serializes_concurrent_reservations_for_each_dimension(
    isolated_postgres, dimension, first, second, expected_error
):
    engine, schema, stub_tables = isolated_postgres
    _seed_references(engine, stub_tables, first, second)
    limits = _limits_for(dimension)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    first_reserved = threading.Event()
    release_first = threading.Event()
    second_pid_ready = threading.Event()
    second_pid: list[int] = []

    def reserve_and_hold():
        with sessions() as db:
            _reserve(db, first, limits)
            first_reserved.set()
            assert release_first.wait(timeout=10), "timed out waiting to release first reservation"
            db.commit()
            return "reserved"

    def competing_reservation():
        with sessions() as db:
            second_pid.append(int(db.connection().exec_driver_sql("SELECT pg_backend_pid()").scalar_one()))
            second_pid_ready.set()
            try:
                _reserve(db, second, limits)
                db.commit()
                return None
            except ScheduledQuotaError as exc:
                db.rollback()
                return exc.code

    first_result = second_result = None
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(reserve_and_hold)
            assert first_reserved.wait(timeout=10), "first reservation did not reach its held transaction"
            second_future = pool.submit(competing_reservation)
            assert second_pid_ready.wait(timeout=10), "competing transaction did not start"

            # Observe the real backend waiting on PostgreSQL's SELECT FOR UPDATE
            # for the singleton control row before allowing the winner to commit.
            observed_lock_wait = _wait_for_control_row_lock(
                engine, second_pid[0], second_future
            )
            assert observed_lock_wait, "competing reservation never waited on PostgreSQL row lock"

            release_first.set()
            first_result = first_future.result(timeout=10)
            second_result = second_future.result(timeout=10)
    finally:
        release_first.set()

    assert first_result == "reserved"
    assert second_result == expected_error
    with sessions() as db:
        rows = db.scalars(sa.select(RoutineScheduledQuotaReservation)).all()
        assert len(rows) == 1
        assert rows[0].publication_id == first["publication_id"]


@pytest.mark.parametrize("conflicting_identity", [False, True])
def test_postgres_concurrent_duplicate_publication_is_serialized_and_debits_once(
    isolated_postgres, conflicting_identity
):
    engine, _, stub_tables = isolated_postgres
    winner_identity = _identity("racing-publication")
    if conflicting_identity:
        contender_identity = {
            **winner_identity,
            "plan_id": "plan-racing-contender",
            "plan_item_id": "item-racing-contender",
        }
    else:
        contender_identity = dict(winner_identity)
    third_identity = _identity("racing-third")
    _seed_references(
        engine, stub_tables, winner_identity, contender_identity, third_identity
    )
    limits = ScheduledQuotaLimits(
        daily=2, monthly=20, product=20, vendor=20, board=20
    )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    winner_reserved = threading.Event()
    release_winner = threading.Event()
    contender_pid_ready = threading.Event()
    contender_pid: list[int] = []

    def reserve_winner_and_hold():
        with sessions() as db:
            reservation = _reserve(db, winner_identity, limits)
            winner_reserved.set()
            assert release_winner.wait(timeout=10), "timed out waiting to release winning reservation"
            db.commit()
            return reservation.id

    def submit_contender():
        with sessions() as db:
            contender_pid.append(
                int(db.connection().exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            )
            contender_pid_ready.set()
            try:
                reservation = _reserve(db, contender_identity, limits)
                db.commit()
                return ("reserved", reservation.id)
            except ScheduledQuotaError as exc:
                db.rollback()
                return ("error", exc.code)

    winner_result = contender_result = None
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner_future = pool.submit(reserve_winner_and_hold)
            assert winner_reserved.wait(timeout=10), "winning reservation did not reach its held transaction"
            contender_future = pool.submit(submit_contender)
            assert contender_pid_ready.wait(timeout=10), "duplicate contender did not start"

            assert _wait_for_control_row_lock(
                engine, contender_pid[0], contender_future
            ), "duplicate contender never waited on the PostgreSQL control-row lock"
            release_winner.set()
            winner_result = winner_future.result(timeout=10)
            contender_result = contender_future.result(timeout=10)
    finally:
        release_winner.set()

    with sessions() as db:
        rows = db.scalars(sa.select(RoutineScheduledQuotaReservation)).all()
        assert len(rows) == 1
        assert rows[0].publication_id == winner_identity["publication_id"]
        winner_row_id = rows[0].id
    assert winner_result == winner_row_id

    if conflicting_identity:
        assert contender_result == (
            "error",
            "SCHEDULED_QUOTA_PUBLICATION_IDENTITY_CONFLICT",
        )
    else:
        assert contender_result == ("reserved", winner_row_id)

    # With daily capacity for two, this third distinct publication can only
    # succeed if the concurrent same-publication submissions consumed one slot.
    with sessions() as db:
        third_reservation = _reserve(db, third_identity, limits)
        db.commit()
        assert third_reservation.publication_id == third_identity["publication_id"]
        assert db.scalar(
            sa.select(sa.func.count()).select_from(RoutineScheduledQuotaReservation)
        ) == 2


def test_postgres_duplicate_publication_is_idempotent_but_conflicting_identity_fails(
    isolated_postgres,
):
    engine, _, stub_tables = isolated_postgres
    identity = _identity("same-publication")
    _seed_references(engine, stub_tables, identity)
    limits = _limits_for("monthly")
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        original = _reserve(db, identity, limits)
        duplicate = _reserve(db, identity, limits)
        assert duplicate.id == original.id
        db.commit()

    conflicting = {**identity, "scheduled_for": date(2026, 4, 11)}
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        with pytest.raises(ScheduledQuotaError) as error:
            _reserve(db, conflicting, limits)
        assert error.value.code == "SCHEDULED_QUOTA_PUBLICATION_IDENTITY_CONFLICT"
        db.rollback()

    with sessionmaker(bind=engine)() as db:
        rows = db.scalars(sa.select(RoutineScheduledQuotaReservation)).all()
        assert len(rows) == 1
        assert rows[0].publication_id == identity["publication_id"]


@pytest.mark.parametrize("release_mode", ["rollback", "connection_loss"])
def test_postgres_rollback_or_connection_loss_releases_uncommitted_quota(
    isolated_postgres, release_mode
):
    engine, _, stub_tables = isolated_postgres
    abandoned = _identity(f"abandoned-{release_mode}")
    replacement = _identity(f"replacement-{release_mode}")
    _seed_references(engine, stub_tables, abandoned, replacement)
    limits = _limits_for("daily")
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    abandoned_session = sessions()
    reservation = _reserve(abandoned_session, abandoned, limits)
    reservation_id = reservation.id
    if release_mode == "rollback":
        abandoned_session.rollback()
    else:
        # Invalidating the checked-out PostgreSQL connection models process or
        # backend loss: PostgreSQL rolls the uncommitted transaction back.
        abandoned_session.connection().invalidate()
        abandoned_session.close()

    with sessions() as db:
        assert db.get(RoutineScheduledQuotaReservation, reservation_id) is None
        _reserve(db, replacement, limits)
        db.commit()
        assert db.scalar(sa.select(sa.func.count()).select_from(RoutineScheduledQuotaReservation)) == 1
    abandoned_session.close()


def test_postgres_missing_control_row_fails_closed_without_reservation(isolated_postgres):
    engine, _, stub_tables = isolated_postgres
    identity = _identity("missing-control")
    _seed_references(engine, stub_tables, identity)
    with sessionmaker(bind=engine)() as db:
        db.delete(db.get(RoutinePublishingControl, "default"))
        db.commit()

    with sessionmaker(bind=engine)() as db:
        with pytest.raises(ScheduledQuotaError) as error:
            _reserve(db, identity, _limits_for("daily"))
        assert error.value.code == "SCHEDULED_QUOTA_CONTROL_ROW_MISSING"
        db.rollback()
        assert db.scalar(sa.select(sa.func.count()).select_from(RoutineScheduledQuotaReservation)) == 0


@pytest.mark.parametrize("corruption", ["month_bucket", "vendor_key"])
def test_postgres_corrupt_existing_reservation_fails_closed(isolated_postgres, corruption):
    engine, _, stub_tables = isolated_postgres
    stored = _identity(f"corrupt-{corruption}")
    if corruption == "vendor_key":
        stored["vendor_key"] = " Acme "
    candidate = _identity(f"after-corrupt-{corruption}", day=date(2026, 4, 11))
    _seed_references(engine, stub_tables, stored, candidate)

    with sessionmaker(bind=engine)() as db:
        db.add(
            RoutineScheduledQuotaReservation(
                **stored,
                month_start=(
                    date(2026, 5, 1)
                    if corruption == "month_bucket"
                    else date(2026, 4, 1)
                ),
            )
        )
        db.commit()

    with sessionmaker(bind=engine)() as db:
        with pytest.raises(ScheduledQuotaError):
            _reserve(db, candidate, _limits_for("monthly"))
        db.rollback()


@pytest.mark.parametrize("corruption", ["month_start", "vendor_key"])
def test_postgres_assessment_detects_corruption_despite_stale_loaded_orm_row(
    isolated_postgres, corruption
):
    engine, _, stub_tables = isolated_postgres
    identity = _identity(f"stale-{corruption}")
    _seed_references(engine, stub_tables, identity)
    expected_month = date(2026, 4, 1)
    limits = _limits_for("monthly")

    with sessionmaker(bind=engine)() as setup:
        setup.add(
            RoutineScheduledQuotaReservation(
                **identity,
                month_start=expected_month,
            )
        )
        setup.commit()

    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as reader:
        loaded = reader.scalar(
            sa.select(RoutineScheduledQuotaReservation).where(
                RoutineScheduledQuotaReservation.publication_id
                == identity["publication_id"]
            )
        )
        assert loaded is not None
        if corruption == "month_start":
            assert loaded.month_start == expected_month
            changed_values = {"month_start": date(2026, 5, 1)}
        else:
            assert loaded.vendor_key == identity["vendor_key"]
            changed_values = {"vendor_key": f" {identity['vendor_key']} "}

        with sessions() as writer:
            writer.execute(
                sa.update(RoutineScheduledQuotaReservation)
                .where(
                    RoutineScheduledQuotaReservation.publication_id
                    == identity["publication_id"]
                )
                .values(**changed_values)
            )
            writer.commit()

        # The ORM instance is intentionally still stale in reader's identity
        # map; the service must validate the fresh persisted column projection.
        with pytest.raises(ScheduledQuotaError) as error:
            assess_scheduled_quota(reader, **identity, limits=limits)
        assert error.value.code == "SCHEDULED_QUOTA_LEDGER_INCONSISTENT"


def test_postgres_reduced_limit_applies_to_duplicate_of_existing_reservation(
    isolated_postgres,
):
    engine, _, stub_tables = isolated_postgres
    first = _identity("limit-a")
    second = _identity("limit-b")
    _seed_references(engine, stub_tables, first, second)
    generous_limits = ScheduledQuotaLimits(
        daily=10, monthly=10, product=10, vendor=10, board=10
    )
    with sessionmaker(bind=engine)() as db:
        _reserve(db, first, generous_limits)
        db.commit()
    with sessionmaker(bind=engine)() as db:
        _reserve(db, second, generous_limits)
        db.commit()

    reduced_limits = ScheduledQuotaLimits(
        daily=1, monthly=10, product=10, vendor=10, board=10
    )
    with sessionmaker(bind=engine)() as db:
        with pytest.raises(ScheduledQuotaError) as error:
            _reserve(db, first, reduced_limits)
        assert error.value.code == "SCHEDULED_QUOTA_EXISTING_LIMIT_EXCEEDED"
        db.rollback()