from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

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
    PublicationStatus,
    Store,
)
from app.models.routine_publishing import RoutineDispatchPermit
from app.services import pinterest_autonomous_execution as execution
from app.services.pinterest_optimizer_apply import OPTIMIZER_METADATA_KEY


NOW = datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc)


def _db():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session()


def _settings(**overrides):
    values = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "pinterest_seo_brief_persistence_enabled": True,
        "pinterest_autonomous_generation_enabled": True,
        "routine_autonomous_authorization_enabled": True,
        "pinterest_autonomous_execution_enabled": True,
        "pinterest_autonomous_schedule_start_minute_utc": 840,
        "pinterest_autonomous_schedule_end_minute_utc": 1320,
        "pinterest_board_write_scope_enabled": False,
        "pinterest_board_provisioning_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def _seed(db, *, plan_status="ACTIVE", two_same_day=True):
    store = Store(
        id="store-1",
        name="Diamond Shelf",
        shop_domain="diamondshelf.us",
        market="US",
    )
    board = Board(
        id="board-1",
        store_id=store.id,
        name="Arabian Fragrance",
        slug="arabian-fragrance",
        rules={},
        active=True,
    )
    angle = ContentAngle(
        id="angle-1",
        key="arabian-fragrance-discovery",
        name="Arabian Fragrance Discovery",
        rules={},
        active=True,
    )
    product1 = Product(
        id="product-1",
        store_id=store.id,
        shopify_product_id="shop-1",
        handle="afnan-9pm",
        title="Afnan 9PM",
        vendor="Afnan",
        product_url="https://diamondshelf.us/products/afnan-9pm",
        status="ACTIVE",
        inventory_total=10,
    )
    product2 = Product(
        id="product-2",
        store_id=store.id,
        shopify_product_id="shop-2",
        handle="afnan-supremacy",
        title="Afnan Supremacy",
        vendor="Afnan",
        product_url="https://diamondshelf.us/products/afnan-supremacy",
        status="ACTIVE",
        inventory_total=10,
    )
    template = CreativeTemplate(
        id="template-1",
        key="product_classification",
        version=1,
        name="Product Classification",
        active=True,
    )
    image1 = ProductImage(
        id="image-1",
        product_id=product1.id,
        source_url="https://cdn.example/1.png",
        source_sha256="a" * 64,
        is_primary=True,
        editorial_eligible=True,
    )
    image2 = ProductImage(
        id="image-2",
        product_id=product2.id,
        source_url="https://cdn.example/2.png",
        source_sha256="b" * 64,
        is_primary=True,
        editorial_eligible=True,
    )
    plan = PinterestPortfolioPlan(
        id="plan-1",
        store_id=store.id,
        month_start=date(2026, 9, 1),
        month_end=date(2026, 9, 30),
        target_pins=150,
        existing_commitments=0,
        planned_active_slots=2 if two_same_day else 1,
        reserve_slots=0,
        policy_version="PINTEREST_PORTFOLIO_V2",
        input_fingerprint="c" * 64,
        plan_fingerprint="d" * 64,
        status=plan_status,
        metadata_json={},
    )
    db.add_all([store, board, angle, product1, product2, template, image1, image2, plan])
    db.flush()

    app = PinterestOptimizerApplication(
        id="optimizer-app-1",
        plan_id=plan.id,
        plan_fingerprint_snapshot=plan.plan_fingerprint,
        optimizer_policy_version="PINTEREST_OPTIMIZER_V1",
        optimizer_fingerprint="optimizer-fingerprint-1",
        learning_fingerprint="learning-fingerprint-1",
        input_state_fingerprint="input-state-1",
        frozen_item_count=0,
        optimizable_item_count=2 if two_same_day else 1,
        exploit_count=1,
        explore_count=1 if two_same_day else 0,
        recommendation_snapshot={},
        status="APPLIED",
        applied_by="adaptive-optimizer-v1",
        applied_at=NOW - timedelta(hours=1),
    )
    db.add(app)
    db.flush()

    def item(item_id, slot, product_id, position):
        return PinterestPortfolioPlanItem(
            id=item_id,
            plan_id=plan.id,
            slot_index=slot,
            is_reserve=False,
            planned_date=date(2026, 9, 21),
            product_id=product_id,
            local_board_id=board.id,
            board_key_snapshot=board.slug,
            content_angle_id=angle.id,
            angle_key_snapshot=angle.key,
            seed_keywords=["arabian fragrance"],
            selection_score=Decimal("10.000000") - Decimal(slot),
            selection_metadata={
                OPTIMIZER_METADATA_KEY: {
                    "optimizer_policy_version": app.optimizer_policy_version,
                    "optimizer_fingerprint": app.optimizer_fingerprint,
                    "input_state_fingerprint": app.input_state_fingerprint,
                    "recommended_position": position,
                    "target_slot_index": slot,
                    "target_planned_date": "2026-09-21",
                    "selection_reason": "EXPLOIT",
                }
            },
            item_fingerprint=(str(slot) * 64)[:64],
            status="PLANNED",
        )

    item1 = item("item-1", 1, product1.id, 1)
    db.add(item1)
    item2 = None
    if two_same_day:
        item2 = item("item-2", 2, product2.id, 2)
        db.add(item2)

    connection = PinterestConnection(
        id="connection-1",
        external_user_id="pinterest-user-1",
        username="diamond-shelf",
        granted_scopes=["user_accounts:read", "boards:read", "pins:read"],
        access_token_ciphertext="cipher-a",
        refresh_token_ciphertext="cipher-r",
        status="CONNECTED",
        boards_last_synced_at=NOW,
    )
    provider_board = PinterestBoard(
        id="pinterest-board-row-1",
        connection_id=connection.id,
        external_board_id="external-board-1",
        name="Arabian Fragrance",
        privacy="PUBLIC",
        is_active=True,
        is_eligible=True,
        routing_label=board.slug,
        last_seen_at=NOW,
        last_synced_at=NOW,
    )
    db.add_all([connection, provider_board])
    db.commit()
    return {
        "store": store,
        "plan": plan,
        "app": app,
        "item1": item1,
        "item2": item2,
        "board": board,
        "provider_board": provider_board,
        "connection": connection,
        "template": template,
        "image1": image1,
    }


