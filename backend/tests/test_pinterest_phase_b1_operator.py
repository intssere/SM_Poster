from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services import pinterest_phase_b1_operator as svc


STORE_ID = "87483ecc-9bb7-4ec3-80d0-ac3c0f851d41"
MONTH_KEY = "2026-09"
TARGET = 150
PREVIEW_FP = "a" * 64
INPUT_FP = "b" * 64
OPTIMIZER_FP = "c" * 64
OPTIMIZER_INPUT_FP = "d" * 64


class FakeDB:
    def __init__(self, plan=None):
        self.plan = plan

    def get(self, model, row_id):
        if self.plan is not None and row_id == self.plan.id:
            return self.plan
        return None


def _settings(**updates):
    settings = Settings(database_url="sqlite://")
    return settings.model_copy(update=updates)


def _preview():
    return {
        "ready": True,
        "blockers": [],
        "store_id": STORE_ID,
        "month_key": MONTH_KEY,
        "target_pins": TARGET,
        "existing_commitments": 2,
        "resolved_existing_commitments": 2,
        "planned_active_slots": 148,
        "reserve_slots": 15,
        "candidate_count": 2730,
        "cap_policy": {"max_board_share": 0.40},
        "cap_relaxation": {
            "used": True,
            "vendor_cap_relaxed": False,
            "board_cap_relaxed": True,
            "vendor_limit": 38,
            "board_limit": 60,
        },
        "items": [
            {
                "slot_index": 1,
                "is_reserve": False,
                "board_key_snapshot": "new-arrivals",
            },
            {
                "slot_index": 149,
                "is_reserve": True,
                "board_key_snapshot": "new-arrivals",
            },
        ],
        "input_fingerprint": INPUT_FP,
        "preview_fingerprint": PREVIEW_FP,
        "state_mutated": False,
        "provider_called": False,
        "ai_called": False,
    }


def _route_existing(*args, **kwargs):
    return {
        "status": "ROUTE_EXISTING",
        "blockers": [],
        "selected_board_id": "board-record",
        "selected_external_board_id": "1093811896939213067",
        "match_basis": "routing_label",
    }


def _patch_readiness_dependencies(monkeypatch, *, route=None, plans=None):
    monkeypatch.setattr(svc, "portfolio_preview", lambda *a, **k: _preview())
    monkeypatch.setattr(svc, "_publish_unknown_count", lambda db: 0)
    monkeypatch.setattr(svc, "_month_plans", lambda *a, **k: list(plans or []))
    monkeypatch.setattr(svc, "board_strategy", route or _route_existing)


def test_phase_b1_readiness_accepts_authoritative_2_148_15_relaxed_board(monkeypatch):
    _patch_readiness_dependencies(monkeypatch)

    result = svc.phase_b1_readiness(
        object(),
        store_id=STORE_ID,
        month_key=MONTH_KEY,
        target_pins=TARGET,
        settings=_settings(),
    )

    assert result["ready"] is True
    assert result["blockers"] == []
    assert result["existing_commitments"] == 2
    assert result["resolved_existing_commitments"] == 2
    assert result["planned_active_slots"] == 148
    assert result["reserve_slots"] == 15
    assert result["cap_relaxation"]["board_cap_relaxed"] is True
    assert result["preview_fingerprint"] == PREVIEW_FP
    assert result["input_fingerprint"] == INPUT_FP
    assert result["board_routes"]["new-arrivals"]["status"] == "ROUTE_EXISTING"
    assert result["state_mutated"] is False
    assert result["provider_called"] is False
    assert result["ai_called"] is False


