from __future__ import annotations

import getpass
import hashlib
import io
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
from PIL import Image, ImageDraw
from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes import proposals as proposal_routes
from app.core.config import Settings
from app.db.base import Base
from app.models.domain import (
    AuditLog,
    Board,
    ContentAngle,
    PinApproval,
    PinConcept,
    PinCreative,
    PinDraft,
    PinPublication,
    PublicationAttempt,
    PublicationStatus,
    PinterestAutonomousExecutionRun,
    PinterestAutonomousGenerationRun,
    PinterestBoard,
    PinterestConnection,
    PinterestOptimizerApplication,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
    Product,
    ProductImage,
    ProductIntelligence,
    Store,
)
from app.models.routine_publishing import (
    RoutineDispatchPermit,
    RoutinePublishingControl,
    RoutinePublishingRun,
)
from app.services import pinterest_local_canary_fixture as fixture
from app.services import public_creative_media, publication_identity


NOW = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)
ITEM_ID = "local-canary-item"
SOURCE_ID = "local-canary-source"
PLAN_ID = "local-canary-plan"
MEDIA_MARKER = "TASK61_6A_LOCAL_MEDIA_STAGED_V1"


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[sa.engine.URL]:
    """Provide a disposable PostgreSQL instance bound only to a Unix socket."""
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if not initdb or not pg_ctl:
        pytest.skip("local PostgreSQL initdb and pg_ctl binaries are required")

    with tempfile.TemporaryDirectory(prefix="task61-6a-postgres-") as directory:
        root = Path(directory)
        data_dir = root / "data"
        socket_dir = root / "socket"
        socket_dir.mkdir(mode=0o700)
        username = getpass.getuser()
        try:
            subprocess.run(
                [
                    initdb,
                    "-D",
                    str(data_dir),
                    "-U",
                    username,
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
            "-c port=55497 -c unix_socket_permissions=0700"
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
                pytest.skip("could not start the disposable local PostgreSQL cluster")

            yield sa.engine.URL.create(
                "postgresql+psycopg",
                username=username,
                database="postgres",
                query={"host": str(socket_dir), "port": "55497"},
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
def db_session(postgres_url: sa.engine.URL) -> Iterator[Session]:
    schema = f"task616a_{uuid4().hex}"
    admin = sa.create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    engine = None
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        engine = sa.create_engine(
            postgres_url,
            execution_options={"schema_translate_map": {None: schema}},
        )
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as db:
            _seed(db)
            yield db
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def configured(db_session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    settings = Settings(
        database_url="postgresql://local-only-test",
        public_media_base_url="https://media.fixture-cdn.com",
        publishing_enabled=False,
        buffer_publishing_enabled=False,
        buffer_single_pin_pilot_enabled=False,
        pinterest_single_pin_pilot_enabled=False,
        routine_pinterest_worker_enabled=False,
        routine_buffer_dispatch_enabled=False,
        routine_scheduled_live_admission_enabled=False,
        routine_pinterest_scheduler_enabled=False,
        routine_scheduler_canary_enabled=False,
        routine_scheduled_autonomy_enabled=False,
        routine_autonomous_authorization_enabled=False,
        routine_pinterest_dry_run=True,
        routine_pinterest_batch_size=1,
        routine_pinterest_daily_write_limit=1,
        pinterest_portfolio_activation_enabled=False,
        pinterest_optimizer_apply_enabled=False,
        pinterest_autonomous_generation_enabled=False,
        pinterest_autonomous_execution_enabled=False,
        pinterest_autonomous_board_ensure_enabled=False,
        pinterest_write_scope_enabled=False,
        pinterest_board_write_scope_enabled=False,
        pinterest_board_provisioning_enabled=False,
        pinterest_autonomous_schedule_start_minute_utc=600,
        pinterest_autonomous_schedule_end_minute_utc=720,
    )
    monkeypatch.setattr(fixture, "get_settings", lambda: settings)
    monkeypatch.setattr(public_creative_media, "get_settings", lambda: settings)
    monkeypatch.setattr(publication_identity, "get_settings", lambda: settings)
    monkeypatch.setattr(
        fixture,
        "scheduler_status",
        lambda *_: {
            "enabled": False,
            "started": False,
            "task_running": False,
            "tick_running": False,
            "lease_supported": True,
            "lease_held": False,
        },
    )
    return db_session, settings, tmp_path / "staged-media"


def _source_bytes() -> bytes:
    image = Image.new("RGB", (720, 960), "white")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((230, 275, 490, 820), radius=45, fill=(133, 75, 34))
    draw.rectangle((290, 205, 430, 280), fill=(133, 75, 34))
    draw.rectangle((305, 150, 415, 210), fill=(176, 150, 98))
    draw.rounded_rectangle((280, 440, 440, 620), radius=10, fill=(239, 220, 176))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _seed(db: Session) -> bytes:
    source = _source_bytes()
    source_sha = hashlib.sha256(source).hexdigest()
    sync_time = datetime(2026, 9, 30, 10, tzinfo=timezone.utc)
    store = Store(id="local-canary-store", name="Local Test", shop_domain="shop.fixture.invalid")
    product = Product(
        id="local-canary-product",
        store_id=store.id,
        shopify_product_id="offline-product",
        handle="amber-product",
        title="Amber Perfume",
        vendor="Fixture House",
        status="ACTIVE",
        product_url="https://diamondshelf.us/products/amber-product",
        inventory_total=1,
        excluded_from_editorial=False,
    )
    local_board = Board(
        id="local-canary-local-board",
        store_id=store.id,
        name="Amber Fragrance",
        slug="amber-fragrance",
        active=True,
        rules={},
    )
    angle = ContentAngle(
        id="local-canary-angle",
        key="daily-luxury",
        name="Daily luxury",
        active=True,
    )
    image = ProductImage(
        id=SOURCE_ID,
        product_id=product.id,
        shopify_media_id="offline-source-media",
        source_url="https://cdn.shopify.com/s/files/1/0000/amber.png",
        alt_text="Amber perfume bottle",
        width=720,
        height=960,
        source_sha256=source_sha,
        is_primary=True,
        editorial_eligible=True,
    )
    intelligence = ProductIntelligence(
        product_id=product.id,
        brand="Fixture House",
        audience="fragrance enthusiasts",
        fragrance_family="amber",
        inventory_eligible=True,
        image_available=True,
        eligibility_status="ELIGIBLE",
        normalization_status="NORMALIZED",
        normalized_data={"brand": "Fixture House", "family": "amber"},
    )
    plan = PinterestPortfolioPlan(
        id=PLAN_ID,
        store_id=store.id,
        month_start=date(2026, 9, 1),
        month_end=date(2026, 9, 30),
        target_pins=1,
        existing_commitments=0,
        planned_active_slots=1,
        reserve_slots=0,
        policy_version="fixture-plan-v1",
        input_fingerprint="1" * 64,
        plan_fingerprint="2" * 64,
        status="ACTIVE",
        metadata_json={},
    )
    optimizer = PinterestOptimizerApplication(
        id="local-canary-optimizer",
        plan_id=plan.id,
        plan_fingerprint_snapshot=plan.plan_fingerprint,
        optimizer_policy_version="optimizer-test-v1",
        optimizer_fingerprint="3" * 64,
        input_state_fingerprint="4" * 64,
        frozen_item_count=0,
        optimizable_item_count=1,
        exploit_count=0,
        explore_count=1,
        recommendation_snapshot={},
        status="APPLIED",
        applied_by="local-test",
        applied_at=sync_time,
    )
    planned_date = date(2026, 9, 30)
    item = PinterestPortfolioPlanItem(
        id=ITEM_ID,
        plan_id=plan.id,
        slot_index=1,
        is_reserve=False,
        planned_date=planned_date,
        product_id=product.id,
        local_board_id=local_board.id,
        board_key_snapshot=local_board.slug,
        content_angle_id=angle.id,
        angle_key_snapshot=angle.key,
        seed_keywords=["amber perfume", "amber fragrance"],
        selection_score=1,
        selection_metadata={
            "candidate_fingerprint": "5" * 64,
            "adaptive_optimizer_v1": {
                "optimizer_policy_version": optimizer.optimizer_policy_version,
                "optimizer_fingerprint": optimizer.optimizer_fingerprint,
                "input_state_fingerprint": optimizer.input_state_fingerprint,
                "recommended_position": 0,
                "target_slot_index": 1,
                "target_planned_date": planned_date.isoformat(),
                "selection_reason": "offline-fixture-test",
            },
        },
        item_fingerprint="6" * 64,
        status="PLANNED",
    )
    connection = PinterestConnection(
        id="local-canary-connection",
        external_user_id="offline-user",
        username="offline-user",
        granted_scopes=["boards:read"],
        access_token_ciphertext="test-not-a-secret",
        refresh_token_ciphertext="test-not-a-secret",
        status="CONNECTED",
        boards_last_synced_at=sync_time,
    )
    provider_board = PinterestBoard(
        id="local-canary-provider-board",
        connection_id=connection.id,
        external_board_id="offline-board",
        name=local_board.name,
        routing_label=local_board.slug,
        is_active=True,
        is_eligible=True,
        last_synced_at=sync_time,
        last_seen_at=sync_time,
    )
    db.add(store)
    db.flush()
    db.add_all([product, local_board, angle, plan, connection])
    db.flush()
    db.add_all([image, intelligence, optimizer, item, provider_board])
    db.add(
        RoutinePublishingControl(
            id="default",
            state="PAUSED",
            paused_at=sync_time,
            paused_by="local-test",
        )
    )
    db.commit()
    return source


def _prepare(configured, *, item_id: str = ITEM_ID, now: datetime = NOW):
    db, settings, root = configured
    source = _source_bytes()
    return fixture.prepare_local_canary_fixture(
        db,
        item_id,
        source_image_id=SOURCE_ID,
        source_bytes=source,
        local_media_root=root,
        actor="local-test",
        settings=settings,
        now=now,
    )


def _rows(db: Session):
    item = db.get(PinterestPortfolioPlanItem, ITEM_ID)
    execution = db.scalar(
        sa.select(PinterestAutonomousExecutionRun)
        .where(PinterestAutonomousExecutionRun.portfolio_item_id == ITEM_ID)
    )
    generation = db.scalar(
        sa.select(PinterestAutonomousGenerationRun)
        .where(PinterestAutonomousGenerationRun.portfolio_item_id == ITEM_ID)
    )
    publication = db.get(PinPublication, execution.publication_id) if execution else None
    creative = db.get(PinCreative, generation.creative_id) if generation else None
    approval = db.get(PinApproval, execution.approval_id) if execution else None
    return item, execution, generation, publication, creative, approval


def _crash_after_pending_commit(monkeypatch: pytest.MonkeyPatch):
    original = fixture.promote_staged_creative
    monkeypatch.setattr(
        fixture,
        "promote_staged_creative",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            fixture.LocalCanaryMediaError("SIMULATED_CRASH_AFTER_DB_COMMIT")
        ),
    )
    return original


def test_postgres_happy_path_pending_commit_promotion_and_reconcile(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, settings, root = configured
    original_promote = _crash_after_pending_commit(monkeypatch)
    with pytest.raises(fixture.LocalCanaryFixtureError, match="PENDING_RECONCILIATION"):
        _prepare(configured)

    item, execution, generation, publication, creative, approval = _rows(db)
    assert item.status == "PLANNED"
    assert publication.status == PublicationStatus.APPROVED
    assert publication.scheduled_for is None
    assert creative.render_status == "STAGED"
    assert execution.status == "STARTED"
    assert generation.status == "STARTED"
    assert approval.decision == "APPROVED"
    assert db.query(RoutineDispatchPermit).count() == 0
    assert db.query(PublicationAttempt).count() == 0

    monkeypatch.setattr(fixture, "promote_staged_creative", original_promote)
    first = fixture.reconcile_local_canary_fixture(
        db, ITEM_ID, local_media_root=root, settings=settings, now=NOW
    )
    second = fixture.reconcile_local_canary_fixture(
        db, ITEM_ID, local_media_root=root, settings=settings, now=NOW
    )
    item, execution, generation, publication, creative, approval = _rows(db)
    assert first["status"] == second["status"] == "SUCCEEDED"
    assert first["provider_called"] is False
    assert first["ai_called"] is False
    assert first["external_requests"] == 0
    assert execution.status == "SUCCEEDED"
    assert generation.status == "SUCCEEDED"
    assert item.status == "SCHEDULED"
    assert publication.status == PublicationStatus.SCHEDULED
    assert publication.scheduled_for == execution.scheduled_for
    assert creative.render_status == "RENDERED"
    assert creative.sha256 == creative.render_spec["local_canary_stage"]["artifact_sha256"]
    assert execution.safe_metadata["provider_called"] is False
    assert execution.safe_metadata["media_state"] == "PROMOTED"
    assert approval.decision == "APPROVED"
    assert db.query(RoutineDispatchPermit).count() == 1
    assert db.query(PublicationAttempt).count() == 0
    assert db.query(RoutinePublishingRun).count() == 0
    assert db.query(PinPublication).filter(
        PinPublication.status.in_(
            [PublicationStatus.PUBLISHING, PublicationStatus.PUBLISH_UNKNOWN]
        )
    ).count() == 0


def test_postgres_rollback_after_render_leaves_no_fixture_or_usable_media(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, _, root = configured
    original = fixture.autonomous_content_policy

    def fail_after_render(*args, **kwargs):
        raise fixture.LocalCanaryFixtureError("INJECTED_ROLLBACK_AFTER_RENDER")

    monkeypatch.setattr(fixture, "autonomous_content_policy", fail_after_render)
    with pytest.raises(fixture.LocalCanaryFixtureError, match="INJECTED_ROLLBACK"):
        _prepare(configured)
    db.expire_all()
    assert db.query(PinConcept).count() == 0
    assert db.query(PinDraft).count() == 0
    assert db.query(PinCreative).count() == 0
    assert db.query(PinPublication).count() == 0
    assert db.query(PinterestAutonomousExecutionRun).count() == 0
    assert fixture.cleanup_local_canary_orphans(db, root) == []
    assert original is not None


def test_postgres_precommit_crash_rolls_back_and_orphan_stage_is_removed(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, _, root = configured

    class SimulatedProcessCrash(BaseException):
        pass

    real_commit = db.commit
    monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(SimulatedProcessCrash()))
    with pytest.raises(SimulatedProcessCrash):
        _prepare(configured)
    monkeypatch.setattr(db, "commit", real_commit)
    db.close()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    with factory() as fresh:
        assert fresh.query(PinCreative).count() == 0
        files = list((root / ".task61-6-staging").glob("*.stage"))
        assert len(files) == 1
        removed = fixture.cleanup_local_canary_orphans(fresh, root)
        assert len(removed) == 1
        assert not files[0].exists()


def test_postgres_duplicate_prepare_is_idempotent_and_preserves_one_fixture(configured):
    first = _prepare(configured)
    second = _prepare(configured)
    db, _, _ = configured
    assert first["status"] == second["status"] == "SUCCEEDED"
    assert db.query(PinterestAutonomousExecutionRun).count() == 1
    assert db.query(PinterestAutonomousGenerationRun).count() == 1
    assert db.query(PinPublication).count() == 1
    assert db.query(RoutineDispatchPermit).count() == 1


def test_postgres_stale_fingerprint_blocks_reconciliation(configured, monkeypatch):
    db, settings, root = configured
    original_promote = _crash_after_pending_commit(monkeypatch)
    with pytest.raises(fixture.LocalCanaryFixtureError):
        _prepare(configured)
    monkeypatch.setattr(fixture, "promote_staged_creative", original_promote)
    item, _, _, _, _, _ = _rows(db)
    item.item_fingerprint = "f" * 64
    db.commit()
    with pytest.raises(fixture.LocalCanaryFixtureError):
        fixture.reconcile_local_canary_fixture(
            db, ITEM_ID, local_media_root=root, settings=settings, now=NOW
        )
    db.expire_all()
    item, execution, _, publication, creative, _ = _rows(db)
    assert item.status == "PLANNED"
    assert execution.status == "STARTED"
    assert publication.status == PublicationStatus.APPROVED
    assert creative.render_status == "STAGED"
    assert db.query(RoutineDispatchPermit).count() == 0


def test_postgres_route_mismatch_blocks_reconciliation(configured, monkeypatch):
    db, settings, root = configured
    original_promote = _crash_after_pending_commit(monkeypatch)
    with pytest.raises(fixture.LocalCanaryFixtureError):
        _prepare(configured)
    monkeypatch.setattr(fixture, "promote_staged_creative", original_promote)
    board = db.get(PinterestBoard, "local-canary-provider-board")
    board.external_board_id = "changed-board"
    db.commit()
    with pytest.raises(fixture.LocalCanaryFixtureError):
        fixture.reconcile_local_canary_fixture(
            db, ITEM_ID, local_media_root=root, settings=settings, now=NOW
        )
    assert db.query(RoutineDispatchPermit).count() == 0


def test_postgres_missing_provenance_fails_closed(configured, monkeypatch):
    db, settings, root = configured
    original_promote = _crash_after_pending_commit(monkeypatch)
    with pytest.raises(fixture.LocalCanaryFixtureError):
        _prepare(configured)
    monkeypatch.setattr(fixture, "promote_staged_creative", original_promote)
    _, _, _, _, creative, _ = _rows(db)
    creative.render_spec = {"local_canary_protocol": MEDIA_MARKER}
    db.commit()
    with pytest.raises(
        fixture.LocalCanaryFixtureError, match="PENDING_RECONCILIATION"
    ):
        fixture.reconcile_local_canary_fixture(
            db, ITEM_ID, local_media_root=root, settings=settings, now=NOW
        )
    assert db.query(RoutineDispatchPermit).count() == 0
    assert db.get(PinPublication, _rows(db)[3].id).status == PublicationStatus.APPROVED


def test_postgres_admission_blocked_while_media_is_pending(configured, monkeypatch):
    db, _, _ = configured
    _crash_after_pending_commit(monkeypatch)
    with pytest.raises(fixture.LocalCanaryFixtureError):
        _prepare(configured)
    _, _, _, publication, _, _ = _rows(db)
    from app.services.publication_scheduler import schedule

    with pytest.raises(ValueError, match="LOCAL_CANARY_MEDIA_PENDING"):
        schedule(db, publication, NOW, commit=False)
    assert db.query(RoutineDispatchPermit).count() == 0


def test_postgres_historical_publication_attempts_are_unchanged(configured):
    db, _, _ = configured
    item = db.get(PinterestPortfolioPlanItem, ITEM_ID)
    product = db.get(Product, item.product_id)
    local_board = db.get(Board, item.local_board_id)
    angle = db.get(ContentAngle, item.content_angle_id)
    template = fixture.CreativeTemplate(
        id="historical-template",
        key="historical-template",
        version=1,
        name="Historical",
        active=True,
    )
    db.add(template)
    concept = PinConcept(
        id="historical-concept",
        store_id=product.store_id,
        product_id=product.id,
        content_angle_id=angle.id,
        board_id=local_board.id,
        fingerprint="7" * 64,
        rationale={},
    )
    db.add(concept)
    db.flush()
    draft = PinDraft(
        id="historical-draft",
        concept_id=concept.id,
        version=1,
        title="Historical title",
        description="Historical description",
        alt_text="Historical alt",
        destination_url="https://diamondshelf.us/old",
        utm_url="https://diamondshelf.us/old?utm_source=pinterest",
        text_fingerprint="8" * 64,
        status="APPROVED",
    )
    db.add(draft)
    db.flush()
    creative = PinCreative(
        id="historical-creative",
        draft_id=draft.id,
        template_id=template.id,
        source_image_id=SOURCE_ID,
        rendered_url="https://media.fixture-cdn.com/historical.png",
        sha256="9" * 64,
        creative_fingerprint="c" * 64,
        width=1000,
        height=1500,
        render_status="RENDERED",
    )
    db.add(creative)
    db.flush()
    approval = PinApproval(
        id="historical-approval",
        draft_id=draft.id,
        creative_id=creative.id,
        approved_version_id="original",
        decision="APPROVED",
        decided_by="historical-reviewer",
    )
    db.add(approval)
    db.flush()
    historical = PinPublication(
        id="historical-published",
        draft_id=draft.id,
        creative_id=creative.id,
        approval_id=approval.id,
        pinterest_connection_id="local-canary-connection",
        pinterest_board_record_id="local-canary-provider-board",
        pinterest_board_id_snapshot="offline-board",
        publication_fingerprint="a" * 64,
        text_fingerprint="b" * 64,
        creative_fingerprint="c" * 64,
        status=PublicationStatus.PUBLISHED,
        scheduled_for=datetime(2025, 1, 1, tzinfo=timezone.utc),
        title_snapshot="historical title",
        description_snapshot="historical description",
        alt_text_snapshot="historical alt",
        destination_url="https://diamondshelf.us/old",
        utm_url="https://diamondshelf.us/old?utm_source=pinterest",
        media_url_snapshot="https://media.fixture-cdn.com/old.png",
        source_image_id=SOURCE_ID,
        template_id=template.id,
        template_key=template.key,
        template_version=1,
    )
    db.add(historical)
    db.commit()
    before = (
        historical.status,
        historical.scheduled_for,
        historical.publication_fingerprint,
        historical.media_url_snapshot,
    )
    _prepare(configured)
    db.expire_all()
    historical = db.get(PinPublication, "historical-published")
    assert (
        historical.status,
        historical.scheduled_for,
        historical.publication_fingerprint,
        historical.media_url_snapshot,
    ) == before
    assert db.query(PublicationAttempt).count() == 0


def test_postgres_cleanup_preserves_committed_pending_artifact(configured, monkeypatch):
    db, _, root = configured
    original_promote = _crash_after_pending_commit(monkeypatch)
    with pytest.raises(fixture.LocalCanaryFixtureError):
        _prepare(configured)
    monkeypatch.setattr(fixture, "promote_staged_creative", original_promote)
    staged = list((root / ".task61-6-staging").glob("*.stage"))
    assert len(staged) == 1
    assert fixture.cleanup_local_canary_orphans(db, root) == []
    assert staged[0].exists()


def test_postgres_cleanup_waits_for_precommit_stage_and_reconcile_succeeds(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, settings, root = configured
    sessions = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    staged_event = threading.Event()
    allow_commit = threading.Event()
    cleanup_started = threading.Event()
    cleanup_finished = threading.Event()
    real_stage = fixture.stage_creative_png

    def pause_after_stage(*args, **kwargs):
        receipt = real_stage(*args, **kwargs)
        staged_event.set()
        if not allow_commit.wait(timeout=15):
            raise AssertionError("test did not release staged preparation")
        return receipt

    monkeypatch.setattr(fixture, "stage_creative_png", pause_after_stage)

    def prepare_in_session():
        with sessions() as worker_db:
            return fixture.prepare_local_canary_fixture(
                worker_db,
                ITEM_ID,
                source_image_id=SOURCE_ID,
                source_bytes=_source_bytes(),
                local_media_root=root,
                actor="precommit-race-test",
                settings=settings,
                now=NOW,
            )

    def cleanup_in_session():
        with sessions() as cleanup_db:
            cleanup_started.set()
            result = fixture.cleanup_local_canary_orphans(cleanup_db, root)
            cleanup_finished.set()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        preparation = pool.submit(prepare_in_session)
        try:
            assert staged_event.wait(timeout=10)
            staged_files = list((root / ".task61-6-staging").glob("*.stage"))
            assert len(staged_files) == 1
            cleanup = pool.submit(cleanup_in_session)
            assert cleanup_started.wait(timeout=5)
            # The cleanup uses the same singleton lock as preparation. It must
            # remain blocked while the stage exists only inside an uncommitted
            # transaction and must not delete that live stage.
            assert not cleanup_finished.wait(timeout=0.25)
            assert staged_files[0].exists()
        finally:
            allow_commit.set()

        result = preparation.result(timeout=20)
        removed = cleanup.result(timeout=20)
    assert result["status"] == "SUCCEEDED"
    assert removed == []
    assert not staged_files[0].exists()
    db.expire_all()
    _, execution, generation, publication, creative, _ = _rows(db)
    assert execution.status == generation.status == "SUCCEEDED"
    assert publication.status == PublicationStatus.SCHEDULED
    assert creative.render_status == "RENDERED"


def test_publication_media_route_hides_stage_then_serves_verified_promoted_png(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, settings, root = configured
    monkeypatch.setattr(proposal_routes, "get_settings", lambda: settings)
    original_promote = _crash_after_pending_commit(monkeypatch)
    with pytest.raises(fixture.LocalCanaryFixtureError):
        _prepare(configured)
    monkeypatch.setattr(fixture, "promote_staged_creative", original_promote)
    _, _, _, publication, creative, _ = _rows(db)
    receipt = creative.render_spec["local_canary_stage"]
    digest = receipt["artifact_sha256"]
    expected_path = (
        f"https://media.fixture-cdn.com/api/pins/public-creatives/"
        f"{creative.id}/{digest}.png"
    )
    assert publication.media_url_snapshot == expected_path

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/api/pins/public-creatives/{creative.id}/{digest}.png",
        "raw_path": f"/api/pins/public-creatives/{creative.id}/{digest}.png".encode(),
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }
    request = Request(scope)
    with pytest.raises(HTTPException) as response:
        proposal_routes.public_creative_image(
            creative.id, digest, request, db=db
        )
    assert response.value.status_code == 404

    fixture.reconcile_local_canary_fixture(
        db, ITEM_ID, local_media_root=root, settings=settings, now=NOW
    )
    db.expire_all()
    _, _, _, publication, creative, _ = _rows(db)
    assert publication.media_url_snapshot == expected_path
    served = proposal_routes.public_creative_image(
        creative.id, digest, request, db=db
    )
    assert served.status_code == 200
    assert served.media_type == "image/png"
    assert hashlib.sha256(served.body).hexdigest() == digest
    assert served.body == (root / receipt["final_path"]).read_bytes()


def test_postgres_duplicate_concurrent_calls_create_one_execution(configured):
    db, settings, root = configured
    barrier = threading.Barrier(2)
    sessions = sessionmaker(bind=db.get_bind(), expire_on_commit=False)

    def invoke():
        with sessions() as concurrent_db:
            barrier.wait(timeout=10)
            try:
                return fixture.prepare_local_canary_fixture(
                    concurrent_db,
                    ITEM_ID,
                    source_image_id=SOURCE_ID,
                    source_bytes=_source_bytes(),
                    local_media_root=root,
                    actor="concurrent-local-test",
                    settings=settings,
                    now=NOW,
                )["status"]
            except fixture.LocalCanaryFixtureError as exc:
                return f"{exc}::{type(exc.__cause__).__name__}:{exc.__cause__}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: invoke(), range(2)))
    assert all(
        outcome == "SUCCEEDED"
        or any(
            marker in outcome
            for marker in (
                "HISTORY_ALREADY_PRESENT",
                "PORTFOLIO_ITEM_NOT_PLANNED",
                "PENDING_RECONCILIATION",
            )
        )
        for outcome in outcomes
    )
    db.expire_all()
    assert db.query(PinterestAutonomousExecutionRun).count() <= 1
    assert db.query(PinterestAutonomousGenerationRun).count() <= 1
    assert db.query(PinPublication).filter(
        PinPublication.status.in_(
            [PublicationStatus.PUBLISHING, PublicationStatus.PUBLISH_UNKNOWN]
        )
    ).count() == 0
    assert db.query(PublicationAttempt).count() == 0
    if db.query(PinterestAutonomousExecutionRun).count() == 0:
        result = _prepare(configured)
    else:
        result = fixture.reconcile_local_canary_fixture(
            db, ITEM_ID, local_media_root=root, settings=settings, now=NOW
        )
    assert result["status"] == "SUCCEEDED"
    db.expire_all()
    assert db.query(PinterestAutonomousExecutionRun).count() == 1
    assert db.query(PinterestAutonomousGenerationRun).count() == 1
    assert db.query(PinPublication).count() == 1
    assert db.query(RoutineDispatchPermit).count() == 1