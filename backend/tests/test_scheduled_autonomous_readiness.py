from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import (
    Board,
    ContentAngle,
    CreativeTemplate,
    DraftStatus,
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
    PinterestSeoBrief,
    Product,
    ProductImage,
    Store,
)
from app.models.routine_publishing import (
    RoutineAttemptBoundary,
    RoutineDispatchPermit,
    RoutinePublishingControl,
    RoutinePublishingRun,
)
from app.services import routine_pinterest_scheduler as scheduler
from app.services import routine_pinterest_worker as worker
from app.services import routine_offline_preflight
from app.services.fingerprints import text_fingerprint
from app.services.publication_scheduler import request_fingerprint_for
from app.services.routine_autonomous_authorization import AUTONOMOUS_ACTOR
from app.services.routine_dispatch_authorization import _snapshots
from app.services.scheduled_autonomous_readiness import (
    OPTIMIZER_METADATA_KEY,
    _current_persisted_route,
    _execution_fingerprint,
    _hash,
    scheduled_autonomous_readiness,
)


NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _settings(**overrides):
    values = dict(
        database_url="sqlite+pysqlite:///:memory:",
        publishing_enabled=False,
        buffer_publishing_enabled=False,
        routine_pinterest_scheduler_enabled=True,
        routine_pinterest_worker_enabled=True,
        routine_buffer_dispatch_enabled=False,
        routine_pinterest_dry_run=True,
        routine_pinterest_batch_size=1,
        routine_pinterest_daily_write_limit=1,
        routine_scheduled_autonomy_enabled=False,
    )
    values.update(overrides)
    settings = Settings(**values)
    object.__setattr__(
        settings,
        "routine_scheduled_autonomy_enabled",
        values["routine_scheduled_autonomy_enabled"],
    )
    return settings


def _database():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _due_publication():
    return PinPublication(
        id="scheduled-autonomy-publication",
        draft_id="draft-existing",
        creative_id="creative-existing",
        approval_id="approval-existing",
        pinterest_board_record_id="board-existing",
        pinterest_connection_id="connection-existing",
        pinterest_board_id_snapshot="external-board-existing",
        publication_fingerprint="a" * 64,
        text_fingerprint="b" * 64,
        creative_fingerprint="c" * 64,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=NOW - timedelta(minutes=1),
        title_snapshot="Already approved title",
        description_snapshot="Already approved description",
        alt_text_snapshot="Already approved alt text",
        destination_url="https://diamondshelf.us/products/already-approved",
        utm_url="https://diamondshelf.us/products/already-approved?utm_source=pinterest",
        media_url_snapshot="https://cdn.example.test/already-approved.jpg",
        source_image_id="source-existing",
        template_id="template-existing",
        template_key="template",
        template_version=1,
    )


def _existing_permit(publication):
    return RoutineDispatchPermit(
        id="permit-created-before-certification",
        publication_id=publication.id,
        dispatch_provider="buffer",
        approval_id=publication.approval_id,
        pinterest_board_record_id=publication.pinterest_board_record_id,
        publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint=request_fingerprint_for(publication),
        scheduled_for_snapshot=publication.scheduled_for,
        quality_policy_version="PINTEREST_QUALITY_V1",
        quality_snapshot={},
        duplicate_snapshot={},
        readiness_snapshot={"dispatch_provider": "buffer"},
        authorized_by="preexisting-operator-authorization",
        authorized_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
        status="ACTIVE",
    )