def test_phase_b1_readiness_blocks_non_existing_route(monkeypatch):
    _patch_readiness_dependencies(
        monkeypatch,
        route=lambda *a, **k: {
            "status": "PROVISION_REQUIRED",
            "blockers": ["BOARD_WRITE_SCOPE_DISABLED"],
            "selected_board_id": None,
            "selected_external_board_id": None,
            "match_basis": None,
        },
    )

    result = svc.phase_b1_readiness(
        object(),
        store_id=STORE_ID,
        month_key=MONTH_KEY,
        target_pins=TARGET,
        settings=_settings(),
    )

    assert result["ready"] is False
    assert "BOARD_ROUTE_NOT_EXISTING:new-arrivals:PROVISION_REQUIRED" in result["blockers"]


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
        "routine_autonomous_authorization_enabled",
        "pinterest_autonomous_generation_enabled",
        "pinterest_autonomous_execution_enabled",
        "pinterest_autonomous_board_ensure_enabled",
    ],
)
def test_phase_b1_readiness_blocks_runtime_safety_gates(monkeypatch, field):
    _patch_readiness_dependencies(monkeypatch)
    result = svc.phase_b1_readiness(
        object(),
        store_id=STORE_ID,
        month_key=MONTH_KEY,
        target_pins=TARGET,
        settings=_settings(**{field: True}),
    )
    assert result["ready"] is False
    assert f"{field.upper()}_MUST_BE_FALSE" in result["blockers"]


def test_phase_b1_readiness_requires_dry_run(monkeypatch):
    _patch_readiness_dependencies(monkeypatch)
    result = svc.phase_b1_readiness(
        object(),
        store_id=STORE_ID,
        month_key=MONTH_KEY,
        target_pins=TARGET,
        settings=_settings(routine_pinterest_dry_run=False),
    )
    assert result["ready"] is False
    assert "ROUTINE_PINTEREST_DRY_RUN_REQUIRED" in result["blockers"]


def test_phase_b1_readiness_blocks_publish_unknown(monkeypatch):
    _patch_readiness_dependencies(monkeypatch)
    monkeypatch.setattr(svc, "_publish_unknown_count", lambda db: 1)
    result = svc.phase_b1_readiness(
        object(),
        store_id=STORE_ID,
        month_key=MONTH_KEY,
        target_pins=TARGET,
        settings=_settings(),
    )
    assert result["ready"] is False
    assert "PUBLISH_UNKNOWN_PRESENT" in result["blockers"]


def test_phase_b1_readiness_allows_exact_existing_plan_and_blocks_drift(monkeypatch):
    exact = SimpleNamespace(
        id="plan-1",
        status="DRAFT",
        plan_fingerprint=PREVIEW_FP,
    )
    _patch_readiness_dependencies(monkeypatch, plans=[exact])
    ok = svc.phase_b1_readiness(
        object(),
        store_id=STORE_ID,
        month_key=MONTH_KEY,
        target_pins=TARGET,
        settings=_settings(),
    )
    assert ok["ready"] is True
    assert ok["existing_plan"]["id"] == "plan-1"

    drift = SimpleNamespace(
        id="plan-2",
        status="DRAFT",
        plan_fingerprint="f" * 64,
    )
    _patch_readiness_dependencies(monkeypatch, plans=[drift])
    blocked = svc.phase_b1_readiness(
        object(),
        store_id=STORE_ID,
        month_key=MONTH_KEY,
        target_pins=TARGET,
        settings=_settings(),
    )
    assert blocked["ready"] is False
    assert "PORTFOLIO_PLAN_MONTH_CONFLICT" in blocked["blockers"]


def test_operator_settings_enable_only_planner_optimizer_and_keep_writes_off():
    original = _settings()
    result = svc._operator_settings(original)

    assert result.pinterest_portfolio_planner_enabled is True
    assert result.pinterest_optimizer_enabled is True
    assert result.pinterest_portfolio_activation_enabled is True
    assert result.pinterest_optimizer_apply_enabled is True
    assert result.routine_pinterest_dry_run is True

    for name in svc._PROVIDER_GATE_FIELDS:
        assert getattr(result, name) is False
    for name in svc._AUTONOMOUS_DOWNSTREAM_FIELDS:
        assert getattr(result, name) is False

    assert original.pinterest_portfolio_planner_enabled is False
    assert original.pinterest_optimizer_enabled is False


