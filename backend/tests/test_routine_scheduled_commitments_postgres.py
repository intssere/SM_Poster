"""PostgreSQL integration tests for read-only scheduled commitment assessment."""
from __future__ import annotations

import getpass
import shlex
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.models.domain import (
    Board,
    ContentAngle,
    PinConcept,
    PinDraft,
    PinPublication,
    Product,
    PublicationStatus,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
)
from app.models.routine_publishing import (
    RoutinePublishingControl,
    RoutineScheduledQuotaReservation,
)
from app.services.routine_scheduled_commitments import (
    assess_scheduled_quota_with_commitments,
)
from app.services.routine_scheduled_quotas import (
    ScheduledQuotaError,
    ScheduledQuotaLimits,
    reserve_scheduled_quota,
)


def _schema_name() -> str:
    return f"routine_commitments_{uuid4().hex}"


@pytest.fixture(scope="module")
def local_postgres_url() -> Iterator[sa.engine.URL]:
    """Start a disposable PostgreSQL cluster that accepts Unix sockets only."""
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if not initdb or not pg_ctl:
        pytest.skip("local PostgreSQL initdb and pg_ctl binaries are required")

    with tempfile.TemporaryDirectory(prefix="routine-commitments-postgres-") as temp_dir:
        root = Path(temp_dir)
        data_dir = root / "data"
        socket_dir = root / "socket"
        socket_dir.mkdir(mode=0o700)
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
                        str(root / "postgres.log"),
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
def isolated_postgres(local_postgres_url: sa.engine.URL):
    schema = _schema_name()
    admin = sa.create_engine(local_postgres_url, isolation_level="AUTOCOMMIT")
    schema_created = False
    engine = None
    try:
        with admin.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True

        engine = sa.create_engine(
            local_postgres_url,
            pool_pre_ping=True,
            execution_options={"schema_translate_map": {None: schema}},
        )
        # Minimal FK targets for unneeded publication attributes and store ownership.
        stub_metadata = sa.MetaData(schema=schema)
        stub_tables = {}
        for name in (
            "stores",
            "keyword_clusters",
            "campaigns",
            "pin_creatives",
            "content_revisions",
            "pin_approvals",
            "product_images",
            "creative_templates",
            "integration_accounts",
            "pinterest_connections",
            "pinterest_boards",
        ):
            columns = [sa.Column("id", sa.String(36), primary_key=True)]
            if name == "pin_creatives":
                columns.extend(
                    [
                        sa.Column(
                            "render_status",
                            sa.String(30),
                            nullable=False,
                            server_default="PENDING",
                        ),
                        sa.Column(
                            "render_spec",
                            sa.JSON(),
                            nullable=False,
                            server_default=sa.text("'{}'"),
                        ),
                        sa.Column("sha256", sa.String(64)),
                        sa.Column("source_image_id", sa.String(36)),
                    ]
                )
            stub_tables[name] = sa.Table(name, stub_metadata, *columns)
        stub_metadata.create_all(engine)
        domain_tables = [
            Product.__table__,
            Board.__table__,
            ContentAngle.__table__,
            PinterestPortfolioPlan.__table__,
            PinConcept.__table__,
            PinDraft.__table__,
            PinPublication.__table__,
            PinterestPortfolioPlanItem.__table__,
            RoutinePublishingControl.__table__,
            RoutineScheduledQuotaReservation.__table__,
        ]
        Base.metadata.create_all(engine, tables=domain_tables)
        with engine.begin() as connection:
            connection.execute(
                RoutinePublishingControl.__table__.insert().values(
                    id="default", state="PAUSED"
                )
            )
        yield (
            engine,
            sessionmaker(bind=engine, expire_on_commit=False),
            stub_tables,
        )
    finally:
        if engine is not None:
            engine.dispose()
        if schema_created:
            with admin.connect() as connection:
                connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def _limits() -> ScheduledQuotaLimits:
    return ScheduledQuotaLimits(
        daily=20,
        monthly=20,
        product=20,
        vendor=20,
        board=20,
    )


