from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.models.domain import PublicationStatus
from app.services import pinterest_phase_b2_operator as svc


ITEM_ID = "b09d0c93-8da0-4561-86fa-bcf5bb36e9be"
PLAN_ID = "fbd103b5-841a-4ced-84a2-2f5a35deef4c"
OPTIMIZER_ID = "affced85-21f7-47d5-bab6-9eea694d8519"
ITEM_FP = "b" * 64
DEST_FP = "d" * 64
EXEC_FP = "e" * 64
SEO_INPUT_FP = "1" * 64
SEO_FP = "2" * 64
BOARD_ID = "dab1fe65-621d-4ace-9a4c-bbbf7f26272b"
EXTERNAL_BOARD_ID = "1093811896939213067"
SCHEDULED_FOR = datetime(2026, 9, 23, 14, 15, tzinfo=timezone.utc)


def _settings(**updates):
    base = Settings(database_url="sqlite://")
    safe = {
        "publishing_enabled": False,
        "buffer_publishing_enabled": False,
        "buffer_single_pin_pilot_enabled": False,
        "routine_pinterest_scheduler_enabled": False,
        "routine_pinterest_worker_enabled": False,
        "routine_buffer_dispatch_enabled": False,
        "pinterest_write_scope_enabled": False,
        "pinterest_board_write_scope_enabled": False,
        "pinterest_board_provisioning_enabled": False,
        "pinterest_single_pin_pilot_enabled": False,
        "pinterest_seo_brief_persistence_enabled": False,
        "pinterest_autonomous_generation_enabled": False,
        "routine_autonomous_authorization_enabled": False,
        "pinterest_autonomous_execution_enabled": False,
        "pinterest_autonomous_board_ensure_enabled": False,
        "pinterest_analytics_ingestion_enabled": False,
        "pinterest_learning_snapshot_persistence_enabled": False,
        "routine_pinterest_dry_run": True,
    }
    safe.update(updates)
    return base.model_copy(update=safe)


def _item(status="PLANNED", publication_id=None):
    return SimpleNamespace(
        id=ITEM_ID,
        plan_id=PLAN_ID,
        product_id="product-1",
        item_fingerprint=ITEM_FP,
        board_key_snapshot="new-arrivals",
        is_reserve=False,
        status=status,
        publication_id=publication_id,
    )


def _plan():
    return SimpleNamespace(id=PLAN_ID, status="ACTIVE")


def _optimizer():
    return SimpleNamespace(
        id=OPTIMIZER_ID,
        status="APPLIED",
        optimizer_fingerprint="f" * 64,
    )


class FakeDB:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.scalar_value = 0

    def get(self, model, row_id):
        return self.rows.get(row_id)

    def scalar(self, statement):
        return self.scalar_value

    def execute(self, statement):
        raise AssertionError("execute should be patched or unused in this unit test")


def _destination(blockers=None):
    return {
        "ready": False,
        "blockers": blockers
        if blockers is not None
        else [
            "SEO_BRIEF_PERSISTENCE_DISABLED",
            "AUTONOMOUS_GENERATION_DISABLED",
            "AUTONOMOUS_AUTHORIZATION_DISABLED",
            "AUTONOMOUS_EXECUTION_DISABLED",
        ],
        "scheduled_for": SCHEDULED_FOR,
        "input_fingerprint": DEST_FP,
        "board_strategy": {
            "status": "ROUTE_EXISTING",
            "selected_board_id": BOARD_ID,
            "selected_external_board_id": EXTERNAL_BOARD_ID,
        },
    }