def _seed_positive_ready_autonomous_chain(db):
    """Seed a real plan-to-permit lineage before the certification run."""
    planned_date = date(2026, 10, 1)
    scheduled_for = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
    synced_at = NOW - timedelta(hours=1)
    product_url = "https://diamondshelf.us/products/amber-perfume"
    utm_url = (
        f"{product_url}?utm_source=pinterest&utm_medium=social"
        "&utm_campaign=fall-amber"
    )
    media_url = (
        "https://cdn.shopify.com/s/files/1/0000/0001/products/"
        "amber-perfume.jpg"
    )
    title = "Amber Fragrance for Everyday Elegance"
    description = (
        "Discover an elegant amber fragrance for daily rituals. "
        "A warm scent profile pairs beautifully with a polished presentation "
        "for gifting or personal wear."
    )
    alt_text = (
        "A clear amber perfume bottle displayed against a soft neutral background."
    )

    store = Store(id="ready-store", name="Readiness Test Store", shop_domain="diamondshelf.us")
    product = Product(
        id="ready-product",
        store_id=store.id,
        shopify_product_id="ready-shopify-product",
        handle="amber-perfume",
        title="Amber Perfume",
        vendor="Diamond Shelf",
        status="ACTIVE",
        product_url=product_url,
        inventory_total=1,
        excluded_from_editorial=False,
    )
    local_board = Board(
        id="ready-local-board",
        store_id=store.id,
        name="Fragrance Gifts",
        slug="fragrance-gifts",
        rules={},
        active=True,
    )
    angle = ContentAngle(
        id="ready-angle",
        key="daily-luxury",
        name="Daily luxury",
        active=True,
    )
    connection = PinterestConnection(
        id="ready-connection",
        provider="pinterest",
        external_user_id="fixture-user",
        username="fixture-account",
        access_token_ciphertext="fixture-access-ciphertext",
        refresh_token_ciphertext="fixture-refresh-ciphertext",
        status="CONNECTED",
        boards_last_synced_at=synced_at,
    )
    provider_board = PinterestBoard(
        id="ready-provider-board",
        connection_id=connection.id,
        external_board_id="ready-external-board",
        name=local_board.name,
        routing_label=local_board.slug,
        is_active=True,
        is_eligible=True,
        last_synced_at=synced_at,
    )
    template = CreativeTemplate(
        id="ready-template",
        key="editorial-product",
        version=1,
        name="Editorial Product",
        active=True,
    )
    source_image = ProductImage(
        id="ready-source-image",
        product_id=product.id,
        source_url=media_url,
        alt_text="Amber fragrance bottle",
        width=1000,
        height=1500,
        source_sha256="e" * 64,
        is_primary=True,
        editorial_eligible=True,
    )
    concept = PinConcept(
        id="ready-concept",
        store_id=store.id,
        product_id=product.id,
        content_angle_id=angle.id,
        board_id=local_board.id,
        fingerprint="1" * 64,
        rationale={},
    )
    draft = PinDraft(
        id="ready-draft",
        concept_id=concept.id,
        version=1,
        title=title,
        description=description,
        alt_text=alt_text,
        destination_url=product_url,
        utm_url=utm_url,
        text_fingerprint=text_fingerprint(
            title=title,
            description=description,
            alt_text=alt_text,
        ),
        status=DraftStatus.APPROVED,
    )
    creative = PinCreative(
        id="ready-creative",
        draft_id=draft.id,
        template_id=template.id,
        source_image_id=source_image.id,
        rendered_url=media_url,
        sha256="f" * 64,
        creative_fingerprint="2" * 64,
        width=1000,
        height=1500,
        render_status="RENDERED",
    )
    plan = PinterestPortfolioPlan(
        id="ready-plan",
        store_id=store.id,
        month_start=date(2026, 10, 1),
        month_end=date(2026, 10, 31),
        target_pins=1,
        existing_commitments=0,
        planned_active_slots=1,
        reserve_slots=0,
        policy_version="portfolio-test-v1",
        input_fingerprint="3" * 64,
        plan_fingerprint="4" * 64,
        status="ACTIVE",
        metadata_json={},
    )
    optimizer_fingerprint = "5" * 64
    optimizer_input_fingerprint = "6" * 64
    optimizer = PinterestOptimizerApplication(
        id="ready-optimizer",
        plan_id=plan.id,
        plan_fingerprint_snapshot=plan.plan_fingerprint,
        optimizer_policy_version="optimizer-test-v1",
        optimizer_fingerprint=optimizer_fingerprint,
        input_state_fingerprint=optimizer_input_fingerprint,
        frozen_item_count=0,
        optimizable_item_count=1,
        exploit_count=0,
        explore_count=1,
        recommendation_snapshot={},
        status="APPLIED",
        applied_by="test-fixture",
        applied_at=NOW - timedelta(days=1),
    )
    item_metadata = {
        "candidate_fingerprint": "7" * 64,
        OPTIMIZER_METADATA_KEY: {
            "optimizer_policy_version": optimizer.optimizer_policy_version,
            "optimizer_fingerprint": optimizer_fingerprint,
            "input_state_fingerprint": optimizer_input_fingerprint,
            "recommended_position": 0,
            "target_slot_index": 1,
            "target_planned_date": planned_date.isoformat(),
            "selection_reason": "fixture",
        },
    }
    item = PinterestPortfolioPlanItem(
        id="ready-item",
        plan_id=plan.id,
        slot_index=1,
        is_reserve=False,
        planned_date=planned_date,
        product_id=product.id,
        local_board_id=local_board.id,
        board_key_snapshot=local_board.slug,
        content_angle_id=angle.id,
        angle_key_snapshot=angle.key,
        seed_keywords=["amber fragrance"],
        selection_score=Decimal("1.0"),
        selection_metadata=item_metadata,
        item_fingerprint="0" * 64,
        status="SCHEDULED",
    )
    item.item_fingerprint = _hash({
        "plan_fingerprint": plan.plan_fingerprint,
        "slot_index": item.slot_index,
        "is_reserve": item.is_reserve,
        "planned_date": item.planned_date.isoformat(),
        "candidate_fingerprint": item_metadata["candidate_fingerprint"],
    })
    seo = PinterestSeoBrief(
        id="ready-seo",
        portfolio_item_id=item.id,
        policy_version="seo-test-v1",
        input_fingerprint="8" * 64,
        seo_fingerprint="9" * 64,
        primary_keyword="amber fragrance",
        secondary_keywords=["amber perfume"],
        intent="commercial",
        source_evidence={},
        dimension_scores={},
        coverage_targets={},
        guidance={},
        cannibalization_warnings=[],
        status="CURRENT",
    )
    generation = PinterestAutonomousGenerationRun(
        id="ready-generation",
        portfolio_item_id=item.id,
        seo_brief_id=seo.id,
        input_fingerprint="a" * 64,
        attempt_number=1,
        status="SUCCEEDED",
        concept_id=concept.id,
        draft_id=draft.id,
        creative_id=creative.id,
        safe_metadata={
            "portfolio_item_fingerprint": item.item_fingerprint,
            "seo_fingerprint": seo.seo_fingerprint,
            "provider_called": False,
            "ai_called": False,
        },
        started_at=NOW - timedelta(days=1),
        completed_at=NOW - timedelta(days=1) + timedelta(minutes=1),
    )
    approval = PinApproval(
        id="ready-approval",
        draft_id=draft.id,
        revision_id=None,
        creative_id=creative.id,
        approved_version_id="original",
        decision="APPROVED",
        decided_by=AUTONOMOUS_ACTOR,
    )
    publication = PinPublication(
        id="ready-publication",
        draft_id=draft.id,
        revision_id=None,
        creative_id=creative.id,
        approval_id=approval.id,
        source_image_id=source_image.id,
        template_id=template.id,
        template_key=template.key,
        template_version=template.version,
        text_fingerprint=draft.text_fingerprint,
        creative_fingerprint=creative.creative_fingerprint,
        board_id=None,
        pinterest_board_id=provider_board.external_board_id,
        pinterest_connection_id=connection.id,
        pinterest_board_record_id=provider_board.id,
        pinterest_board_id_snapshot=provider_board.external_board_id,
        title_snapshot=title,
        description_snapshot=description,
        alt_text_snapshot=alt_text,
        media_url_snapshot=media_url,
        destination_url=product_url,
        utm_url=utm_url,
        publication_fingerprint="b" * 64,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=scheduled_for,
    )
    db.add_all([
        store, product, local_board, angle, connection, provider_board,
        template, source_image, concept, draft, creative, plan, optimizer,
        item, seo, generation, approval, publication,
        RoutinePublishingControl(id="default", state="DRY_RUN"),
    ])
    db.flush()

    quality, duplicate, readiness = _snapshots(
        db,
        publication,
        now=NOW,
        expected_status=PublicationStatus.SCHEDULED,
        require_due=True,
    )
    assert quality["status"] == "PASS", quality
    assert duplicate["status"] == "SAFE_TO_CONTINUE", duplicate
    assert readiness["ready"] is True, readiness
    permit = RoutineDispatchPermit(
        id="ready-permit",
        publication_id=publication.id,
        dispatch_provider="buffer",
        approval_id=approval.id,
        pinterest_board_record_id=provider_board.id,
        publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint=request_fingerprint_for(publication),
        scheduled_for_snapshot=scheduled_for,
        quality_policy_version=quality["policy_version"],
        quality_snapshot=quality,
        duplicate_snapshot=duplicate,
        readiness_snapshot=readiness,
        authorized_by=AUTONOMOUS_ACTOR,
        authorized_at=NOW - timedelta(hours=2),
        expires_at=NOW + timedelta(hours=2),
        status="ACTIVE",
    )
    db.add(permit)
    item.publication_id = publication.id
    execution = PinterestAutonomousExecutionRun(
        id="ready-execution",
        portfolio_item_id=item.id,
        plan_id=plan.id,
        optimizer_application_id=optimizer.id,
        input_fingerprint="c" * 64,
        attempt_number=1,
        status="SUCCEEDED",
        stage="PERMITTED",
        seo_brief_id=seo.id,
        generation_run_id=generation.id,
        approval_id=approval.id,
        publication_id=publication.id,
        routine_permit_id=permit.id,
        scheduled_for=scheduled_for,
        safe_metadata={"seo_brief_id": seo.id},
        started_at=NOW - timedelta(days=1),
        completed_at=NOW - timedelta(days=1) + timedelta(minutes=2),
    )
    execution.input_fingerprint = _execution_fingerprint(
        item=item,
        plan=plan,
        optimizer=optimizer,
        optimizer_metadata=item_metadata[OPTIMIZER_METADATA_KEY],
        board=provider_board,
        scheduled_for=scheduled_for,
    )
    db.add(execution)
    db.commit()
    return item, publication, permit