def _seed_canonical_graph(
    engine: sa.Engine,
    stub_tables: dict[str, sa.Table],
    *,
    publications: list[dict],
) -> dict[str, dict]:
    """Persist publication → draft → concept and plan-item lineage."""
    store_id = "store-commitments"
    angle_id = "angle-commitments"
    plan_id = "plan-commitments"
    first = date(2026, 4, 1)

    with engine.begin() as connection:
        connection.execute(
            stub_tables["stores"].insert().values(id=store_id)
        )
        connection.execute(
            stub_tables["pin_creatives"].insert(),
            [{"id": f"creative-{specification['label']}"} for specification in publications],
        )

    identities = {}
    with Session(engine) as db:
        db.add(
            ContentAngle(
                id=angle_id,
                key="editorial",
                name="Editorial",
                active=True,
            )
        )
        db.add(
            PinterestPortfolioPlan(
                id=plan_id,
                store_id=store_id,
                month_start=first,
                month_end=date(2026, 4, 30),
                target_pins=20,
                existing_commitments=0,
                planned_active_slots=len(publications),
                reserve_slots=0,
                policy_version="test",
                input_fingerprint="a" * 64,
                plan_fingerprint="b" * 64,
                status="ACTIVE",
                metadata_json={},
            )
        )
        db.flush()
        created_products = set()
        created_boards = set()
        for index, specification in enumerate(publications):
            label = specification["label"]
            product_id = specification["product_id"]
            board_id = specification["board_id"]
            concept_id = f"concept-{label}"
            draft_id = f"draft-{label}"
            publication_id = f"publication-{label}"
            item_id = f"item-{label}"
            day = specification["day"]
            identities[label] = {
                "publication_id": publication_id,
                "plan_id": plan_id,
                "plan_item_id": item_id,
                "product_id": product_id,
                "vendor_key": specification["vendor"].strip().casefold(),
                "board_id": board_id,
                "scheduled_for": day,
            }

            if product_id not in created_products:
                db.add(
                    Product(
                        id=product_id,
                        store_id=store_id,
                        shopify_product_id=f"shopify-{product_id}",
                        handle=product_id,
                        title=product_id,
                        vendor=specification["vendor"],
                        product_url=f"https://example.invalid/products/{product_id}",
                    )
                )
                created_products.add(product_id)
            if board_id not in created_boards:
                db.add(
                    Board(
                        id=board_id,
                        store_id=store_id,
                        name=board_id,
                        slug=board_id,
                        active=True,
                        rules={},
                    )
                )
                created_boards.add(board_id)
            db.flush()
            db.add(
                PinConcept(
                    id=concept_id,
                    store_id=store_id,
                    product_id=product_id,
                    content_angle_id=angle_id,
                    board_id=board_id,
                    fingerprint=f"{index + 1:064x}",
                    rationale={},
                )
            )
            db.flush()
            db.add(
                PinDraft(
                    id=draft_id,
                    concept_id=concept_id,
                    version=1,
                    title=f"Title {label}",
                    description=f"Description {label}",
                    alt_text=f"Alt {label}",
                    destination_url="https://example.invalid/",
                    utm_url="https://example.invalid/",
                    text_fingerprint=f"{index + 10:064x}",
                )
            )
            db.flush()
            db.add(
                PinPublication(
                    id=publication_id,
                    draft_id=draft_id,
                    creative_id=f"creative-{label}",
                    board_id=board_id,
                    publication_fingerprint=f"{index + 20:064x}",
                    status=PublicationStatus.SCHEDULED,
                    scheduled_for=datetime.combine(
                        day, datetime.min.time(), tzinfo=timezone.utc
                    ),
                )
            )
            db.flush()
            db.add(
                PinterestPortfolioPlanItem(
                    id=item_id,
                    plan_id=plan_id,
                    slot_index=index,
                    is_reserve=False,
                    planned_date=day,
                    product_id=product_id,
                    local_board_id=board_id,
                    board_key_snapshot=board_id,
                    content_angle_id=angle_id,
                    angle_key_snapshot="editorial",
                    seed_keywords=[],
                    selection_score=1,
                    selection_metadata={},
                    item_fingerprint=f"{index + 30:064x}",
                    status="SCHEDULED",
                    publication_id=publication_id,
                )
            )
        db.commit()
    return identities


def _assess(db: Session, identity: dict, limits: ScheduledQuotaLimits):
    return assess_scheduled_quota_with_commitments(
        db, **identity, limits=limits
    )