def _fake_seo(db, item_id, *, settings=None):
    existing = db.scalar(
        select(PinterestSeoBrief)
        .where(PinterestSeoBrief.portfolio_item_id == item_id)
        .limit(1)
    )
    if existing:
        return existing
    row = PinterestSeoBrief(
        id=f"seo-{item_id}",
        portfolio_item_id=item_id,
        policy_version="PINTEREST_SEO_V1",
        input_fingerprint="e" * 64,
        seo_fingerprint="f" * 64,
        primary_keyword="arabian fragrance",
        secondary_keywords=["afnan perfume"],
        intent="product_discovery",
        source_evidence={},
        dimension_scores={},
        coverage_targets={},
        guidance={},
        cannibalization_warnings=[],
        status="CURRENT",
    )
    db.add(row)
    db.commit()
    return row


def _fake_generation(db, item_id, *, settings=None, renderer=None, now=None):
    existing = db.scalar(
        select(PinterestAutonomousGenerationRun)
        .where(PinterestAutonomousGenerationRun.portfolio_item_id == item_id)
        .limit(1)
    )
    if existing:
        return existing

    item = db.get(PinterestPortfolioPlanItem, item_id)
    concept = PinConcept(
        id=f"concept-{item_id}",
        store_id="store-1",
        product_id=item.product_id,
        content_angle_id=item.content_angle_id,
        board_id=item.local_board_id,
        fingerprint="1" * 63 + "a",
        rationale={"unsupported_claims": []},
    )
    draft = PinDraft(
        id=f"draft-{item_id}",
        concept_id=concept.id,
        version=1,
        title="Arabian fragrance discovery",
        description="Explore Arabian fragrance at Diamond Shelf.",
        alt_text="Arabian fragrance bottle",
        destination_url="https://diamondshelf.us/products/afnan-9pm",
        utm_url="https://diamondshelf.us/products/afnan-9pm?utm_source=pinterest",
        text_fingerprint="2" * 64,
        status=DraftStatus.READY_FOR_REVIEW,
    )
    creative = PinCreative(
        id=f"creative-{item_id}",
        draft_id=draft.id,
        template_id="template-1",
        source_image_id="image-1",
        rendered_url=f"https://diamondshelf.replit.app/media/{item_id}.png",
        sha256="3" * 64,
        creative_fingerprint="4" * 64,
        width=1000,
        height=1500,
        render_status="RENDERED",
    )
    seo = db.scalar(
        select(PinterestSeoBrief)
        .where(PinterestSeoBrief.portfolio_item_id == item_id)
        .limit(1)
    )
    row = PinterestAutonomousGenerationRun(
        id=f"generation-{item_id}",
        portfolio_item_id=item_id,
        seo_brief_id=seo.id,
        input_fingerprint="5" * 64,
        status="SUCCEEDED",
        concept_id=concept.id,
        draft_id=draft.id,
        creative_id=creative.id,
        safe_metadata={"provider_called": False, "ai_called": False},
        started_at=now or NOW,
        completed_at=now or NOW,
    )
    db.add_all([concept, draft, creative, row])
    item.status = "GENERATED"
    db.commit()
    return row


def _fake_authorize(db, draft_id, *, settings=None, now=None):
    existing = db.scalar(
        select(PinApproval)
        .where(PinApproval.draft_id == draft_id)
        .limit(1)
    )
    if existing:
        return existing
    creative = db.scalar(
        select(PinCreative)
        .where(PinCreative.draft_id == draft_id)
        .limit(1)
    )
    draft = db.get(PinDraft, draft_id)
    draft.status = DraftStatus.APPROVED
    row = PinApproval(
        id=f"approval-{draft_id}",
        draft_id=draft_id,
        revision_id=None,
        creative_id=creative.id,
        approved_version_id="original",
        decision="APPROVED",
        decided_by="autonomous-policy-v1",
        note="AUTONOMOUS_POLICY_V1:test",
    )
    db.add(row)
    db.commit()
    return row


class _FakePublicationService:
    def __init__(self, db):
        self.db = db
        self.calls = 0

    def create_snapshot(
        self,
        *,
        approval_id,
        board_id,
        integration_account_id=None,
        pinterest_connection_id=None,
        pinterest_board_record_id=None,
        scheduled_for=None,
    ):
        self.calls += 1
        approval = self.db.get(PinApproval, approval_id)
        creative = self.db.get(PinCreative, approval.creative_id)
        row = PinPublication(
            id=f"publication-{approval.id}",
            draft_id=approval.draft_id,
            revision_id=approval.revision_id,
            creative_id=creative.id,
            approval_id=approval.id,
            source_image_id=creative.source_image_id,
            template_id=creative.template_id,
            board_id=board_id,
            pinterest_board_id="external-board-1",
            pinterest_connection_id=pinterest_connection_id,
            pinterest_board_record_id=pinterest_board_record_id,
            pinterest_board_id_snapshot="external-board-1",
            publication_fingerprint="6" * 64,
            status=PublicationStatus.SCHEDULED,
            scheduled_for=scheduled_for,
        )
        self.db.add(row)
        self.db.commit()
        return row