class FakeLease:
    held = True
    supported = True
    last_status = "ACQUIRED"
    last_error = None
    acquired_at = NOW
    lost_at = None

    def validate(self):
        return self.held


class TripwireProviderGateway:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        self.calls.append(name)
        raise AssertionError(f"provider gateway access in DRY_RUN: {name}")


class TrackedSession:
    """Test-owned session facade for verifying scheduler session cleanup."""

    def __init__(self, session):
        self._session = session
        self.closed = False

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        self.closed = True
        self._session.close()


@pytest.mark.asyncio
async def test_scheduler_to_worker_dry_run_never_calls_provider_or_creates_permit(monkeypatch):
    scheduler.reset_scheduler_state_for_tests()
    engine, Session = _database()
    seed = Session()
    publication = _due_publication()
    permit = _existing_permit(publication)
    seed.add_all([
        publication,
        permit,
        RoutinePublishingControl(id="default", state="DRY_RUN"),
    ])
    seed.commit()
    seed.close()

    gateway = TripwireProviderGateway()
    evidence_calls = []
    worker_sessions = []
    monkeypatch.setattr(
        routine_offline_preflight,
        "validate_permit",
        lambda *args, **kwargs: {
            "valid": True,
            "status": "ACTIVE",
            "quality": {"status": "PASS"},
            "duplicate": {"status": "SAFE_TO_CONTINUE"},
        },
    )
    monkeypatch.setattr(
        routine_offline_preflight,
        "_persisted_route",
        lambda *args, **kwargs: (
            SimpleNamespace(id="connection-existing"),
            SimpleNamespace(id="board-existing", external_board_id="external-board-existing"),
        ),
    )
    original_offline_evidence = worker.build_routine_offline_evidence

    def record_offline_evidence(*args, **kwargs):
        result = original_offline_evidence(*args, **kwargs)
        evidence_calls.append(result)
        return result

    monkeypatch.setattr(worker, "build_routine_offline_evidence", record_offline_evidence)
    monkeypatch.setattr(
        worker,
        "validate_permit",
        lambda *args, **kwargs: {"valid": True, "status": "ACTIVE"},
    )

    async def real_worker_runner(db, *, settings):
        return await worker.run_once(
            db,
            settings=settings,
            gateway=gateway,
            now=NOW,
        )

    def tracked_session_factory():
        db = TrackedSession(Session())
        worker_sessions.append(db)
        return db

    settings = _settings()
    result = await scheduler.scheduler_tick(
        settings=settings,
        session_factory=tracked_session_factory,
        runner=real_worker_runner,
        leader_lease=FakeLease(),
    )

    check = Session()
    persisted_publication = check.get(PinPublication, publication.id)
    active_permits = list(check.scalars(
        select(RoutineDispatchPermit).where(
            RoutineDispatchPermit.publication_id == publication.id,
            RoutineDispatchPermit.status == "ACTIVE",
        )
    ).all())
    all_permits = list(check.scalars(
        select(RoutineDispatchPermit).where(
            RoutineDispatchPermit.publication_id == publication.id,
        )
    ).all())
    attempts = list(check.scalars(
        select(PublicationAttempt).where(PublicationAttempt.publication_id == publication.id)
    ).all())
    boundaries = list(check.scalars(
        select(RoutineAttemptBoundary).where(RoutineAttemptBoundary.publication_id == publication.id)
    ).all())
    runs = list(check.scalars(select(RoutinePublishingRun)).all())

    assert result["status"] == "SUCCEEDED"
    assert result["mode"] == "DRY_RUN"
    assert result["scanned"] == 1
    assert result["eligible"] == 1
    assert result["skipped"] == 0
    assert result["dispatched"] == 0
    assert len(evidence_calls) == 1
    assert evidence_calls[0].external_requests == 0
    assert evidence_calls[0].permit_validated is True
    assert gateway.calls == []
    assert len(worker_sessions) == 1 and worker_sessions[0].closed is True
    assert persisted_publication.status == PublicationStatus.SCHEDULED
    assert [row.id for row in active_permits] == [permit.id]
    assert [row.id for row in all_permits] == [permit.id]
    assert attempts == []
    assert boundaries == []
    assert len(runs) == 1 and runs[0].mode == "DRY_RUN"
    check.close()
    scheduler.reset_scheduler_state_for_tests()
    engine.dispose()


