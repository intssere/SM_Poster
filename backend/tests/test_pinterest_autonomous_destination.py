from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import (
    PinterestAutonomousDestinationRun,
    PinterestAutonomousExecutionRun,
    PinterestOptimizerApplication,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
)
from app.services import pinterest_autonomous_destination_readiness as readiness


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
SCHEDULED = datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc)


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session()


def _settings(**overrides):
    values = {
        "database_url": "sqlite:///:memory:",
        "pinterest_autonomous_board_ensure_enabled": False,
        "pinterest_seo_brief_persistence_enabled": True,
        "pinterest_autonomous_generation_enabled": True,
        "routine_autonomous_authorization_enabled": True,
        "pinterest_autonomous_execution_enabled": True,
        "pinterest_board_write_scope_enabled": False,
        "pinterest_board_provisioning_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def _seed(db):
    plan = PinterestPortfolioPlan(
        id="plan-1",
        store_id="store-1",
        month_start=date(2026, 9, 1),
        month_end=date(2026, 9, 30),
        target_pins=150,
        existing_commitments=0,
        planned_active_slots=1,
        reserve_slots=0,
        policy_version="PINTEREST_PORTFOLIO_V2",
        input_fingerprint="a" * 64,
        plan_fingerprint="b" * 64,
        status="ACTIVE",
        metadata_json={},
    )
    item = PinterestPortfolioPlanItem(
        id="item-1",
        plan_id=plan.id,
        slot_index=0,
        is_reserve=False,
        planned_date=date(2026, 9, 21),
        product_id="product-1",
        local_board_id="local-board-1",
        board_key_snapshot="arabian-fragrance",
        content_angle_id="angle-1",
        angle_key_snapshot="arabian-fragrance-discovery",
        seed_keywords=["arabian fragrance"],
        selection_score=Decimal("10.000000"),
        selection_metadata={},
        item_fingerprint="c" * 64,
        status="PLANNED",
    )
    optimizer = PinterestOptimizerApplication(
        id="optimizer-1",
        plan_id=plan.id,
        plan_fingerprint_snapshot=plan.plan_fingerprint,
        optimizer_policy_version="PINTEREST_OPTIMIZER_V1",
        optimizer_fingerprint="optimizer-fp",
        learning_fingerprint="learning-fp",
        input_state_fingerprint="state-fp",
        frozen_item_count=0,
        optimizable_item_count=1,
        exploit_count=1,
        explore_count=0,
        recommendation_snapshot={},
        status="APPLIED",
        applied_by="adaptive-optimizer-v1",
        applied_at=NOW,
    )
    db.add_all([plan, item, optimizer])
    db.commit()
    return plan, item, optimizer


def _execution(**overrides):
    value = {
        "ready": True,
        "blockers": [],
        "scheduled_for": SCHEDULED,
        "input_fingerprint": "e" * 64,
        "state_mutated": False,
        "provider_called": False,
        "ai_called": False,
    }
    value.update(overrides)
    return value


def _route(**overrides):
    value = {
        "status": "ROUTE_EXISTING",
        "blockers": [],
        "selected_board_id": "provider-board-row-1",
        "selected_external_board_id": "external-board-1",
        "canonical_key": "arabian-fragrance",
        "desired_name": "Arabian Fragrance",
        "desired_description": "Arabian fragrance discoveries.",
        "privacy": "PUBLIC",
        "request_fingerprint": None,
        "existing_attempt": None,
        "provisioning_ready": False,
        "state_mutated": False,
        "provider_called": False,
    }
    value.update(overrides)
    return value


def test_default_flag_blocks_existing_board_without_mutation(monkeypatch):
    engine, db = _db()
    _, item, _ = _seed(db)
    monkeypatch.setattr(readiness, "execution_readiness", lambda *a, **k: _execution())
    monkeypatch.setattr(readiness, "board_strategy", lambda *a, **k: _route())
    before = (db.query(PinterestAutonomousDestinationRun).count(), len(db.new), len(db.dirty))

    result = readiness.destination_readiness(
        db, item.id, settings=_settings(), now=NOW
    )
    after = (db.query(PinterestAutonomousDestinationRun).count(), len(db.new), len(db.dirty))

    assert result["ready"] is False
    assert result["blockers"] == ["AUTONOMOUS_BOARD_ENSURE_DISABLED"]
    assert result["board_state"] == "BOARD_READY"
    assert len(result["input_fingerprint"]) == 64
    assert result["state_mutated"] is False
    assert result["provider_called"] is False
    assert result["pin_write_called"] is False
    assert result["ai_called"] is False
    assert before == after
    db.close(); engine.dispose()


def test_existing_board_is_ready_when_coordinator_enabled(monkeypatch):
    engine, db = _db()
    _, item, _ = _seed(db)
    monkeypatch.setattr(readiness, "execution_readiness", lambda *a, **k: _execution())
    monkeypatch.setattr(readiness, "board_strategy", lambda *a, **k: _route())

    result = readiness.destination_readiness(
        db,
        item.id,
        settings=_settings(pinterest_autonomous_board_ensure_enabled=True),
        now=NOW,
    )

    assert result["ready"] is True
    assert result["blockers"] == []
    assert result["board_state"] == "BOARD_READY"
    assert result["execution_readiness"]["ready"] is True
    db.close(); engine.dispose()


def test_missing_board_surfaces_exact_provisioning_flags(monkeypatch):
    engine, db = _db()
    _, item, _ = _seed(db)
    monkeypatch.setattr(
        readiness,
        "execution_readiness",
        lambda *a, **k: _execution(
            ready=False,
            blockers=["ROUTABLE_PINTEREST_BOARD_REQUIRED"],
            input_fingerprint=None,
        ),
    )
    monkeypatch.setattr(
        readiness,
        "board_strategy",
        lambda *a, **k: _route(
            status="PROVISION_REQUIRED",
            selected_board_id=None,
            selected_external_board_id=None,
            request_fingerprint="f" * 64,
            provisioning_ready=False,
            blockers=[
                "BOARD_WRITE_SCOPE_DISABLED",
                "BOARD_PROVISIONING_DISABLED",
                "BOARDS_WRITE_SCOPE_REQUIRED",
            ],
        ),
    )

    result = readiness.destination_readiness(
        db,
        item.id,
        settings=_settings(pinterest_autonomous_board_ensure_enabled=True),
        now=NOW,
    )

    assert result["ready"] is False
    assert result["board_state"] == "PROVISION_REQUIRED"
    assert result["blockers"] == [
        "BOARD_WRITE_SCOPE_DISABLED",
        "BOARD_PROVISIONING_DISABLED",
        "BOARDS_WRITE_SCOPE_REQUIRED",
    ]
    assert result["execution_prerequisite_blockers"] == []
    db.close(); engine.dispose()


def test_confirmed_provisioning_success_is_get_only_resumable(monkeypatch):
    engine, db = _db()
    _, item, _ = _seed(db)
    monkeypatch.setattr(
        readiness,
        "execution_readiness",
        lambda *a, **k: _execution(
            ready=False,
            blockers=["ROUTABLE_PINTEREST_BOARD_REQUIRED"],
            input_fingerprint=None,
        ),
    )
    monkeypatch.setattr(
        readiness,
        "board_strategy",
        lambda *a, **k: _route(
            status="BLOCKED",
            selected_board_id=None,
            selected_external_board_id=None,
            request_fingerprint="f" * 64,
            existing_attempt={
                "id": "attempt-1",
                "status": "SUCCEEDED",
                "provider_board_id": "external-board-1",
                "provider_mutation_started_at": NOW,
                "error_code": None,
            },
            blockers=["PROVISIONING_SUCCEEDED_SYNC_REQUIRED"],
        ),
    )

    result = readiness.destination_readiness(
        db,
        item.id,
        settings=_settings(pinterest_autonomous_board_ensure_enabled=True),
        now=NOW,
    )

    assert result["ready"] is True
    assert result["blockers"] == []
    assert result["board_state"] == "BOARD_SYNC_PENDING"
    db.close(); engine.dispose()


def test_terminal_provisioning_unknown_blocks(monkeypatch):
    engine, db = _db()
    _, item, _ = _seed(db)
    monkeypatch.setattr(
        readiness,
        "execution_readiness",
        lambda *a, **k: _execution(
            ready=False,
            blockers=["ROUTABLE_PINTEREST_BOARD_REQUIRED"],
            input_fingerprint=None,
        ),
    )
    monkeypatch.setattr(
        readiness,
        "board_strategy",
        lambda *a, **k: _route(
            status="BLOCKED",
            existing_attempt={
                "id": "attempt-1",
                "status": "UNKNOWN",
                "provider_board_id": None,
                "provider_mutation_started_at": NOW,
                "error_code": "UNKNOWN",
            },
            blockers=["PROVISIONING_UNKNOWN_RECONCILIATION_REQUIRED"],
        ),
    )

    result = readiness.destination_readiness(
        db,
        item.id,
        settings=_settings(pinterest_autonomous_board_ensure_enabled=True),
        now=NOW,
    )
    assert result["ready"] is False
    assert result["board_state"] == "PROVISIONING_UNKNOWN"
    assert result["blockers"] == [
        "PROVISIONING_UNKNOWN_RECONCILIATION_REQUIRED"
    ]
    db.close(); engine.dispose()


def test_existing_destination_input_drift_fails_closed(monkeypatch):
    engine, db = _db()
    plan, item, _ = _seed(db)
    monkeypatch.setattr(readiness, "execution_readiness", lambda *a, **k: _execution())
    monkeypatch.setattr(readiness, "board_strategy", lambda *a, **k: _route())

    first = readiness.destination_readiness(
        db,
        item.id,
        settings=_settings(pinterest_autonomous_board_ensure_enabled=True),
        now=NOW,
    )
    db.add(PinterestAutonomousDestinationRun(
        id="destination-1",
        portfolio_item_id=item.id,
        plan_id=plan.id,
        input_fingerprint=first["input_fingerprint"],
        status="STARTED",
        stage="STARTED",
        safe_metadata={},
        started_at=NOW,
    ))
    db.commit()

    plan.plan_fingerprint = "z" * 64
    db.commit()
    second = readiness.destination_readiness(
        db,
        item.id,
        settings=_settings(pinterest_autonomous_board_ensure_enabled=True),
        now=NOW,
    )
    assert "AUTONOMOUS_DESTINATION_INPUT_DRIFT" in second["blockers"]
    assert second["ready"] is False
    db.close(); engine.dispose()


def test_succeeded_destination_requires_permitted_execution(monkeypatch):
    engine, db = _db()
    plan, item, optimizer = _seed(db)
    monkeypatch.setattr(readiness, "execution_readiness", lambda *a, **k: _execution())
    monkeypatch.setattr(readiness, "board_strategy", lambda *a, **k: _route())
    first = readiness.destination_readiness(
        db,
        item.id,
        settings=_settings(pinterest_autonomous_board_ensure_enabled=True),
        now=NOW,
    )
    execution = PinterestAutonomousExecutionRun(
        id="execution-1",
        portfolio_item_id=item.id,
        plan_id=plan.id,
        optimizer_application_id=optimizer.id,
        input_fingerprint="x" * 64,
        status="SUCCEEDED",
        stage="PERMITTED",
        scheduled_for=SCHEDULED,
        safe_metadata={},
        started_at=NOW,
        completed_at=NOW,
    )
    run = PinterestAutonomousDestinationRun(
        id="destination-1",
        portfolio_item_id=item.id,
        plan_id=plan.id,
        input_fingerprint=first["input_fingerprint"],
        status="SUCCEEDED",
        stage="EXECUTION_READY",
        autonomous_execution_run_id=execution.id,
        safe_metadata={},
        started_at=NOW,
        completed_at=NOW,
    )
    db.add_all([execution, run])
    db.commit()

    result = readiness.destination_readiness(
        db,
        item.id,
        settings=_settings(),
        now=NOW,
    )
    assert result["already_succeeded"] is True
    assert result["ready"] is True
    db.close(); engine.dispose()