def _fake_permit(db, publication_id, *, settings=None, now=None):
    existing = db.scalar(
        select(RoutineDispatchPermit)
        .where(
            RoutineDispatchPermit.publication_id == publication_id,
            RoutineDispatchPermit.status == "ACTIVE",
        )
        .limit(1)
    )
    if existing:
        return existing
    publication = db.get(PinPublication, publication_id)
    row = RoutineDispatchPermit(
        id=f"permit-{publication_id}",
        publication_id=publication.id,
        dispatch_provider="buffer",
        approval_id=publication.approval_id,
        pinterest_board_record_id=publication.pinterest_board_record_id,
        publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint="7" * 64,
        scheduled_for_snapshot=publication.scheduled_for,
        quality_policy_version="PINTEREST_QUALITY_V1",
        quality_snapshot={},
        duplicate_snapshot={},
        readiness_snapshot={"dispatch_provider": "buffer"},
        authorized_by="autonomous-policy-v1",
        authorized_at=now or NOW,
        expires_at=(now or NOW) + timedelta(days=2),
        status="ACTIVE",
    )
    db.add(row)
    db.commit()
    return row


def _patch_happy(monkeypatch, db):
    fake_pub = _FakePublicationService(db)
    monkeypatch.setattr(execution, "persist_seo_brief", _fake_seo)
    monkeypatch.setattr(execution, "execute_autonomous_generation", _fake_generation)
    monkeypatch.setattr(execution, "authorize_draft_autonomously", _fake_authorize)
    monkeypatch.setattr(execution, "_publication_service", lambda _db: fake_pub)
    monkeypatch.setattr(execution, "auto_permit_publication", _fake_permit)
    return fake_pub


def test_defaults_disabled_and_schedule_window_validated():
    settings = Settings(database_url="sqlite:///:memory:")
    assert settings.pinterest_autonomous_execution_enabled is False
    assert settings.pinterest_autonomous_schedule_start_minute_utc == 840
    assert settings.pinterest_autonomous_schedule_end_minute_utc == 1320

    with pytest.raises(ValidationError):
        Settings(
            database_url="sqlite:///:memory:",
            pinterest_autonomous_schedule_start_minute_utc=1320,
            pinterest_autonomous_schedule_end_minute_utc=840,
        )


def test_deterministic_intraday_schedule_uses_stable_optimizer_order():
    engine, db = _db()
    seeded = _seed(db, two_same_day=True)
    first = execution.deterministic_scheduled_for(db, seeded["item1"], settings=_settings())
    second = execution.deterministic_scheduled_for(db, seeded["item2"], settings=_settings())

    assert first == datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc)
    assert second == datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc)

    # Query order and repeated evaluation cannot change the midpoint assignment.
    assert execution.deterministic_scheduled_for(db, seeded["item1"], settings=_settings()) == first
    db.close(); engine.dispose()


def test_execution_readiness_safe_case_is_provider_free_and_read_only():
    engine, db = _db()
    seeded = _seed(db)
    before = (
        db.query(PinterestAutonomousExecutionRun).count(),
        len(db.new),
        len(db.dirty),
        len(db.deleted),
    )
    result = execution.execution_readiness(
        db,
        seeded["item1"].id,
        settings=_settings(),
        now=NOW,
    )
    after = (
        db.query(PinterestAutonomousExecutionRun).count(),
        len(db.new),
        len(db.dirty),
        len(db.deleted),
    )

    assert result["ready"] is True
    assert result["blockers"] == []
    assert result["scheduled_for"] == datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc)
    assert result["board_routing"]["status"] == "ROUTE_EXISTING"
    assert len(result["input_fingerprint"]) == 64
    assert result["state_mutated"] is False
    assert result["provider_called"] is False
    assert result["ai_called"] is False
    assert before == after
    db.close(); engine.dispose()


def test_reserve_and_missing_date_block():
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    item = seeded["item1"]
    item.is_reserve = True
    item.planned_date = None
    db.commit()

    result = execution.execution_readiness(db, item.id, settings=_settings(), now=NOW)
    assert "RESERVE_ITEM_NOT_EXECUTABLE" in result["blockers"]
    assert "PLANNED_DATE_REQUIRED" in result["blockers"]
    assert result["ready"] is False
    db.close(); engine.dispose()


def test_inactive_plan_and_optimizer_binding_fail_closed():
    engine, db = _db()
    seeded = _seed(db, plan_status="DRAFT", two_same_day=False)

    result = execution.execution_readiness(db, seeded["item1"].id, settings=_settings(), now=NOW)
    assert "PORTFOLIO_PLAN_NOT_ACTIVE" in result["blockers"]

    seeded["plan"].status = "ACTIVE"
    seeded["item1"].selection_metadata = {
        OPTIMIZER_METADATA_KEY: {
            **seeded["item1"].selection_metadata[OPTIMIZER_METADATA_KEY],
            "optimizer_fingerprint": "different",
        }
    }
    db.commit()
    result = execution.execution_readiness(db, seeded["item1"].id, settings=_settings(), now=NOW)
    assert "OPTIMIZER_ITEM_BINDING_MISMATCH" in result["blockers"]

    db.delete(seeded["app"])
    db.commit()
    result = execution.execution_readiness(db, seeded["item1"].id, settings=_settings(), now=NOW)
    assert "OPTIMIZER_APPLICATION_REQUIRED" in result["blockers"]
    db.close(); engine.dispose()