def _execution_readiness():
    return {
        "ready": True,
        "blockers": [],
        "preview_fingerprint": PREVIEW_FP,
        "input_fingerprint": INPUT_FP,
        "existing_commitments": 2,
        "planned_active_slots": 148,
        "reserve_slots": 15,
    }


def _plan(status="DRAFT"):
    return SimpleNamespace(
        id="plan-1",
        status=status,
        plan_fingerprint=PREVIEW_FP,
        input_fingerprint=INPUT_FP,
        existing_commitments=2,
        planned_active_slots=148,
        reserve_slots=15,
    )


def _application():
    return SimpleNamespace(
        id="optimizer-1",
        status="APPLIED",
        optimizer_fingerprint=OPTIMIZER_FP,
        input_state_fingerprint=OPTIMIZER_INPUT_FP,
    )


def _patch_execute_success(monkeypatch, plan):
    app = _application()
    monkeypatch.setattr(svc, "phase_b1_readiness", lambda *a, **k: _execution_readiness())
    monkeypatch.setattr(svc, "_downstream_counts", lambda db: {
        "publications": 2,
        "destination_runs": 0,
        "execution_runs": 0,
        "generation_runs": 0,
        "seo_briefs": 0,
        "approvals": 2,
        "routine_permits": 0,
    })
    monkeypatch.setattr(svc, "create_draft_portfolio_plan", lambda *a, **k: plan)
    monkeypatch.setattr(
        svc,
        "optimizer_apply_readiness",
        lambda *a, **k: {
            "structurally_ready": True,
            "blockers": [],
            "planner_cap_contract_fingerprint": "e" * 64,
            "optimizable_item_count": 163,
            "optimizer_fingerprint": OPTIMIZER_FP,
            "input_state_fingerprint": OPTIMIZER_INPUT_FP,
        },
    )

    def apply(*args, **kwargs):
        plan.status = "ACTIVE"
        return app

    monkeypatch.setattr(svc, "apply_optimizer", apply)
    monkeypatch.setattr(svc, "_optimizer_applications", lambda db, plan_id: [app])
    monkeypatch.setattr(
        svc,
        "_plan_counts",
        lambda db, plan_id: {"total": 163, "active": 148, "reserve": 15},
    )
    return app


def test_execute_phase_b1_exact_success_and_zero_downstream_delta(monkeypatch):
    plan = _plan()
    _patch_execute_success(monkeypatch, plan)
    db = FakeDB(plan=plan)

    result = svc.execute_phase_b1(
        db,
        store_id=STORE_ID,
        month_key=MONTH_KEY,
        target_pins=TARGET,
        expected_preview_fingerprint=PREVIEW_FP,
        expected_input_fingerprint=INPUT_FP,
        expected_existing_commitments=2,
        expected_active_slots=148,
        expected_reserve_slots=15,
        settings=_settings(),
    )

    assert result["status"] == "SUCCEEDED"
    assert result["plan_id"] == "plan-1"
    assert result["plan_status"] == "ACTIVE"
    assert result["optimizer_application_id"] == "optimizer-1"
    assert result["active_slots"] == 148
    assert result["reserve_slots"] == 15
    assert result["downstream_counts_before"] == result["downstream_counts_after"]
    assert result["provider_called"] is False
    assert result["ai_called"] is False


def test_execute_phase_b1_resumes_exact_draft(monkeypatch):
    plan = _plan(status="DRAFT")
    _patch_execute_success(monkeypatch, plan)
    result = svc.execute_phase_b1(
        FakeDB(plan=plan),
        store_id=STORE_ID,
        month_key=MONTH_KEY,
        target_pins=TARGET,
        expected_preview_fingerprint=PREVIEW_FP,
        expected_input_fingerprint=INPUT_FP,
        expected_existing_commitments=2,
        expected_active_slots=148,
        expected_reserve_slots=15,
        settings=_settings(),
    )
    assert result["status"] == "SUCCEEDED"
    assert plan.status == "ACTIVE"