def _execution(blockers=None):
    return {
        "ready": False,
        "blockers": blockers
        if blockers is not None
        else [
            "SEO_BRIEF_PERSISTENCE_DISABLED",
            "AUTONOMOUS_GENERATION_DISABLED",
            "AUTONOMOUS_AUTHORIZATION_DISABLED",
            "AUTONOMOUS_EXECUTION_DISABLED",
        ],
        "scheduled_for": SCHEDULED_FOR,
        "input_fingerprint": EXEC_FP,
        "optimizer_binding": {
            "application_id": OPTIMIZER_ID,
            "optimizer_fingerprint": "f" * 64,
            "item_optimizer_fingerprint": "f" * 64,
            "recommended_position": 17,
        },
        "board_routing": {
            "status": "ROUTE_EXISTING",
            "selected_board_id": BOARD_ID,
            "selected_external_board_id": EXTERNAL_BOARD_ID,
        },
    }


def _patch_basic_readiness(monkeypatch, *, destination=None, execution=None):
    item = _item()
    plan = _plan()
    optimizer = _optimizer()
    db = FakeDB({ITEM_ID: item, PLAN_ID: plan})
    monkeypatch.setattr(svc, "_optimizer_for_plan", lambda db, plan_id: optimizer)
    monkeypatch.setattr(svc, "destination_readiness", lambda *a, **k: destination or _destination())
    monkeypatch.setattr(svc, "execution_readiness", lambda *a, **k: execution or _execution())
    monkeypatch.setattr(svc, "_destination_run", lambda db, item_id: None)
    monkeypatch.setattr(svc, "_execution_run", lambda db, item_id: None)
    monkeypatch.setattr(svc, "_publish_unknown_count", lambda db: 0)
    monkeypatch.setattr(
        svc,
        "seo_brief_preview",
        lambda *a, **k: {
            "ready": True,
            "blockers": [],
            "input_fingerprint": SEO_INPUT_FP,
            "seo_fingerprint": SEO_FP,
            "primary_keyword": "new brand perfume",
            "state_mutated": False,
            "provider_called": False,
            "ai_called": False,
        },
    )
    monkeypatch.setattr(
        svc,
        "_cache_status",
        lambda *a, **k: {
            "ready": True,
            "required": True,
            "source_image_id": "image-1",
            "source_sha256": "a" * 64,
            "source_bytes": b"cached",
            "blocker": None,
        },
    )
    return db


def test_phase_b2_readiness_ignores_only_four_expected_feature_blockers(monkeypatch):
    db = _patch_basic_readiness(monkeypatch)
    result = svc.phase_b2_readiness(
        db,
        portfolio_item_id=ITEM_ID,
        settings=_settings(),
        now=datetime(2026, 9, 22, 15, 49, tzinfo=timezone.utc),
    )
    assert result["structurally_ready"] is True
    assert result["ready"] is True
    assert result["destination_input_fingerprint"] == DEST_FP
    assert result["execution_input_fingerprint"] == EXEC_FP
    assert result["pinterest_board_record_id"] == BOARD_ID
    assert result["seo_preview_ready"] is True
    assert result["seo_input_fingerprint"] == SEO_INPUT_FP
    assert result["seo_fingerprint"] == SEO_FP
    assert result["primary_keyword"] == "new brand perfume"
    assert result["source_cache"]["ready"] is True


def test_phase_b2_readiness_fails_before_run_on_seo_preview_blocker(monkeypatch):
    db = _patch_basic_readiness(monkeypatch)
    monkeypatch.setattr(
        svc,
        "seo_brief_preview",
        lambda *a, **k: {
            "ready": False,
            "blockers": ["NO_EVIDENCE_BOUND_PRIMARY_KEYWORD"],
            "state_mutated": False,
            "provider_called": False,
            "ai_called": False,
        },
    )

    result = svc.phase_b2_readiness(
        db,
        portfolio_item_id=ITEM_ID,
        settings=_settings(),
        now=datetime(2026, 9, 22, 15, 49, tzinfo=timezone.utc),
    )

    assert result["ready"] is False
    assert result["structurally_ready"] is False
    assert result["seo_preview_ready"] is False
    assert result["seo_preview_blockers"] == ["NO_EVIDENCE_BOUND_PRIMARY_KEYWORD"]
    assert "NO_EVIDENCE_BOUND_PRIMARY_KEYWORD" in result["blockers"]