def test_publish_unknown_and_unroutable_board_block():
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)

    concept = PinConcept(
        id="unknown-concept",
        store_id="store-1",
        product_id="product-1",
        content_angle_id="angle-1",
        board_id="board-1",
        fingerprint="8" * 64,
        rationale={},
    )
    draft = PinDraft(
        id="unknown-draft",
        concept_id=concept.id,
        version=1,
        title="x",
        description="x",
        alt_text="x",
        destination_url="https://x",
        utm_url="https://x",
        text_fingerprint="9" * 64,
        status=DraftStatus.APPROVED,
    )
    creative = PinCreative(
        id="unknown-creative",
        draft_id=draft.id,
        template_id="template-1",
        source_image_id="image-1",
        creative_fingerprint="a" * 64,
    )
    unknown = PinPublication(
        id="unknown-publication",
        draft_id=draft.id,
        creative_id=creative.id,
        board_id="board-1",
        publication_fingerprint="b" * 64,
        status=PublicationStatus.PUBLISH_UNKNOWN,
    )
    db.add_all([concept, draft, creative, unknown])
    db.commit()

    result = execution.execution_readiness(db, seeded["item1"].id, settings=_settings(), now=NOW)
    assert "PUBLISH_UNKNOWN_PRESENT" in result["blockers"]

    unknown.status = PublicationStatus.CANCELLED
    seeded["provider_board"].is_eligible = False
    db.commit()
    result = execution.execution_readiness(db, seeded["item1"].id, settings=_settings(), now=NOW)
    assert "ROUTABLE_PINTEREST_BOARD_REQUIRED" in result["blockers"]
    db.close(); engine.dispose()


def test_subordinate_flags_are_all_required():
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    settings = _settings(
        pinterest_seo_brief_persistence_enabled=False,
        pinterest_autonomous_generation_enabled=False,
        routine_autonomous_authorization_enabled=False,
        pinterest_autonomous_execution_enabled=False,
    )
    result = execution.execution_readiness(db, seeded["item1"].id, settings=settings, now=NOW)
    assert {
        "SEO_BRIEF_PERSISTENCE_DISABLED",
        "AUTONOMOUS_GENERATION_DISABLED",
        "AUTONOMOUS_AUTHORIZATION_DISABLED",
        "AUTONOMOUS_EXECUTION_DISABLED",
    }.issubset(set(result["blockers"]))
    db.close(); engine.dispose()


def test_started_run_is_persisted_before_first_downstream_mutation(monkeypatch):
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)

    def fail_after_asserting_started(db_arg, item_id, *, settings=None):
        row = db_arg.scalar(
            select(PinterestAutonomousExecutionRun)
            .where(PinterestAutonomousExecutionRun.portfolio_item_id == item_id)
        )
        assert row is not None
        assert row.status == "STARTED"
        assert row.stage == "STARTED"
        raise RuntimeError("synthetic downstream failure")

    monkeypatch.setattr(execution, "persist_seo_brief", fail_after_asserting_started)

    with pytest.raises(execution.AutonomousExecutionError, match="RuntimeError"):
        execution.execute_autonomous_item(
            db,
            seeded["item1"].id,
            settings=_settings(),
            now=NOW,
        )

    row = db.scalar(select(PinterestAutonomousExecutionRun))
    assert row.status == "FAILED"
    assert row.stage == "STARTED"
    assert row.safe_metadata["error_code"] == "RuntimeError"
    db.close(); engine.dispose()


def test_happy_path_reaches_permitted_and_exact_repeat_is_idempotent(monkeypatch):
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    fake_pub = _patch_happy(monkeypatch, db)

    first = execution.execute_autonomous_item(
        db,
        seeded["item1"].id,
        settings=_settings(),
        now=NOW,
    )
    assert first.status == "SUCCEEDED"
    assert first.stage == "PERMITTED"
    assert first.seo_brief_id
    assert first.generation_run_id
    assert first.approval_id
    assert first.publication_id
    assert first.routine_permit_id
    item = db.get(PinterestPortfolioPlanItem, seeded["item1"].id)
    assert item.status == "SCHEDULED"
    assert item.publication_id == first.publication_id
    assert fake_pub.calls == 1

    # An exact successful repeat must return without re-running subordinate work.
    monkeypatch.setattr(
        execution,
        "persist_seo_brief",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not rerun")),
    )
    monkeypatch.setattr(
        execution,
        "execute_autonomous_generation",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not rerun")),
    )
    second = execution.execute_autonomous_item(
        db,
        seeded["item1"].id,
        settings=_settings(),
        now=NOW,
    )
    assert second.id == first.id
    assert db.query(PinterestAutonomousExecutionRun).count() == 1
    db.close(); engine.dispose()


def test_crash_recovery_discovers_committed_publication_before_item_link(monkeypatch):
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    settings = _settings()
    ready = execution.execution_readiness(db, seeded["item1"].id, settings=settings, now=NOW)
    seo = _fake_seo(db, seeded["item1"].id, settings=settings)
    generation = _fake_generation(db, seeded["item1"].id, settings=settings, now=NOW)
    approval = _fake_authorize(db, generation.draft_id, settings=settings, now=NOW)

    run = PinterestAutonomousExecutionRun(
        id="exec-recovery",
        portfolio_item_id=seeded["item1"].id,
        plan_id=seeded["plan"].id,
        optimizer_application_id=seeded["app"].id,
        input_fingerprint=ready["input_fingerprint"],
        status="STARTED",
        stage="AUTHORIZED",
        seo_brief_id=seo.id,
        generation_run_id=generation.id,
        approval_id=approval.id,
        scheduled_for=ready["scheduled_for"],
        safe_metadata={},
        started_at=NOW,
    )
    db.add(run)
    db.commit()

    publication = PinPublication(
        id="publication-recovered",
        draft_id=generation.draft_id,
        creative_id=generation.creative_id,
        approval_id=approval.id,
        board_id=seeded["board"].id,
        pinterest_board_id=seeded["provider_board"].external_board_id,
        pinterest_connection_id=seeded["connection"].id,
        pinterest_board_record_id=seeded["provider_board"].id,
        pinterest_board_id_snapshot=seeded["provider_board"].external_board_id,
        publication_fingerprint="c" * 64,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=ready["scheduled_for"],
    )
    db.add(publication)
    db.commit()

    monkeypatch.setattr(
        execution,
        "_publication_service",
        lambda _db: (_ for _ in ()).throw(AssertionError("must recover instead of create")),
    )
    monkeypatch.setattr(execution, "auto_permit_publication", _fake_permit)

    result = execution.execute_autonomous_item(
        db,
        seeded["item1"].id,
        settings=settings,
        now=NOW,
    )
    assert result.status == "SUCCEEDED"
    assert result.publication_id == publication.id
    item = db.get(PinterestPortfolioPlanItem, seeded["item1"].id)
    assert item.publication_id == publication.id
    assert item.status == "SCHEDULED"
    db.close(); engine.dispose()