def test_execute_phase_b1_exact_active_retry_is_idempotent(monkeypatch):
    plan = _plan(status="ACTIVE")
    _patch_execute_success(monkeypatch, plan)
    result = svc.execute_phase_b1(
        FakeDB(plan=plan),
        store_id=STORE_ID,
        month_key=MONTH_KEY,
        target_pins=TARGET,
        expected_preview_fingerprint=PREVIEW_FP,
        expected_input_fingerprint=INPUT_FP,
        expected_existing_commitments=2,
        expected_active_slots=148,
        expected_reserve_slots=15,
        settings=_settings(),
    )
    assert result["optimizer_application_id"] == "optimizer-1"
    assert result["plan_status"] == "ACTIVE"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("expected_preview_fingerprint", "f" * 64, "PREVIEW_FINGERPRINT_MISMATCH"),
        ("expected_input_fingerprint", "f" * 64, "INPUT_FINGERPRINT_MISMATCH"),
        ("expected_existing_commitments", 0, "EXISTING_COMMITMENT_COUNT_MISMATCH"),
        ("expected_active_slots", 150, "ACTIVE_SLOT_COUNT_MISMATCH"),
        ("expected_reserve_slots", 14, "RESERVE_SLOT_COUNT_MISMATCH"),
    ],
)
def test_execute_phase_b1_expected_binding_mismatch_fails_before_mutation(
    monkeypatch, field, value, code
):
    monkeypatch.setattr(svc, "phase_b1_readiness", lambda *a, **k: _execution_readiness())

    def should_not_run(*args, **kwargs):
        raise AssertionError("mutation service must not run")

    monkeypatch.setattr(svc, "create_draft_portfolio_plan", should_not_run)
    payload = {
        "store_id": STORE_ID,
        "month_key": MONTH_KEY,
        "target_pins": TARGET,
        "expected_preview_fingerprint": PREVIEW_FP,
        "expected_input_fingerprint": INPUT_FP,
        "expected_existing_commitments": 2,
        "expected_active_slots": 148,
        "expected_reserve_slots": 15,
        "settings": _settings(),
    }
    payload[field] = value

    with pytest.raises(svc.PhaseB1OperatorError, match=code):
        svc.execute_phase_b1(object(), **payload)


def test_execute_phase_b1_optimizer_conflict_fails_closed(monkeypatch):
    plan = _plan()
    monkeypatch.setattr(svc, "phase_b1_readiness", lambda *a, **k: _execution_readiness())
    monkeypatch.setattr(svc, "_downstream_counts", lambda db: {key: 0 for key in svc._DOWNSTREAM_MODELS})
    monkeypatch.setattr(svc, "create_draft_portfolio_plan", lambda *a, **k: plan)
    monkeypatch.setattr(
        svc,
        "optimizer_apply_readiness",
        lambda *a, **k: {
            "structurally_ready": True,
            "blockers": [],
            "planner_cap_contract_fingerprint": "e" * 64,
            "optimizable_item_count": 163,
            "optimizer_fingerprint": OPTIMIZER_FP,
            "input_state_fingerprint": OPTIMIZER_INPUT_FP,
        },
    )

    def conflict(*args, **kwargs):
        raise svc.OptimizerApplyError("OPTIMIZER_APPLICATION_CONFLICT")

    monkeypatch.setattr(svc, "apply_optimizer", conflict)

    with pytest.raises(svc.PhaseB1OperatorError, match="OPTIMIZER_APPLICATION_CONFLICT"):
        svc.execute_phase_b1(
            FakeDB(plan=plan),
            store_id=STORE_ID,
            month_key=MONTH_KEY,
            target_pins=TARGET,
            expected_preview_fingerprint=PREVIEW_FP,
            expected_input_fingerprint=INPUT_FP,
            expected_existing_commitments=2,
            expected_active_slots=148,
            expected_reserve_slots=15,
            settings=_settings(),
        )