def test_phase_b2_readiness_fails_on_any_fifth_blocker(monkeypatch):
    destination = _destination(
        blockers=[
            "SEO_BRIEF_PERSISTENCE_DISABLED",
            "AUTONOMOUS_GENERATION_DISABLED",
            "AUTONOMOUS_AUTHORIZATION_DISABLED",
            "AUTONOMOUS_EXECUTION_DISABLED",
            "ROUTABLE_PINTEREST_BOARD_REQUIRED",
        ]
    )
    db = _patch_basic_readiness(monkeypatch, destination=destination)
    result = svc.phase_b2_readiness(
        db,
        portfolio_item_id=ITEM_ID,
        settings=_settings(),
        now=datetime(2026, 9, 22, 15, 49, tzinfo=timezone.utc),
    )
    assert result["structurally_ready"] is False
    assert "ROUTABLE_PINTEREST_BOARD_REQUIRED" in result["blockers"]


def test_phase_b2_readiness_requires_future_schedule(monkeypatch):
    execution = _execution()
    execution["scheduled_for"] = datetime(2026, 9, 22, 14, 15, tzinfo=timezone.utc)
    destination = _destination()
    destination["scheduled_for"] = execution["scheduled_for"]
    db = _patch_basic_readiness(monkeypatch, destination=destination, execution=execution)
    result = svc.phase_b2_readiness(
        db,
        portfolio_item_id=ITEM_ID,
        settings=_settings(),
        now=datetime(2026, 9, 22, 15, 49, tzinfo=timezone.utc),
    )
    assert result["ready"] is False
    assert "SCHEDULE_TIME_NOT_FUTURE" in result["blockers"]


@pytest.mark.parametrize(
    "field",
    [
        "publishing_enabled",
        "buffer_publishing_enabled",
        "buffer_single_pin_pilot_enabled",
        "routine_pinterest_scheduler_enabled",
        "routine_pinterest_worker_enabled",
        "routine_buffer_dispatch_enabled",
        "pinterest_write_scope_enabled",
        "pinterest_board_write_scope_enabled",
        "pinterest_board_provisioning_enabled",
        "pinterest_single_pin_pilot_enabled",
    ],
)
def test_phase_b2_readiness_blocks_provider_and_routine_gates(monkeypatch, field):
    db = _patch_basic_readiness(monkeypatch)
    result = svc.phase_b2_readiness(
        db,
        portfolio_item_id=ITEM_ID,
        settings=_settings(**{field: True}),
        now=datetime(2026, 9, 22, 15, 49, tzinfo=timezone.utc),
    )
    assert result["ready"] is False
    assert f"{field.upper()}_MUST_BE_FALSE" in result["blockers"]


def test_operator_settings_enable_exactly_four_internal_gates():
    original = _settings()
    result = svc._operator_settings(original)
    for name in svc._OPERATOR_ENABLED_FIELDS:
        assert getattr(result, name) is True
    for name in svc._PROVIDER_GATE_FIELDS:
        assert getattr(result, name) is False
    assert result.pinterest_autonomous_board_ensure_enabled is False
    assert result.pinterest_analytics_ingestion_enabled is False
    assert result.pinterest_learning_snapshot_persistence_enabled is False
    assert result.routine_pinterest_dry_run is True
    for name in svc._OPERATOR_ENABLED_FIELDS:
        assert getattr(original, name) is False