def test_publication_recovery_ambiguity_fails_closed(monkeypatch):
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    settings = _settings()
    ready = execution.execution_readiness(db, seeded["item1"].id, settings=settings, now=NOW)
    seo = _fake_seo(db, seeded["item1"].id, settings=settings)
    generation = _fake_generation(db, seeded["item1"].id, settings=settings, now=NOW)
    approval = _fake_authorize(db, generation.draft_id, settings=settings, now=NOW)
    run = PinterestAutonomousExecutionRun(
        id="exec-ambiguous",
        portfolio_item_id=seeded["item1"].id,
        plan_id=seeded["plan"].id,
        optimizer_application_id=seeded["app"].id,
        input_fingerprint=ready["input_fingerprint"],
        status="STARTED",
        stage="AUTHORIZED",
        seo_brief_id=seo.id,
        generation_run_id=generation.id,
        approval_id=approval.id,
        scheduled_for=ready["scheduled_for"],
        safe_metadata={},
        started_at=NOW,
    )
    db.add(run)
    for suffix, fp in [("a", "d" * 64), ("b", "e" * 64)]:
        db.add(PinPublication(
            id=f"publication-{suffix}",
            draft_id=generation.draft_id,
            creative_id=generation.creative_id,
            approval_id=approval.id,
            board_id=seeded["board"].id,
            pinterest_board_id=seeded["provider_board"].external_board_id,
            pinterest_connection_id=seeded["connection"].id,
            pinterest_board_record_id=seeded["provider_board"].id,
            pinterest_board_id_snapshot=seeded["provider_board"].external_board_id,
            publication_fingerprint=fp,
            status=PublicationStatus.SCHEDULED,
            scheduled_for=ready["scheduled_for"],
        ))
    db.commit()

    with pytest.raises(execution.AutonomousExecutionError, match="PUBLICATION_RECOVERY_AMBIGUOUS"):
        execution.execute_autonomous_item(
            db,
            seeded["item1"].id,
            settings=settings,
            now=NOW,
        )
    failed = db.get(PinterestAutonomousExecutionRun, run.id)
    assert failed.status == "FAILED"
    db.close(); engine.dispose()


def test_failed_run_blocks_retry():
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    ready = execution.execution_readiness(db, seeded["item1"].id, settings=_settings(), now=NOW)
    db.add(PinterestAutonomousExecutionRun(
        id="failed-run",
        portfolio_item_id=seeded["item1"].id,
        plan_id=seeded["plan"].id,
        optimizer_application_id=seeded["app"].id,
        input_fingerprint=ready["input_fingerprint"],
        status="FAILED",
        stage="STARTED",
        scheduled_for=ready["scheduled_for"],
        safe_metadata={"error_code": "SYNTHETIC"},
        started_at=NOW,
        completed_at=NOW,
    ))
    db.commit()

    with pytest.raises(
        execution.AutonomousExecutionError,
        match="AUTONOMOUS_EXECUTION_FAILED_RECONCILIATION_REQUIRED",
    ):
        execution.execute_autonomous_item(
            db,
            seeded["item1"].id,
            settings=_settings(),
            now=NOW,
        )
    db.close(); engine.dispose()


def test_past_schedule_blocks_new_execution():
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    late = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
    result = execution.execution_readiness(
        db,
        seeded["item1"].id,
        settings=_settings(),
        now=late,
    )
    assert "SCHEDULE_TIME_NOT_FUTURE" in result["blockers"]
    assert result["ready"] is False
    db.close(); engine.dispose()


# Task #58 — autonomous board ensure + portfolio execution coordinator.
import asyncio
from types import SimpleNamespace

from app.models.domain import (
    PinterestAutonomousDestinationRun,
    PinterestBoardProvisioningAttempt,
)
from app.services import pinterest_autonomous_destination as destination


def _destination_settings(**overrides):
    values = {
        "pinterest_autonomous_board_ensure_enabled": True,
        "pinterest_board_write_scope_enabled": True,
        "pinterest_board_provisioning_enabled": True,
    }
    values.update(overrides)
    return _settings(**values)


def _remove_routed_provider_board(db, seeded):
    db.delete(db.get(PinterestBoard, seeded["provider_board"].id))
    connection = db.get(PinterestConnection, seeded["connection"].id)
    connection.granted_scopes = [
        "user_accounts:read",
        "boards:read",
        "pins:read",
        "boards:write",
    ]
    db.commit()


def _successful_task57(*args, **kwargs):
    return SimpleNamespace(
        id="execution-destination-1",
        status="SUCCEEDED",
        stage="PERMITTED",
    )


def _fake_board_create_success(db, attempt_id, **kwargs):
    attempt = db.get(PinterestBoardProvisioningAttempt, attempt_id)
    attempt.provider_mutation_started_at = kwargs.get("now") or NOW
    attempt.status = "SUCCEEDED"
    attempt.provider_board_id = "created-board-1"
    attempt.completed_at = kwargs.get("now") or NOW
    attempt.safe_metadata = {"provider_called": True, "classification": "SUCCEEDED"}
    db.commit()
    db.refresh(attempt)
    return attempt


