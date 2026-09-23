from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.domain import (
    PinterestAutonomousDestinationRun,
    PinterestAutonomousExecutionRun,
    PinterestAutonomousGenerationRun,
    PinterestAutonomousRunReconciliation,
    PinterestPortfolioPlanItem,
)
from app.services import pinterest_autonomous_reconciliation as reconciliation


NOW = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
DEST_FP = "a" * 64
EXEC_FP = "b" * 64
FAILED_GEN_FP = "c" * 64
RETRY_GEN_FP = "d" * 64
ITEM_FP = "e" * 64


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _seed_failed_chain(db):
    item = PinterestPortfolioPlanItem(
        id="item-1",
        plan_id="plan-1",
        slot_index=20,
        is_reserve=False,
        planned_date=date(2026, 9, 23),
        product_id="product-1",
        local_board_id="board-1",
        board_key_snapshot="new-arrivals",
        content_angle_id="angle-1",
        angle_key_snapshot="new-arrival",
        seed_keywords=["new brand perfume"],
        selection_score=100,
        selection_metadata={},
        item_fingerprint=ITEM_FP,
        status="PLANNED",
        publication_id=None,
    )
    destination = PinterestAutonomousDestinationRun(
        id="dest-1",
        portfolio_item_id=item.id,
        plan_id=item.plan_id,
        input_fingerprint=DEST_FP,
        attempt_number=1,
        status="FAILED",
        stage="BOARD_READY",
        pinterest_board_record_id="provider-board-1",
        safe_metadata={"provider_called": False, "ai_called": False},
        started_at=NOW,
        completed_at=NOW,
    )
    execution = PinterestAutonomousExecutionRun(
        id="exec-1",
        portfolio_item_id=item.id,
        plan_id=item.plan_id,
        optimizer_application_id="optimizer-1",
        input_fingerprint=EXEC_FP,
        attempt_number=1,
        status="FAILED",
        stage="SEO_READY",
        scheduled_for=datetime(2026, 9, 23, 15, 45, tzinfo=timezone.utc),
        seo_brief_id="seo-1",
        safe_metadata={"provider_called": False, "ai_called": False},
        started_at=NOW,
        completed_at=NOW,
    )
    generation = PinterestAutonomousGenerationRun(
        id="gen-1",
        portfolio_item_id=item.id,
        seo_brief_id="seo-1",
        input_fingerprint=FAILED_GEN_FP,
        attempt_number=1,
        status="FAILED",
        safe_metadata={
            "provider_called": False,
            "ai_called": False,
            "failure_code": "CREATIVE_TEXT_OVERFLOWS_THE_CANVAS",
        },
        started_at=NOW,
        completed_at=NOW,
    )
    db.add_all([item, destination, execution, generation])
    db.commit()
    return item, destination, execution, generation


def _patch_retry_readiness(monkeypatch):
    monkeypatch.setattr(
        reconciliation,
        "destination_readiness",
        lambda *a, **k: {"input_fingerprint": DEST_FP},
    )
    monkeypatch.setattr(
        reconciliation,
        "execution_readiness",
        lambda *a, **k: {"input_fingerprint": EXEC_FP},
    )
    monkeypatch.setattr(
        reconciliation,
        "autonomous_generation_readiness",
        lambda *a, **k: {"input_fingerprint": RETRY_GEN_FP},
    )


def test_reconciliation_readiness_binds_failed_and_retry_fingerprints(monkeypatch):
    db = _db()
    item, destination, execution, generation = _seed_failed_chain(db)
    _patch_retry_readiness(monkeypatch)

    result = reconciliation.reconciliation_readiness(
        db,
        portfolio_item_id=item.id,
        now=NOW,
    )

    assert result["ready"] is True
    assert result["already_reconciled"] is False
    assert result["blockers"] == []
    assert result["failed_attempt_number"] == 1
    assert result["failed_destination_run_id"] == destination.id
    assert result["failed_execution_run_id"] == execution.id
    assert result["failed_generation_run_id"] == generation.id
    assert result["failed_destination_input_fingerprint"] == DEST_FP
    assert result["failed_execution_input_fingerprint"] == EXEC_FP
    assert result["failed_generation_input_fingerprint"] == FAILED_GEN_FP
    assert result["retry_destination_input_fingerprint"] == DEST_FP
    assert result["retry_execution_input_fingerprint"] == EXEC_FP
    assert result["retry_generation_input_fingerprint"] == RETRY_GEN_FP
    assert len(result["reconciliation_fingerprint"]) == 64
    assert result["state_mutated"] is False
    assert result["provider_called"] is False
    db.close()