@pytest.mark.asyncio
async def test_execute_phase_b2_exact_binding_mismatch_fails_before_mutation(monkeypatch):
    monkeypatch.setattr(
        svc,
        "phase_b2_readiness",
        lambda *a, **k: {
            "structurally_ready": True,
            "plan_id": PLAN_ID,
            "optimizer_application_id": OPTIMIZER_ID,
            "item_fingerprint": ITEM_FP,
            "destination_input_fingerprint": DEST_FP,
            "execution_input_fingerprint": EXEC_FP,
            "seo_input_fingerprint": SEO_INPUT_FP,
            "seo_fingerprint": SEO_FP,
            "scheduled_for": SCHEDULED_FOR,
            "pinterest_board_record_id": BOARD_ID,
            "external_board_id": EXTERNAL_BOARD_ID,
            "source_cache": {
                "ready": True,
                "source_image_id": "image-1",
                "source_sha256": "a" * 64,
            },
        },
    )

    async def should_not_run(*args, **kwargs):
        raise AssertionError("mutation coordinator must not run")

    monkeypatch.setattr(svc, "ensure_autonomous_destination", should_not_run)

    with pytest.raises(svc.PhaseB2OperatorError, match="ITEM_FINGERPRINT_MISMATCH"):
        await svc.execute_phase_b2(
            object(),
            portfolio_item_id=ITEM_ID,
            expected_plan_id=PLAN_ID,
            expected_optimizer_application_id=OPTIMIZER_ID,
            expected_item_fingerprint="0" * 64,
            expected_destination_input_fingerprint=DEST_FP,
            expected_execution_input_fingerprint=EXEC_FP,
            expected_seo_input_fingerprint=SEO_INPUT_FP,
            expected_seo_fingerprint=SEO_FP,
            expected_scheduled_for=SCHEDULED_FOR,
            expected_pinterest_board_record_id=BOARD_ID,
            expected_external_board_id=EXTERNAL_BOARD_ID,
            settings=_settings(),
        )