@pytest.mark.asyncio
async def test_positive_certificate_gates_scheduler_worker_dry_run_without_writes(monkeypatch):
    scheduler.reset_scheduler_state_for_tests()
    monkeypatch.setattr(
        worker,
        "recover_stale_routine_claims",
        lambda *args, **kwargs: pytest.fail(
            "scheduled autonomous DRY_RUN must not recover stale publication claims"
        ),
    )
    engine, Session = _database()
    seed = Session()
    item, publication, permit = _seed_positive_ready_autonomous_chain(seed)
    seed.close()
    settings = _settings(routine_scheduled_autonomy_enabled=True)

    certificate_session = Session()
    certificate = scheduled_autonomous_readiness(
        certificate_session,
        item.id,
        settings=settings,
        now=NOW,
    )
    assert certificate["ready"] is True, certificate["blockers"]
    assert certificate["blockers"] == []
    assert certificate["state_mutated"] is False
    assert certificate["provider_called"] is False
    assert certificate["provider_calls"] == 0
    assert certificate["permit_created"] is False
    assert certificate["live_ready"] is False
    assert certificate["quota_reservation_committed"] is False
    certificate_session.close()

    gateway = TripwireProviderGateway()
    worker_sessions = []

    async def real_worker_runner(db, *, settings):
        return await worker.run_once(
            db,
            settings=settings,
            gateway=gateway,
            now=NOW,
        )

    def tracked_session_factory():
        db = TrackedSession(Session())
        worker_sessions.append(db)
        return db

    result = await scheduler.scheduler_tick(
        settings=settings,
        session_factory=tracked_session_factory,
        runner=real_worker_runner,
        leader_lease=FakeLease(),
    )

    check = Session()
    persisted_publication = check.get(PinPublication, publication.id)
    all_permits = list(check.scalars(
        select(RoutineDispatchPermit).where(
            RoutineDispatchPermit.publication_id == publication.id,
        )
    ).all())
    attempts = list(check.scalars(
        select(PublicationAttempt).where(PublicationAttempt.publication_id == publication.id)
    ).all())
    boundaries = list(check.scalars(
        select(RoutineAttemptBoundary).where(RoutineAttemptBoundary.publication_id == publication.id)
    ).all())
    runs = list(check.scalars(select(RoutinePublishingRun)).all())
    assert result["status"] == "SUCCEEDED"
    assert result["mode"] == "DRY_RUN"
    assert result["scanned"] == 1
    assert result["eligible"] == 1
    assert result["skipped"] == 0
    assert result["dispatched"] == 0
    assert gateway.calls == []
    assert len(worker_sessions) == 1 and worker_sessions[0].closed is True
    assert persisted_publication.status == PublicationStatus.SCHEDULED
    assert [row.id for row in all_permits] == [permit.id]
    assert attempts == []
    assert boundaries == []
    assert len(runs) == 1 and runs[0].mode == "DRY_RUN"
    certificate_receipts = (runs[0].metadata_json or {}).get(
        "scheduled_autonomy_certificates"
    )
    assert certificate_receipts == [{
        "publication_id": publication.id,
        "portfolio_item_id": item.id,
        "fingerprint": certificate["certificate_fingerprint"],
        "ready": True,
        "blockers": [],
        "offline_validated": True,
        "external_requests": 0,
    }]
    check.close()
    scheduler.reset_scheduler_state_for_tests()
    engine.dispose()