async def _fake_sync_created_board(db, connection, client=None):
    synced = NOW + timedelta(minutes=1)
    connection.boards_last_synced_at = synced
    existing = db.scalar(
        select(PinterestBoard).where(
            PinterestBoard.connection_id == connection.id,
            PinterestBoard.external_board_id == "created-board-1",
        )
    )
    if existing is None:
        existing = PinterestBoard(
            id="created-board-row-1",
            connection_id=connection.id,
            external_board_id="created-board-1",
            name="Arabian Fragrance",
            privacy="PUBLIC",
            is_active=True,
            is_eligible=False,
            routing_label=None,
            last_seen_at=synced,
            last_synced_at=synced,
        )
        db.add(existing)
    else:
        existing.last_synced_at = synced
        existing.last_seen_at = synced
        existing.is_active = True
    db.commit()


def test_destination_readiness_is_non_mutating_and_get_route_is_registered():
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    settings = _destination_settings()
    before = (
        db.query(PinterestAutonomousDestinationRun).count(),
        db.query(PinterestBoardProvisioningAttempt).count(),
        len(db.new),
        len(db.dirty),
    )
    result = destination.destination_readiness(
        db, seeded["item1"].id, settings=settings, now=NOW
    )
    after = (
        db.query(PinterestAutonomousDestinationRun).count(),
        db.query(PinterestBoardProvisioningAttempt).count(),
        len(db.new),
        len(db.dirty),
    )
    assert result["ready"] is True
    assert result["state_mutated"] is False
    assert result["provider_called"] is False
    assert before == after

    from app.api.routes import portfolio as portfolio_routes
    assert any(
        getattr(route, "path", "").endswith(
            "/portfolio/items/{portfolio_item_id}/destination-readiness"
        )
        and "GET" in getattr(route, "methods", set())
        for route in portfolio_routes.router.routes
    )
    db.close(); engine.dispose()


def test_destination_existing_board_executes_without_board_provider_calls(monkeypatch):
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    monkeypatch.setattr(destination, "execute_autonomous_item", _successful_task57)
    monkeypatch.setattr(
        destination,
        "start_board_provisioning",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("existing-board path must not provision")
        ),
    )
    async def no_sync(*a, **k):
        raise AssertionError("existing-board path must not sync")
    monkeypatch.setattr(destination, "sync_boards", no_sync)

    run = asyncio.run(destination.ensure_autonomous_destination(
        db,
        seeded["item1"].id,
        settings=_destination_settings(),
        now=NOW,
    ))
    assert run.status == "SUCCEEDED"
    assert run.stage == "EXECUTION_READY"
    assert run.pinterest_board_record_id == seeded["provider_board"].id
    assert run.autonomous_execution_run_id == "execution-destination-1"
    db.close(); engine.dispose()


def test_destination_missing_board_creates_once_syncs_reconciles_and_executes(monkeypatch):
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    _remove_routed_provider_board(db, seeded)
    calls = {"create": 0, "sync": 0, "execute": 0}

    async def create(db_arg, attempt_id, **kwargs):
        calls["create"] += 1
        return _fake_board_create_success(db_arg, attempt_id, **kwargs)

    async def sync(db_arg, connection, client=None):
        calls["sync"] += 1
        await _fake_sync_created_board(db_arg, connection, client=client)

    def execute(*args, **kwargs):
        calls["execute"] += 1
        return _successful_task57()

    monkeypatch.setattr(destination, "execute_board_provisioning_attempt", create)
    monkeypatch.setattr(destination, "sync_boards", sync)
    monkeypatch.setattr(destination, "execute_autonomous_item", execute)

    run = asyncio.run(destination.ensure_autonomous_destination(
        db,
        seeded["item1"].id,
        settings=_destination_settings(),
        now=NOW,
    ))
    assert run.status == "SUCCEEDED"
    assert run.stage == "EXECUTION_READY"
    assert calls == {"create": 1, "sync": 1, "execute": 1}
    attempt = db.get(PinterestBoardProvisioningAttempt, run.board_provisioning_attempt_id)
    assert attempt.status == "SUCCEEDED"
    board = db.get(PinterestBoard, run.pinterest_board_record_id)
    assert board.routing_label == "arabian-fragrance"
    assert board.is_eligible is True
    db.close(); engine.dispose()


def test_destination_resumes_sync_pending_without_replaying_create(monkeypatch):
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    _remove_routed_provider_board(db, seeded)
    create_calls = {"n": 0}
    sync_calls = {"n": 0}

    async def create(db_arg, attempt_id, **kwargs):
        create_calls["n"] += 1
        return _fake_board_create_success(db_arg, attempt_id, **kwargs)

    async def first_sync(*args, **kwargs):
        sync_calls["n"] += 1
        raise RuntimeError("board not visible yet")

    monkeypatch.setattr(destination, "execute_board_provisioning_attempt", create)
    monkeypatch.setattr(destination, "sync_boards", first_sync)
    monkeypatch.setattr(destination, "execute_autonomous_item", _successful_task57)

    first = asyncio.run(destination.ensure_autonomous_destination(
        db, seeded["item1"].id, settings=_destination_settings(), now=NOW
    ))
    assert first.status == "STARTED"
    assert first.stage == "BOARD_SYNC_PENDING"
    assert create_calls["n"] == 1

    async def second_sync(db_arg, connection, client=None):
        sync_calls["n"] += 1
        await _fake_sync_created_board(db_arg, connection, client=client)

    monkeypatch.setattr(destination, "sync_boards", second_sync)
    second = asyncio.run(destination.ensure_autonomous_destination(
        db, seeded["item1"].id, settings=_destination_settings(), now=NOW
    ))
    assert second.status == "SUCCEEDED"
    assert second.stage == "EXECUTION_READY"
    assert create_calls["n"] == 1
    assert sync_calls["n"] == 2
    db.close(); engine.dispose()