def _publication_and_ledger_snapshot(db: Session) -> tuple[list, list]:
    publications = db.execute(
        sa.select(
            PinPublication.id,
            PinPublication.draft_id,
            PinPublication.board_id,
            PinPublication.status,
            PinPublication.scheduled_for,
        ).order_by(PinPublication.id)
    ).all()
    reservations = db.execute(
        sa.select(
            RoutineScheduledQuotaReservation.id,
            RoutineScheduledQuotaReservation.publication_id,
            RoutineScheduledQuotaReservation.plan_id,
            RoutineScheduledQuotaReservation.plan_item_id,
            RoutineScheduledQuotaReservation.product_id,
            RoutineScheduledQuotaReservation.vendor_key,
            RoutineScheduledQuotaReservation.board_id,
            RoutineScheduledQuotaReservation.scheduled_for,
            RoutineScheduledQuotaReservation.month_start,
        ).order_by(RoutineScheduledQuotaReservation.id)
    ).all()
    return publications, reservations


def _assert_fail_closed_without_mutation(
    sessions,
    identity: dict,
    limits: ScheduledQuotaLimits,
    expected_code: str,
) -> None:
    with sessions() as db:
        before = _publication_and_ledger_snapshot(db)
        with pytest.raises(ScheduledQuotaError) as error:
            _assess(db, identity, limits)
        assert error.value.code == expected_code
        assert _publication_and_ledger_snapshot(db) == before


def test_postgres_assessment_counts_canonical_unreserved_and_ledgered_commitments_once(
    isolated_postgres,
):
    engine, sessions, stub_tables = isolated_postgres
    identities = _seed_canonical_graph(
        engine,
        stub_tables,
        publications=[
            {
                "label": "target",
                "day": date(2026, 4, 10),
                "product_id": "product-target",
                "vendor": "Acme",
                "board_id": "board-target",
            },
            {
                "label": "same-day-peer",
                "day": date(2026, 4, 10),
                "product_id": "product-target",
                "vendor": "Acme",
                "board_id": "board-target",
            },
            {
                "label": "other-day-peer",
                "day": date(2026, 4, 11),
                "product_id": "product-other",
                "vendor": "Acme",
                "board_id": "board-other",
            },
            {
                "label": "ledgered-peer",
                "day": date(2026, 4, 12),
                "product_id": "product-target",
                "vendor": "ACME",
                "board_id": "board-target",
            },
        ],
    )
    limits = _limits()
    with sessions() as db:
        reserve_scheduled_quota(db, **identities["ledgered-peer"], limits=limits)
        db.commit()

    with sessions() as db:
        result = _assess(db, identities["target"], limits)

    # The target and two unreserved peers are reconciled from persisted lineage.
    # The fourth publication is represented by its ledger row, not counted again.
    assert result.already_committed is True
    assert {
        dimension: getattr(result, f"{dimension}_used")
        for dimension in ("daily", "monthly", "product", "vendor", "board")
    } == {
        "daily": 2,
        "monthly": 4,
        "product": 3,
        "vendor": 4,
        "board": 3,
    }
    assert {
        dimension: getattr(result, f"{dimension}_remaining")
        for dimension in ("daily", "monthly", "product", "vendor", "board")
    } == {
        "daily": 18,
        "monthly": 16,
        "product": 17,
        "vendor": 16,
        "board": 17,
    }
    assert result.can_reserve is True