def test_readiness_certificate_for_missing_item_is_bounded_read_only():
    engine, Session = _database()
    db = Session()
    before = db.scalar(select(RoutineDispatchPermit.id).limit(1))

    result = scheduled_autonomous_readiness(
        db,
        "not-a-persisted-item",
        settings=_settings(),
        now=NOW,
    )

    after = db.scalar(select(RoutineDispatchPermit.id).limit(1))
    assert result["ready"] is False
    assert result["blockers"] == ["PORTFOLIO_ITEM_EXISTS"]
    assert result["state_mutated"] is False
    assert result["provider_called"] is False
    assert result["provider_calls"] == 0
    assert result["permit_created"] is False
    assert result["certificate_fingerprint"]
    assert before == after
    db.close()
    engine.dispose()


class EmptyReadOnlyResult:
    def all(self):
        return []


class ExistingItemReadOnlySession:
    def __init__(self):
        self.item = SimpleNamespace(
            id="persisted-plan-item",
            plan_id="persisted-plan",
            is_reserve=False,
            status="SCHEDULED",
            planned_date=None,
            selection_metadata={},
            item_fingerprint="stale",
            publication_id=None,
            local_board_id="local-board",
            board_key_snapshot="board-key",
            product_id="product",
        )

    def get(self, model, ident):
        from app.models.domain import PinterestPortfolioPlanItem

        if model is PinterestPortfolioPlanItem and ident == self.item.id:
            return self.item
        return None

    def scalars(self, _statement):
        return EmptyReadOnlyResult()