def test_destination_sync_exception_is_resumable_without_replaying_create(monkeypatch):
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    _remove_routed_provider_board(db, seeded)
    create_calls = {"n": 0}

    async def create(db_arg, attempt_id, **kwargs):
        create_calls["n"] += 1
        return _fake_board_create_success(db_arg, attempt_id, **kwargs)

    async def broken_sync(*args, **kwargs):
        raise OSError("temporary GET failure")

    monkeypatch.setattr(destination, "execute_board_provisioning_attempt", create)
    monkeypatch.setattr(destination, "sync_boards", broken_sync)
    monkeypatch.setattr(destination, "execute_autonomous_item", _successful_task57)

    first = asyncio.run(destination.ensure_autonomous_destination(
        db, seeded["item1"].id, settings=_destination_settings(), now=NOW
    ))
    assert first.stage == "BOARD_SYNC_PENDING"

    async def recovered_sync(db_arg, connection, client=None):
        await _fake_sync_created_board(db_arg, connection, client=client)

    monkeypatch.setattr(destination, "sync_boards", recovered_sync)
    second = asyncio.run(destination.ensure_autonomous_destination(
        db, seeded["item1"].id, settings=_destination_settings(), now=NOW
    ))
    assert second.status == "SUCCEEDED"
    assert create_calls["n"] == 1
    db.close(); engine.dispose()


def test_destination_concurrent_missing_board_has_one_coordinator_owner(monkeypatch):
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    _remove_routed_provider_board(db, seeded)
    calls = {"create": 0}

    async def create(db_arg, attempt_id, **kwargs):
        calls["create"] += 1
        return _fake_board_create_success(db_arg, attempt_id, **kwargs)

    async def sync(db_arg, connection, client=None):
        await _fake_sync_created_board(db_arg, connection, client=client)

    monkeypatch.setattr(destination, "execute_board_provisioning_attempt", create)
    monkeypatch.setattr(destination, "sync_boards", sync)
    monkeypatch.setattr(destination, "execute_autonomous_item", _successful_task57)

    first = asyncio.run(destination.ensure_autonomous_destination(
        db, seeded["item1"].id, settings=_destination_settings(), now=NOW
    ))
    second = asyncio.run(destination.ensure_autonomous_destination(
        db, seeded["item1"].id, settings=_destination_settings(), now=NOW
    ))
    assert first.id == second.id
    assert calls["create"] == 1
    assert db.query(PinterestAutonomousDestinationRun).count() == 1
    assert db.query(PinterestBoardProvisioningAttempt).count() == 1
    db.close(); engine.dispose()


def test_destination_rejects_rerouting_after_identity_is_persisted(monkeypatch):
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    settings = _destination_settings()
    ready = destination.destination_readiness(
        db, seeded["item1"].id, settings=settings, now=NOW
    )
    run = PinterestAutonomousDestinationRun(
        id="destination-reroute",
        portfolio_item_id=seeded["item1"].id,
        plan_id=seeded["plan"].id,
        input_fingerprint=ready["input_fingerprint"],
        status="STARTED",
        stage="BOARD_READY",
        pinterest_board_record_id="old-board-row",
        safe_metadata={"board_record_id": "old-board-row"},
        started_at=NOW,
    )
    db.add(run); db.commit()

    with pytest.raises(
        destination.AutonomousDestinationError,
        match="AUTONOMOUS_DESTINATION_BOARD_ROUTING_DRIFT",
    ):
        asyncio.run(destination.ensure_autonomous_destination(
            db, seeded["item1"].id, settings=settings, now=NOW
        ))
    db.close(); engine.dispose()


def test_destination_persists_exact_readiness_identity_before_reroute(monkeypatch):
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)

    def inspect_then_succeed(db_arg, item_id, **kwargs):
        run = db_arg.scalar(
            select(PinterestAutonomousDestinationRun).where(
                PinterestAutonomousDestinationRun.portfolio_item_id == item_id
            )
        )
        assert run is not None
        assert run.status == "STARTED"
        assert run.stage == "BOARD_READY"
        assert run.safe_metadata["policy_version"] == "PINTEREST_AUTONOMOUS_DESTINATION_V1"
        assert run.safe_metadata["board_canonical_key"] == "arabian-fragrance"
        assert run.safe_metadata["board_record_id"] == seeded["provider_board"].id
        return _successful_task57()

    monkeypatch.setattr(destination, "execute_autonomous_item", inspect_then_succeed)
    result = asyncio.run(destination.ensure_autonomous_destination(
        db, seeded["item1"].id, settings=_destination_settings(), now=NOW
    ))
    assert result.status == "SUCCEEDED"
    db.close(); engine.dispose()


def test_destination_compare_and_set_cannot_overwrite_terminal_unknown(monkeypatch):
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    settings = _destination_settings()
    ready = destination.destination_readiness(
        db, seeded["item1"].id, settings=settings, now=NOW
    )
    run = PinterestAutonomousDestinationRun(
        id="destination-cas",
        portfolio_item_id=seeded["item1"].id,
        plan_id=seeded["plan"].id,
        input_fingerprint=ready["input_fingerprint"],
        status="UNKNOWN",
        stage="BOARD_PROVISIONING",
        safe_metadata={"terminal_code": "FIRST_UNKNOWN"},
        started_at=NOW,
        completed_at=NOW,
    )
    db.add(run); db.commit()
    result = destination._terminalize(
        db,
        run.id,
        status="FAILED",
        code="LATE_FAILURE",
        now=NOW + timedelta(minutes=1),
    )
    assert result.status == "UNKNOWN"
    assert result.safe_metadata["terminal_code"] == "FIRST_UNKNOWN"
    db.close(); engine.dispose()