def test_postgres_read_only_assessments_complete_during_pending_reservation_without_mutation(
    isolated_postgres,
):
    engine, sessions, stub_tables = isolated_postgres
    identities = _seed_canonical_graph(
        engine,
        stub_tables,
        publications=[
            {
                "label": "pending",
                "day": date(2026, 4, 10),
                "product_id": "product-pending",
                "vendor": "Acme",
                "board_id": "board-pending",
            },
            {
                "label": "unreserved",
                "day": date(2026, 4, 10),
                "product_id": "product-pending",
                "vendor": "Acme",
                "board_id": "board-pending",
            },
        ],
    )
    limits = _limits()
    reservation_pending = threading.Event()
    release_reservation = threading.Event()

    def reserve_and_hold():
        with sessions() as writer:
            reservation = reserve_scheduled_quota(
                writer, **identities["pending"], limits=limits
            )
            reservation_pending.set()
            assert release_reservation.wait(timeout=10), (
                "timed out waiting to release pending reservation"
            )
            writer.commit()
            return reservation.publication_id

    def read_only_assessment():
        with sessions() as reader:
            reader.connection().exec_driver_sql("SET TRANSACTION READ ONLY")
            result = _assess(reader, identities["pending"], limits)
            ledger_count = reader.scalar(
                sa.select(sa.func.count()).select_from(
                    RoutineScheduledQuotaReservation
                )
            )
            return result, ledger_count

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            reservation_future = pool.submit(reserve_and_hold)
            assert reservation_pending.wait(timeout=10), (
                "reservation did not reach its pending transaction"
            )
            readers = [pool.submit(read_only_assessment) for _ in range(2)]
            reader_results = [future.result(timeout=5) for future in readers]

            # The uncommitted ledger row is invisible; the canonical publication
            # still contributes exactly one slot and read-only sessions cannot add rows.
            for result, visible_ledger_rows in reader_results:
                assert visible_ledger_rows == 0
                assert result.already_committed is True
                assert result.daily_used == 2
                assert result.monthly_used == 2
                assert result.product_used == 2
                assert result.vendor_used == 2
                assert result.board_used == 2
            release_reservation.set()
            assert reservation_future.result(timeout=10) == identities["pending"][
                "publication_id"
            ]
    finally:
        release_reservation.set()

    with sessions() as db:
        assert db.scalar(
            sa.select(sa.func.count()).select_from(
                RoutineScheduledQuotaReservation
            )
        ) == 1
        # Once committed, the ledger debit replaces the matching persisted
        # commitment and both independent paths still account for a single slot.
        result = _assess(db, identities["pending"], limits)
        assert result.daily_used == 2
        assert result.monthly_used == 2
        assert result.product_used == 2
        assert result.vendor_used == 2
        assert result.board_used == 2


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ("moved_within_month", "SCHEDULED_QUOTA_COMMITMENT_CONFLICT"),
        ("moved_out_of_month", "SCHEDULED_QUOTA_COMMITMENT_STALE"),
        ("vendor", "SCHEDULED_QUOTA_COMMITMENT_CONFLICT"),
        ("board", "SCHEDULED_QUOTA_COMMITMENT_CONFLICT"),
    ],
)
def test_postgres_stale_current_month_commitment_fails_closed_without_mutation(
    isolated_postgres, change, expected_code
):
    engine, sessions, stub_tables = isolated_postgres
    identities = _seed_canonical_graph(
        engine,
        stub_tables,
        publications=[
            {
                "label": "stale-target",
                "day": date(2026, 4, 10),
                "product_id": "product-stale-target",
                "vendor": "Acme",
                "board_id": "board-stale-target",
            },
            {
                "label": "stale-peer",
                "day": date(2026, 4, 11),
                "product_id": "product-stale-peer",
                "vendor": "Peer Vendor",
                "board_id": "board-stale-peer",
            },
        ],
    )
    identity = identities["stale-target"]

    with engine.begin() as connection:
        if change in ("moved_within_month", "moved_out_of_month"):
            moved_to = (
                date(2026, 4, 12)
                if change == "moved_within_month"
                else date(2026, 5, 10)
            )
            moved_at = datetime.combine(
                moved_to, datetime.min.time(), tzinfo=timezone.utc
            )
            connection.execute(
                sa.update(PinPublication)
                .where(PinPublication.id == identity["publication_id"])
                .values(scheduled_for=moved_at)
            )
            connection.execute(
                sa.update(PinterestPortfolioPlanItem)
                .where(PinterestPortfolioPlanItem.id == identity["plan_item_id"])
                .values(planned_date=moved_to)
            )
        elif change == "vendor":
            connection.execute(
                sa.update(Product)
                .where(Product.id == identity["product_id"])
                .values(vendor="Changed Vendor")
            )
        else:
            changed_board = "board-stale-peer"
            connection.execute(
                sa.update(PinConcept)
                .where(PinConcept.id == "concept-stale-target")
                .values(board_id=changed_board)
            )
            connection.execute(
                sa.update(PinterestPortfolioPlanItem)
                .where(PinterestPortfolioPlanItem.id == identity["plan_item_id"])
                .values(
                    local_board_id=changed_board,
                    board_key_snapshot=changed_board,
                )
            )
            connection.execute(
                sa.update(PinPublication)
                .where(PinPublication.id == identity["publication_id"])
                .values(board_id=changed_board)
            )

    _assert_fail_closed_without_mutation(
        sessions, identity, _limits(), expected_code
    )