@pytest.mark.asyncio
async def test_execute_phase_b2_success_enforces_provider_free_postconditions(monkeypatch):
    item = _item()
    plan = _plan()
    optimizer = _optimizer()
    board = SimpleNamespace(
        id=BOARD_ID,
        external_board_id=EXTERNAL_BOARD_ID,
        connection_id="connection-1",
        is_active=True,
        is_eligible=True,
    )
    connection = SimpleNamespace(id="connection-1", status="CONNECTED")
    destination_run = SimpleNamespace(
        id="dest-1",
        portfolio_item_id=ITEM_ID,
        plan_id=PLAN_ID,
        input_fingerprint=DEST_FP,
        status="SUCCEEDED",
        stage="EXECUTION_READY",
        pinterest_board_record_id=BOARD_ID,
        autonomous_execution_run_id="exec-1",
        safe_metadata={"provider_called": False, "ai_called": False},
    )
    execution_run = SimpleNamespace(
        id="exec-1",
        portfolio_item_id=ITEM_ID,
        plan_id=PLAN_ID,
        optimizer_application_id=OPTIMIZER_ID,
        input_fingerprint=EXEC_FP,
        status="SUCCEEDED",
        stage="PERMITTED",
        scheduled_for=SCHEDULED_FOR,
        seo_brief_id="seo-1",
        generation_run_id="gen-1",
        approval_id="approval-1",
        publication_id="pub-1",
        routine_permit_id="permit-1",
        safe_metadata={"provider_called": False, "ai_called": False},
    )
    seo = SimpleNamespace(id="seo-1", portfolio_item_id=ITEM_ID, status="CURRENT")
    generation = SimpleNamespace(
        id="gen-1",
        portfolio_item_id=ITEM_ID,
        seo_brief_id="seo-1",
        status="SUCCEEDED",
        draft_id="draft-1",
        creative_id="creative-1",
        safe_metadata={"provider_called": False, "ai_called": False},
    )
    creative = SimpleNamespace(id="creative-1", render_status="RENDERED")
    approval = SimpleNamespace(
        id="approval-1",
        decision="APPROVED",
        decided_by=svc.AUTONOMOUS_ACTOR,
        draft_id="draft-1",
        creative_id="creative-1",
    )
    publication = SimpleNamespace(
        id="pub-1",
        status=PublicationStatus.SCHEDULED,
        approval_id="approval-1",
        draft_id="draft-1",
        creative_id="creative-1",
        pinterest_board_record_id=BOARD_ID,
        pinterest_board_id_snapshot=EXTERNAL_BOARD_ID,
        pinterest_connection_id="connection-1",
        scheduled_for=SCHEDULED_FOR,
    )
    permit = SimpleNamespace(
        id="permit-1",
        publication_id="pub-1",
        status="ACTIVE",
        authorized_by=svc.AUTONOMOUS_ACTOR,
    )

    rows = {
        ITEM_ID: item,
        PLAN_ID: plan,
        OPTIMIZER_ID: optimizer,
        BOARD_ID: board,
        "connection-1": connection,
        "dest-1": destination_run,
        "exec-1": execution_run,
        "seo-1": seo,
        "gen-1": generation,
        "creative-1": creative,
        "approval-1": approval,
        "pub-1": publication,
        "permit-1": permit,
    }
    db = FakeDB(rows)
    monkeypatch.setattr(
        svc,
        "phase_b2_readiness",
        lambda *a, **k: {
            "structurally_ready": True,
            "plan_id": PLAN_ID,
            "optimizer_application_id": OPTIMIZER_ID,
            "item_fingerprint": ITEM_FP,
            "destination_input_fingerprint": DEST_FP,
            "execution_input_fingerprint": EXEC_FP,
            "scheduled_for": SCHEDULED_FOR,
            "pinterest_board_record_id": BOARD_ID,
            "external_board_id": EXTERNAL_BOARD_ID,
            "source_cache": {
                "ready": True,
                "source_image_id": "image-1",
                "source_sha256": "a" * 64,
            },
        },
    )
    monkeypatch.setattr(
        svc,
        "seo_brief_preview",
        lambda *a, **k: {
            "ready": True,
            "blockers": [],
            "input_fingerprint": SEO_INPUT_FP,
            "seo_fingerprint": SEO_FP,
            "primary_keyword": "new brand perfume",
        },
    )
    monkeypatch.setattr(
        svc,
        "destination_readiness",
        lambda *a, **k: {
            "ready": True,
            "blockers": [],
            "input_fingerprint": DEST_FP,
            "board_strategy": {
                "status": "ROUTE_EXISTING",
                "selected_board_id": BOARD_ID,
                "selected_external_board_id": EXTERNAL_BOARD_ID,
            },
        },
    )
    monkeypatch.setattr(
        svc,
        "execution_readiness",
        lambda *a, **k: {
            "ready": True,
            "blockers": [],
            "input_fingerprint": EXEC_FP,
        },
    )
    monkeypatch.setattr(
        svc,
        "_cache_status",
        lambda *a, **k: {
            "ready": False,
            "required": False,
            "source_image_id": "image-1",
            "source_sha256": "a" * 64,
            "source_bytes": None,
            "blocker": None,
        },
    )
    snapshots = [
        {ITEM_ID: ("PLANNED", None), "other": ("PLANNED", None)},
        {ITEM_ID: ("SCHEDULED", "pub-1"), "other": ("PLANNED", None)},
    ]
    monkeypatch.setattr(svc, "_item_snapshot", lambda *a, **k: snapshots.pop(0))

    async def ensure(*args, **kwargs):
        item.status = "SCHEDULED"
        item.publication_id = "pub-1"
        return destination_run

    monkeypatch.setattr(svc, "ensure_autonomous_destination", ensure)

    result = await svc.execute_phase_b2(
        db,
        portfolio_item_id=ITEM_ID,
        expected_plan_id=PLAN_ID,
        expected_optimizer_application_id=OPTIMIZER_ID,
        expected_item_fingerprint=ITEM_FP,
        expected_destination_input_fingerprint=DEST_FP,
        expected_execution_input_fingerprint=EXEC_FP,
        expected_scheduled_for=SCHEDULED_FOR,
        expected_pinterest_board_record_id=BOARD_ID,
        expected_external_board_id=EXTERNAL_BOARD_ID,
        settings=_settings(),
    )

    assert result["status"] == "SUCCEEDED"
    assert result["destination_run_id"] == "dest-1"
    assert result["execution_run_id"] == "exec-1"
    assert result["publication_id"] == "pub-1"
    assert result["routine_permit_id"] == "permit-1"
    assert result["provider_called"] is False
    assert result["ai_called"] is False