def test_reconciliation_write_is_immutable_and_idempotent(monkeypatch):
    db = _db()
    item, destination, execution, generation = _seed_failed_chain(db)
    _patch_retry_readiness(monkeypatch)
    readiness = reconciliation.reconciliation_readiness(
        db,
        portfolio_item_id=item.id,
        now=NOW,
    )
    before = {
        "destination": (
            destination.status,
            destination.stage,
            destination.input_fingerprint,
            destination.attempt_number,
            destination.supersedes_run_id,
        ),
        "execution": (
            execution.status,
            execution.stage,
            execution.input_fingerprint,
            execution.attempt_number,
            execution.supersedes_run_id,
        ),
        "generation": (
            generation.status,
            generation.input_fingerprint,
            generation.attempt_number,
            generation.supersedes_run_id,
        ),
    }

    kwargs = dict(
        portfolio_item_id=item.id,
        expected_item_fingerprint=ITEM_FP,
        expected_failed_destination_run_id=destination.id,
        expected_failed_destination_input_fingerprint=DEST_FP,
        expected_failed_execution_run_id=execution.id,
        expected_failed_execution_input_fingerprint=EXEC_FP,
        expected_failed_generation_run_id=generation.id,
        expected_failed_generation_input_fingerprint=FAILED_GEN_FP,
        expected_retry_destination_input_fingerprint=DEST_FP,
        expected_retry_execution_input_fingerprint=EXEC_FP,
        expected_retry_generation_input_fingerprint=RETRY_GEN_FP,
        expected_reconciliation_fingerprint=readiness["reconciliation_fingerprint"],
        now=NOW,
    )
    first = reconciliation.reconcile_failed_run_chain(db, **kwargs)
    second = reconciliation.reconcile_failed_run_chain(db, **kwargs)

    assert first["status"] == "RECONCILED"
    assert first["idempotent"] is False
    assert first["state_mutated"] is True
    assert second["status"] == "RECONCILED"
    assert second["idempotent"] is True
    assert second["reconciliation_id"] == first["reconciliation_id"]
    assert db.query(PinterestAutonomousRunReconciliation).count() == 1

    destination = db.get(PinterestAutonomousDestinationRun, destination.id)
    execution = db.get(PinterestAutonomousExecutionRun, execution.id)
    generation = db.get(PinterestAutonomousGenerationRun, generation.id)
    after = {
        "destination": (
            destination.status,
            destination.stage,
            destination.input_fingerprint,
            destination.attempt_number,
            destination.supersedes_run_id,
        ),
        "execution": (
            execution.status,
            execution.stage,
            execution.input_fingerprint,
            execution.attempt_number,
            execution.supersedes_run_id,
        ),
        "generation": (
            generation.status,
            generation.input_fingerprint,
            generation.attempt_number,
            generation.supersedes_run_id,
        ),
    }
    assert after == before
    db.close()


@pytest.mark.parametrize(
    ("target", "field", "expected_blocker"),
    [
        ("destination", "provider_called", "DESTINATION_PROVIDER_SIDE_EFFECT_NOT_PROVEN_ABSENT"),
        ("execution", "provider_called", "EXECUTION_PROVIDER_SIDE_EFFECT_NOT_PROVEN_ABSENT"),
        ("generation", "provider_called", "GENERATION_PROVIDER_SIDE_EFFECT_NOT_PROVEN_ABSENT"),
    ],
)
def test_reconciliation_fails_closed_on_provider_side_effect(
    monkeypatch,
    target,
    field,
    expected_blocker,
):
    db = _db()
    item, destination, execution, generation = _seed_failed_chain(db)
    _patch_retry_readiness(monkeypatch)
    row = {
        "destination": destination,
        "execution": execution,
        "generation": generation,
    }[target]
    row.safe_metadata = {**row.safe_metadata, field: True}
    db.commit()

    result = reconciliation.reconciliation_readiness(
        db,
        portfolio_item_id=item.id,
        now=NOW,
    )

    assert result["ready"] is False
    assert expected_blocker in result["blockers"]
    assert db.query(PinterestAutonomousRunReconciliation).count() == 0
    db.close()


def test_reconciliation_fails_closed_on_downstream_lineage(monkeypatch):
    db = _db()
    item, _, execution, _ = _seed_failed_chain(db)
    _patch_retry_readiness(monkeypatch)
    execution.publication_id = "publication-side-effect"
    db.commit()

    result = reconciliation.reconciliation_readiness(
        db,
        portfolio_item_id=item.id,
        now=NOW,
    )

    assert result["ready"] is False
    assert "EXECUTION_DOWNSTREAM_SIDE_EFFECT_PRESENT" in result["blockers"]
    assert db.query(PinterestAutonomousRunReconciliation).count() == 0
    db.close()


def test_reconciliation_fails_closed_on_destination_or_execution_input_drift(monkeypatch):
    db = _db()
    item, _, _, _ = _seed_failed_chain(db)
    monkeypatch.setattr(
        reconciliation,
        "destination_readiness",
        lambda *a, **k: {"input_fingerprint": "f" * 64},
    )
    monkeypatch.setattr(
        reconciliation,
        "execution_readiness",
        lambda *a, **k: {"input_fingerprint": EXEC_FP},
    )
    monkeypatch.setattr(
        reconciliation,
        "autonomous_generation_readiness",
        lambda *a, **k: {"input_fingerprint": RETRY_GEN_FP},
    )

    result = reconciliation.reconciliation_readiness(
        db,
        portfolio_item_id=item.id,
        now=NOW,
    )

    assert result["ready"] is False
    assert "DESTINATION_RETRY_INPUT_DRIFT" in result["blockers"]
    db.close()