@pytest.mark.parametrize("ledger_drift", ["vendor", "out_of_month"])
def test_postgres_ledger_conflicting_with_canonical_commitment_fails_closed(
    isolated_postgres, ledger_drift
):
    engine, sessions, stub_tables = isolated_postgres
    identities = _seed_canonical_graph(
        engine,
        stub_tables,
        publications=[
            {
                "label": "ledger-conflict",
                "day": date(2026, 4, 10),
                "product_id": "product-ledger-conflict",
                "vendor": "Acme",
                "board_id": "board-ledger-conflict",
            }
        ],
    )
    identity = identities["ledger-conflict"]
    with sessions() as db:
        reserve_scheduled_quota(db, **identity, limits=_limits())
        db.commit()

    with engine.begin() as connection:
        values = (
            {"vendor_key": "different-vendor"}
            if ledger_drift == "vendor"
            else {
                "scheduled_for": date(2026, 5, 10),
                "month_start": date(2026, 5, 1),
            }
        )
        connection.execute(
            sa.update(RoutineScheduledQuotaReservation)
            .where(
                RoutineScheduledQuotaReservation.publication_id
                == identity["publication_id"]
            )
            .values(**values)
        )

    _assert_fail_closed_without_mutation(
        sessions,
        identity,
        _limits(),
        "SCHEDULED_QUOTA_PUBLICATION_IDENTITY_CONFLICT",
    )


def test_postgres_duplicate_plan_item_linkage_is_ambiguous_and_read_only(
    isolated_postgres,
):
    engine, sessions, stub_tables = isolated_postgres
    identities = _seed_canonical_graph(
        engine,
        stub_tables,
        publications=[
            {
                "label": "ambiguous",
                "day": date(2026, 4, 10),
                "product_id": "product-ambiguous",
                "vendor": "Acme",
                "board_id": "board-ambiguous",
            }
        ],
    )
    identity = identities["ambiguous"]
    with sessions() as db:
        original = db.get(PinterestPortfolioPlanItem, identity["plan_item_id"])
        assert original is not None
        db.add(
            PinterestPortfolioPlanItem(
                id="zz-duplicate-plan-item-link",
                plan_id=original.plan_id,
                slot_index=99,
                is_reserve=False,
                planned_date=original.planned_date,
                product_id=original.product_id,
                local_board_id=original.local_board_id,
                board_key_snapshot=original.board_key_snapshot,
                content_angle_id=original.content_angle_id,
                angle_key_snapshot=original.angle_key_snapshot,
                seed_keywords=[],
                selection_score=1,
                selection_metadata={},
                item_fingerprint="d" * 64,
                status="SCHEDULED",
                publication_id=identity["publication_id"],
            )
        )
        db.commit()

    _assert_fail_closed_without_mutation(
        sessions,
        identity,
        _limits(),
        "SCHEDULED_QUOTA_COMMITMENT_AMBIGUOUS",
    )


@pytest.mark.parametrize("dimension", ["daily", "monthly", "product", "vendor", "board"])
def test_postgres_reduced_limits_below_reconciled_usage_fail_without_mutation(
    isolated_postgres, dimension
):
    engine, sessions, stub_tables = isolated_postgres
    target = {
        "label": f"limit-target-{dimension}",
        "day": date(2026, 4, 10),
        "product_id": f"product-limit-target-{dimension}",
        "vendor": "Target Vendor",
        "board_id": f"board-limit-target-{dimension}",
    }
    peer = {
        "label": f"limit-peer-{dimension}",
        "day": date(2026, 4, 11),
        "product_id": f"product-limit-peer-{dimension}",
        "vendor": "Peer Vendor",
        "board_id": f"board-limit-peer-{dimension}",
    }
    if dimension == "daily":
        peer["day"] = target["day"]
    elif dimension == "product":
        peer["product_id"] = target["product_id"]
    elif dimension == "vendor":
        peer["vendor"] = target["vendor"]
    elif dimension == "board":
        peer["board_id"] = target["board_id"]

    identities = _seed_canonical_graph(
        engine, stub_tables, publications=[target, peer]
    )
    limits = ScheduledQuotaLimits(
        daily=1 if dimension == "daily" else 20,
        monthly=1 if dimension == "monthly" else 20,
        product=1 if dimension == "product" else 20,
        vendor=1 if dimension == "vendor" else 20,
        board=1 if dimension == "board" else 20,
    )
    _assert_fail_closed_without_mutation(
        sessions,
        identities[target["label"]],
        limits,
        "SCHEDULED_QUOTA_EXISTING_LIMIT_EXCEEDED",
    )