def test_readiness_rejects_active_provider_gates_even_with_other_blockers():
    settings = _settings(
        publishing_enabled=True,
        buffer_publishing_enabled=True,
        routine_buffer_dispatch_enabled=True,
        pinterest_write_scope_enabled=True,
    )
    result = scheduled_autonomous_readiness(
        ExistingItemReadOnlySession(),
        "persisted-plan-item",
        settings=settings,
        now=NOW,
    )

    checks = {row["code"]: row for row in result["checks"]}
    assert result["ready"] is False
    assert "PROVIDER_GATES_CLOSED" in result["blockers"]
    assert checks["PROVIDER_GATES_CLOSED"]["context"]["active_gates"] == [
        "buffer_publishing_enabled",
        "pinterest_write_scope_enabled",
        "publishing_enabled",
        "routine_buffer_dispatch_enabled",
    ]
    assert result["state_mutated"] is False
    assert result["provider_calls"] == 0
    assert result["permit_created"] is False


class RouteReadOnlyResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class RouteReadOnlySession:
    def __init__(self, boards):
        self.local = SimpleNamespace(id="local", active=True, slug="board-key", name="Board")
        self.connection = SimpleNamespace(
            id="connection",
            status="CONNECTED",
            provider="pinterest",
            boards_last_synced_at=NOW,
        )
        self.boards = boards

    def get(self, model, ident):
        from app.models.domain import Board

        if model is Board and ident == self.local.id:
            return self.local
        return None

    def scalars(self, statement):
        model = statement.column_descriptions[0]["entity"]
        from app.models.domain import Board, PinterestBoard, PinterestConnection

        if model is Board:
            return RouteReadOnlyResult([self.local])
        if model is PinterestConnection:
            return RouteReadOnlyResult([self.connection])
        if model is PinterestBoard:
            return RouteReadOnlyResult(self.boards)
        raise AssertionError(f"unexpected read query: {model}")


def test_persisted_board_route_rejects_missing_and_ambiguous_rows():
    item = SimpleNamespace(local_board_id="local", board_key_snapshot="board-key")
    publication = SimpleNamespace(
        pinterest_connection_id="connection",
        pinterest_board_record_id="selected-board",
        pinterest_board_id_snapshot="external-board",
    )
    route, reason = _current_persisted_route(RouteReadOnlySession([]), item, publication)
    assert route is None
    assert reason == "PERSISTED_PINTEREST_ROUTING_REQUIRED"

    duplicate = SimpleNamespace(routing_label="board-key", name="Board")
    ambiguous = RouteReadOnlySession([duplicate, duplicate])
    route, reason = _current_persisted_route(ambiguous, item, publication)
    assert route is None
    assert reason == "PERSISTED_PINTEREST_ROUTING_AMBIGUOUS"