@pytest.mark.parametrize("attempt_status", ["UNKNOWN", "FAILED"])
def test_destination_terminal_provisioning_outcome_never_retries(monkeypatch, attempt_status):
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    _remove_routed_provider_board(db, seeded)
    settings = _destination_settings()
    plan = destination.destination_readiness(
        db, seeded["item1"].id, settings=settings, now=NOW
    )["board_strategy"]
    attempt = PinterestBoardProvisioningAttempt(
        id=f"attempt-{attempt_status.lower()}",
        connection_id=seeded["connection"].id,
        canonical_key=plan["canonical_key"],
        desired_name=plan["desired_name"],
        desired_description=plan["desired_description"],
        privacy=plan["privacy"],
        request_fingerprint=plan["request_fingerprint"],
        status=attempt_status,
        provider_mutation_started_at=NOW,
        error_code=f"SYNTHETIC_{attempt_status}",
        safe_metadata={},
        started_at=NOW,
        completed_at=NOW,
    )
    db.add(attempt); db.commit()

    async def must_not_create(*args, **kwargs):
        raise AssertionError("terminal attempt must never be retried")
    monkeypatch.setattr(destination, "execute_board_provisioning_attempt", must_not_create)

    run = asyncio.run(destination.ensure_autonomous_destination(
        db, seeded["item1"].id, settings=settings, now=NOW
    ))
    assert run.status == attempt_status
    db.close(); engine.dispose()


def test_destination_marks_failed_when_task57_fails(monkeypatch):
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)

    def fail(*args, **kwargs):
        raise execution.AutonomousExecutionError("TASK57_SYNTHETIC_FAILURE")

    monkeypatch.setattr(destination, "execute_autonomous_item", fail)
    with pytest.raises(
        destination.AutonomousDestinationError,
        match="TASK57_SYNTHETIC_FAILURE",
    ):
        asyncio.run(destination.ensure_autonomous_destination(
            db, seeded["item1"].id, settings=_destination_settings(), now=NOW
        ))
    run = db.scalar(select(PinterestAutonomousDestinationRun))
    assert run.status == "FAILED"
    assert run.stage == "BOARD_READY"
    db.close(); engine.dispose()


def test_destination_detects_input_drift_before_sync_resume(monkeypatch):
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    settings = _destination_settings()
    ready = destination.destination_readiness(
        db, seeded["item1"].id, settings=settings, now=NOW
    )
    db.add(PinterestAutonomousDestinationRun(
        id="destination-drift",
        portfolio_item_id=seeded["item1"].id,
        plan_id=seeded["plan"].id,
        input_fingerprint=ready["input_fingerprint"],
        status="STARTED",
        stage="BOARD_SYNC_PENDING",
        safe_metadata={},
        started_at=NOW,
    ))
    db.commit()
    seeded["plan"].plan_fingerprint = "z" * 64
    db.commit()

    with pytest.raises(
        destination.AutonomousDestinationError,
        match="AUTONOMOUS_DESTINATION_INPUT_DRIFT",
    ):
        asyncio.run(destination.ensure_autonomous_destination(
            db, seeded["item1"].id, settings=settings, now=NOW
        ))
    db.close(); engine.dispose()


def test_destination_adopts_preexisting_terminal_or_claimed_attempt_fail_closed(monkeypatch):
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    _remove_routed_provider_board(db, seeded)
    settings = _destination_settings()
    plan = destination.destination_readiness(
        db, seeded["item1"].id, settings=settings, now=NOW
    )["board_strategy"]
    attempt = PinterestBoardProvisioningAttempt(
        id="attempt-claimed",
        connection_id=seeded["connection"].id,
        canonical_key=plan["canonical_key"],
        desired_name=plan["desired_name"],
        desired_description=plan["desired_description"],
        privacy=plan["privacy"],
        request_fingerprint=plan["request_fingerprint"],
        status="STARTED",
        provider_mutation_started_at=NOW,
        safe_metadata={},
        started_at=NOW,
    )
    db.add(attempt); db.commit()

    async def no_replay(*args, **kwargs):
        raise AssertionError("claimed provider mutation cannot be replayed")
    monkeypatch.setattr(destination, "execute_board_provisioning_attempt", no_replay)

    run = asyncio.run(destination.ensure_autonomous_destination(
        db, seeded["item1"].id, settings=settings, now=NOW
    ))
    assert run.status == "UNKNOWN"
    assert run.board_provisioning_attempt_id == attempt.id
    db.close(); engine.dispose()


def test_destination_resumes_preexisting_succeeded_attempt_without_create(monkeypatch):
    engine, db = _db()
    seeded = _seed(db, two_same_day=False)
    _remove_routed_provider_board(db, seeded)
    settings = _destination_settings()
    plan = destination.destination_readiness(
        db, seeded["item1"].id, settings=settings, now=NOW
    )["board_strategy"]
    attempt = PinterestBoardProvisioningAttempt(
        id="attempt-succeeded",
        connection_id=seeded["connection"].id,
        canonical_key=plan["canonical_key"],
        desired_name=plan["desired_name"],
        desired_description=plan["desired_description"],
        privacy=plan["privacy"],
        request_fingerprint=plan["request_fingerprint"],
        status="SUCCEEDED",
        provider_mutation_started_at=NOW,
        provider_board_id="created-board-1",
        safe_metadata={},
        started_at=NOW,
        completed_at=NOW,
    )
    db.add(attempt); db.commit()

    async def no_create(*args, **kwargs):
        raise AssertionError("succeeded provisioning attempt cannot be replayed")
    async def sync(db_arg, connection, client=None):
        await _fake_sync_created_board(db_arg, connection, client=client)

    monkeypatch.setattr(destination, "execute_board_provisioning_attempt", no_create)
    monkeypatch.setattr(destination, "sync_boards", sync)
    monkeypatch.setattr(destination, "execute_autonomous_item", _successful_task57)

    run = asyncio.run(destination.ensure_autonomous_destination(
        db, seeded["item1"].id, settings=settings, now=NOW
    ))
    assert run.status == "SUCCEEDED"
    assert run.board_provisioning_attempt_id == attempt.id
    db.close(); engine.dispose()
