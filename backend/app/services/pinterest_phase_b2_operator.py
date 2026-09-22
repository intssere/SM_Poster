from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update

from app.core.config import Settings, get_settings
from app.models.domain import (
    AuditLog,
    PinApproval,
    PinCreative,
    PinPublication,
    PinterestAutonomousDestinationRun,
    PinterestAutonomousExecutionRun,
    PinterestAutonomousGenerationRun,
    PinterestBoard,
    PinterestBoardProvisioningAttempt,
    PinterestConnection,
    PinterestOptimizerApplication,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
    PinterestSeoBrief,
    PublicationStatus,
)
from app.models.routine_publishing import RoutineDispatchPermit
from app.services.creative_rendering import CreativeRenderService
from app.services.media_storage import StorageCorrupt, StorageMissing, StorageUnavailable
from app.services.pinterest_autonomous_destination import (
    AutonomousDestinationError,
    destination_readiness,
    ensure_autonomous_destination,
)
from app.services.pinterest_autonomous_execution import execution_readiness
from app.services.pinterest_autonomous_generation import _select_authentic_image
from app.services.product_source_cache import (
    ProductSourceStorage,
    cached_downloader,
)
from app.services.routine_autonomous_authorization import AUTONOMOUS_ACTOR


class PhaseB2OperatorError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_EXPECTED_DISABLED_BLOCKERS = {
    "SEO_BRIEF_PERSISTENCE_DISABLED",
    "AUTONOMOUS_GENERATION_DISABLED",
    "AUTONOMOUS_AUTHORIZATION_DISABLED",
    "AUTONOMOUS_EXECUTION_DISABLED",
}

_PROVIDER_GATE_FIELDS = (
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
)

_GLOBAL_INTERNAL_FIELDS = (
    "pinterest_seo_brief_persistence_enabled",
    "pinterest_autonomous_generation_enabled",
    "routine_autonomous_authorization_enabled",
    "pinterest_autonomous_execution_enabled",
    "pinterest_autonomous_board_ensure_enabled",
    "pinterest_analytics_ingestion_enabled",
    "pinterest_learning_snapshot_persistence_enabled",
)

_OPERATOR_ENABLED_FIELDS = (
    "pinterest_seo_brief_persistence_enabled",
    "pinterest_autonomous_generation_enabled",
    "routine_autonomous_authorization_enabled",
    "pinterest_autonomous_execution_enabled",
)

_RECOVERABLE_ERROR_CODES = {
    "PinterestSeoError",
    "PINTEREST_SEO_BRIEF_PERSISTENCE_DISABLED",
}
_RECOVERY_BLOCKER_CODES = {
    "AUTONOMOUS_DESTINATION_FAILED_RECONCILIATION_REQUIRED",
    "AUTONOMOUS_DESTINATION_RECONCILIATION_REQUIRED",
    "AUTONOMOUS_EXECUTION_FAILED_RECONCILIATION_REQUIRED",
}
_RECOVERY_REQUIRED_BLOCKER = "PHASE_B2_RECOVERY_REQUIRED"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _publish_unknown_count(db) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(PinPublication)
            .where(PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN)
        )
        or 0
    )


def _optimizer_for_plan(db, plan_id: str) -> PinterestOptimizerApplication | None:
    return db.scalar(
        select(PinterestOptimizerApplication)
        .where(PinterestOptimizerApplication.plan_id == plan_id)
        .limit(1)
    )


def _destination_run(db, item_id: str) -> PinterestAutonomousDestinationRun | None:
    return db.scalar(
        select(PinterestAutonomousDestinationRun)
        .where(PinterestAutonomousDestinationRun.portfolio_item_id == item_id)
        .limit(1)
    )


def _execution_run(db, item_id: str) -> PinterestAutonomousExecutionRun | None:
    return db.scalar(
        select(PinterestAutonomousExecutionRun)
        .where(PinterestAutonomousExecutionRun.portfolio_item_id == item_id)
        .limit(1)
    )


def _generation_run(db, item_id: str) -> PinterestAutonomousGenerationRun | None:
    return db.scalar(
        select(PinterestAutonomousGenerationRun)
        .where(PinterestAutonomousGenerationRun.portfolio_item_id == item_id)
        .limit(1)
    )


def _recoverable_pre_seo_failure(
    db,
    *,
    item: PinterestPortfolioPlanItem,
    destination_run: PinterestAutonomousDestinationRun | None,
    execution_run: PinterestAutonomousExecutionRun | None,
    destination_input_fingerprint: str | None,
    execution_input_fingerprint: str | None,
    expected_board_id: str | None,
    cache_ready: bool,
    publish_unknown_count: int,
    safety_blockers: list[str],
) -> dict[str, Any]:
    blockers: list[str] = []
    if destination_run is None or execution_run is None:
        blockers.append("RECOVERY_RUN_PAIR_REQUIRED")
    else:
        if (
            destination_run.status != "FAILED"
            or destination_run.stage != "BOARD_READY"
            or destination_run.input_fingerprint != destination_input_fingerprint
            or destination_run.pinterest_board_record_id != expected_board_id
            or destination_run.board_provisioning_attempt_id is not None
            or destination_run.autonomous_execution_run_id is not None
        ):
            blockers.append("DESTINATION_FAILURE_SHAPE_MISMATCH")
        destination_meta = destination_run.safe_metadata or {}
        if (
            destination_meta.get("provider_called") is not False
            or destination_meta.get("ai_called") is not False
            or destination_meta.get("error_code") not in _RECOVERABLE_ERROR_CODES
        ):
            blockers.append("DESTINATION_FAILURE_METADATA_MISMATCH")

        if (
            execution_run.status != "FAILED"
            or execution_run.stage != "STARTED"
            or execution_run.input_fingerprint != execution_input_fingerprint
            or execution_run.seo_brief_id is not None
            or execution_run.generation_run_id is not None
            or execution_run.approval_id is not None
            or execution_run.publication_id is not None
            or execution_run.routine_permit_id is not None
        ):
            blockers.append("EXECUTION_FAILURE_SHAPE_MISMATCH")
        execution_meta = execution_run.safe_metadata or {}
        if (
            execution_meta.get("provider_called") is not False
            or execution_meta.get("ai_called") is not False
            or execution_meta.get("error_code") not in _RECOVERABLE_ERROR_CODES
        ):
            blockers.append("EXECUTION_FAILURE_METADATA_MISMATCH")

    if item.status != "PLANNED" or item.publication_id is not None:
        blockers.append("PORTFOLIO_ITEM_RECOVERY_STATE_MISMATCH")
    if publish_unknown_count:
        blockers.append("PUBLISH_UNKNOWN_PRESENT")
    if cache_ready is not True:
        blockers.append("LOCAL_PRODUCT_SOURCE_REQUIRED")
    if safety_blockers:
        blockers.append("UNSAFE_RUNTIME_GATE_STATE")

    seo_count = int(
        db.scalar(
            select(func.count())
            .select_from(PinterestSeoBrief)
            .where(PinterestSeoBrief.portfolio_item_id == item.id)
        )
        or 0
    )
    generation_count = int(
        db.scalar(
            select(func.count())
            .select_from(PinterestAutonomousGenerationRun)
            .where(PinterestAutonomousGenerationRun.portfolio_item_id == item.id)
        )
        or 0
    )
    if seo_count or generation_count:
        blockers.append("DOWNSTREAM_STATE_ALREADY_EXISTS")

    blockers = list(dict.fromkeys(blockers))
    return {
        "recoverable": not blockers,
        "blockers": blockers,
        "destination_run_id": destination_run.id if destination_run else None,
        "execution_run_id": execution_run.id if execution_run else None,
        "reason": (
            "PINTEREST_SEO_BRIEF_PERSISTENCE_DISABLED"
            if not blockers
            else None
        ),
    }


def _recovery_metadata(
    metadata: dict | None,
    *,
    now: datetime,
    reason: str,
) -> dict[str, Any]:
    previous = dict(metadata or {})
    prior_error = previous.pop("error_code", None)
    previous["phase_b2_recovery"] = {
        "reason": reason,
        "previous_error_code": prior_error,
        "reopened_at": now.isoformat(),
        "provider_called": False,
        "ai_called": False,
    }
    return previous


def _reopen_exact_pre_seo_failure(
    db,
    *,
    item: PinterestPortfolioPlanItem,
    recovery: dict[str, Any],
    expected_destination_input_fingerprint: str,
    expected_execution_input_fingerprint: str,
    now: datetime,
) -> None:
    if recovery.get("recoverable") is not True:
        raise PhaseB2OperatorError("PHASE_B2_RECOVERY_NOT_ALLOWED")
    destination = db.get(
        PinterestAutonomousDestinationRun,
        recovery.get("destination_run_id"),
    )
    execution = db.get(
        PinterestAutonomousExecutionRun,
        recovery.get("execution_run_id"),
    )
    if destination is None or execution is None:
        raise PhaseB2OperatorError("PHASE_B2_RECOVERY_RUN_PAIR_MISSING")

    reason = recovery.get("reason") or "PINTEREST_SEO_BRIEF_PERSISTENCE_DISABLED"
    execution_changed = db.execute(
        update(PinterestAutonomousExecutionRun)
        .where(
            PinterestAutonomousExecutionRun.id == execution.id,
            PinterestAutonomousExecutionRun.portfolio_item_id == item.id,
            PinterestAutonomousExecutionRun.status == "FAILED",
            PinterestAutonomousExecutionRun.stage == "STARTED",
            PinterestAutonomousExecutionRun.input_fingerprint
            == expected_execution_input_fingerprint,
            PinterestAutonomousExecutionRun.seo_brief_id.is_(None),
            PinterestAutonomousExecutionRun.generation_run_id.is_(None),
            PinterestAutonomousExecutionRun.approval_id.is_(None),
            PinterestAutonomousExecutionRun.publication_id.is_(None),
            PinterestAutonomousExecutionRun.routine_permit_id.is_(None),
        )
        .values(
            status="STARTED",
            completed_at=None,
            safe_metadata=_recovery_metadata(
                execution.safe_metadata,
                now=now,
                reason=reason,
            ),
        )
    )
    destination_changed = db.execute(
        update(PinterestAutonomousDestinationRun)
        .where(
            PinterestAutonomousDestinationRun.id == destination.id,
            PinterestAutonomousDestinationRun.portfolio_item_id == item.id,
            PinterestAutonomousDestinationRun.status == "FAILED",
            PinterestAutonomousDestinationRun.stage == "BOARD_READY",
            PinterestAutonomousDestinationRun.input_fingerprint
            == expected_destination_input_fingerprint,
            PinterestAutonomousDestinationRun.board_provisioning_attempt_id.is_(None),
            PinterestAutonomousDestinationRun.autonomous_execution_run_id.is_(None),
        )
        .values(
            status="STARTED",
            completed_at=None,
            safe_metadata=_recovery_metadata(
                destination.safe_metadata,
                now=now,
                reason=reason,
            ),
        )
    )
    if int(execution_changed.rowcount or 0) != 1 or int(destination_changed.rowcount or 0) != 1:
        db.rollback()
        raise PhaseB2OperatorError("PHASE_B2_RECOVERY_COMPARE_AND_SET_FAILED")

    db.add(AuditLog(
        actor="phase-b2-operator-v1",
        action="PHASE_B2_PRE_SEO_FAILURE_REOPENED",
        entity_type="PinterestPortfolioPlanItem",
        entity_id=item.id,
        metadata_json={
            "destination_run_id": destination.id,
            "execution_run_id": execution.id,
            "reason": reason,
            "provider_called": False,
            "ai_called": False,
        },
    ))
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise PhaseB2OperatorError("PHASE_B2_RECOVERY_COMMIT_FAILED") from exc
    db.expire_all()


def _gate_state(settings: Settings) -> dict[str, bool]:
    names = (
        *_PROVIDER_GATE_FIELDS,
        *_GLOBAL_INTERNAL_FIELDS,
        "routine_pinterest_dry_run",
    )
    return {name: bool(getattr(settings, name)) for name in names}


def _safety_blockers(settings: Settings) -> list[str]:
    blockers = [
        f"{name.upper()}_MUST_BE_FALSE"
        for name in _PROVIDER_GATE_FIELDS
        if bool(getattr(settings, name))
    ]
    blockers.extend(
        f"GLOBAL_{name.upper()}_MUST_BE_FALSE"
        for name in _GLOBAL_INTERNAL_FIELDS
        if bool(getattr(settings, name))
    )
    if settings.routine_pinterest_dry_run is not True:
        blockers.append("ROUTINE_PINTEREST_DRY_RUN_REQUIRED")
    return blockers


def _operator_settings(settings: Settings) -> Settings:
    updates = {
        "routine_pinterest_dry_run": True,
    }
    updates.update({name: False for name in _PROVIDER_GATE_FIELDS})
    updates.update({name: False for name in _GLOBAL_INTERNAL_FIELDS})
    updates.update({name: True for name in _OPERATOR_ENABLED_FIELDS})
    return settings.model_copy(deep=True, update=updates)


def _item_snapshot(db, plan_id: str) -> dict[str, tuple[str, str | None]]:
    rows = db.execute(
        select(
            PinterestPortfolioPlanItem.id,
            PinterestPortfolioPlanItem.status,
            PinterestPortfolioPlanItem.publication_id,
        )
        .where(PinterestPortfolioPlanItem.plan_id == plan_id)
        .order_by(PinterestPortfolioPlanItem.id)
    ).all()
    return {
        row[0]: (row[1], row[2])
        for row in rows
    }


def _provider_tripwire():
    class Tripwire:
        def __getattr__(self, name):
            raise PhaseB2OperatorError(f"PROVIDER_TRIPWIRE_CALLED:{name}")
    return Tripwire()


def _cache_status(
    db,
    item: PinterestPortfolioPlanItem,
    *,
    settings: Settings,
    source_storage: ProductSourceStorage | None,
) -> dict[str, Any]:
    generation = _generation_run(db, item.id)
    if (
        generation is not None
        and generation.status == "SUCCEEDED"
        and generation.creative_id
    ):
        creative = db.get(PinCreative, generation.creative_id)
        if creative is not None and creative.render_status == "RENDERED":
            return {
                "ready": True,
                "required": False,
                "source_image_id": creative.source_image_id,
                "source_sha256": None,
                "source_bytes": None,
                "blocker": None,
            }

    image, image_blockers = _select_authentic_image(db, item.product_id)
    if image_blockers or image is None:
        return {
            "ready": False,
            "required": True,
            "source_image_id": image.id if image else None,
            "source_sha256": image.source_sha256 if image else None,
            "source_bytes": None,
            "blocker": (image_blockers or ["AUTHENTIC_IMAGE_REQUIRED"])[0],
        }
    if not image.source_sha256:
        return {
            "ready": False,
            "required": True,
            "source_image_id": image.id,
            "source_sha256": None,
            "source_bytes": None,
            "blocker": "LOCAL_PRODUCT_SOURCE_DIGEST_REQUIRED",
        }

    storage = source_storage or ProductSourceStorage(settings=settings)
    try:
        source_bytes = storage.read_verified(image.id, image.source_sha256)
    except StorageMissing:
        blocker = "LOCAL_PRODUCT_SOURCE_REQUIRED"
    except StorageCorrupt:
        blocker = "LOCAL_PRODUCT_SOURCE_CORRUPT"
    except StorageUnavailable:
        blocker = "LOCAL_PRODUCT_SOURCE_STORAGE_UNAVAILABLE"
    except ValueError:
        blocker = "LOCAL_PRODUCT_SOURCE_IDENTITY_INVALID"
    else:
        return {
            "ready": True,
            "required": True,
            "source_image_id": image.id,
            "source_sha256": image.source_sha256,
            "source_url": image.source_url,
            "source_bytes": source_bytes,
            "blocker": None,
        }

    return {
        "ready": False,
        "required": True,
        "source_image_id": image.id,
        "source_sha256": image.source_sha256,
        "source_bytes": None,
        "blocker": blocker,
    }


def phase_b2_readiness(
    db,
    *,
    portfolio_item_id: str,
    settings: Settings | None = None,
    now: datetime | None = None,
    source_storage: ProductSourceStorage | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    now = _utc(now or datetime.now(timezone.utc))
    item = db.get(PinterestPortfolioPlanItem, portfolio_item_id)
    if item is None:
        raise PhaseB2OperatorError("PORTFOLIO_ITEM_NOT_FOUND")

    plan = db.get(PinterestPortfolioPlan, item.plan_id)
    optimizer = _optimizer_for_plan(db, item.plan_id)
    destination = destination_readiness(
        db,
        item.id,
        settings=settings,
        now=now,
    )
    execution = execution_readiness(
        db,
        item.id,
        settings=settings,
        now=now,
    )

    raw_blockers = list(dict.fromkeys([
        *(destination.get("blockers") or []),
        *(execution.get("blockers") or []),
    ]))
    structural_blockers = [
        code for code in raw_blockers
        if code not in _EXPECTED_DISABLED_BLOCKERS
    ]
    blockers: list[str] = list(structural_blockers)

    if plan is None or plan.status != "ACTIVE":
        blockers.append("PORTFOLIO_PLAN_NOT_ACTIVE")
    if optimizer is None or optimizer.status != "APPLIED":
        blockers.append("OPTIMIZER_APPLICATION_REQUIRED")
    optimizer_binding = execution.get("optimizer_binding") or {}
    if optimizer is not None and (
        optimizer_binding.get("application_id") != optimizer.id
        or optimizer_binding.get("optimizer_fingerprint")
        != optimizer.optimizer_fingerprint
        or optimizer_binding.get("item_optimizer_fingerprint")
        != optimizer.optimizer_fingerprint
    ):
        blockers.append("OPTIMIZER_ITEM_BINDING_MISMATCH")

    destination_board = destination.get("board_strategy") or {}
    execution_board = execution.get("board_routing") or {}
    if destination_board.get("status") != "ROUTE_EXISTING":
        blockers.append("DESTINATION_ROUTE_EXISTING_REQUIRED")
    if execution_board.get("status") != "ROUTE_EXISTING":
        blockers.append("EXECUTION_ROUTE_EXISTING_REQUIRED")
    if (
        destination_board.get("selected_board_id")
        != execution_board.get("selected_board_id")
        or destination_board.get("selected_external_board_id")
        != execution_board.get("selected_external_board_id")
    ):
        blockers.append("DESTINATION_EXECUTION_BOARD_DRIFT")

    scheduled_for = execution.get("scheduled_for")
    if scheduled_for is None:
        blockers.append("SCHEDULE_TIME_REQUIRED")
    elif _utc(scheduled_for) <= now:
        blockers.append("SCHEDULE_TIME_NOT_FUTURE")

    existing_destination = _destination_run(db, item.id)
    existing_execution = _execution_run(db, item.id)
    if existing_destination is None and existing_execution is None:
        if item.is_reserve:
            blockers.append("RESERVE_ITEM_NOT_EXECUTABLE")
        if item.status != "PLANNED":
            blockers.append("PORTFOLIO_ITEM_NOT_PLANNED")
        if item.publication_id is not None:
            blockers.append("PORTFOLIO_ITEM_PUBLICATION_ALREADY_SET")
    else:
        if existing_destination is not None and existing_destination.status in {"FAILED", "UNKNOWN"}:
            blockers.append("AUTONOMOUS_DESTINATION_RECONCILIATION_REQUIRED")
        if existing_execution is not None and existing_execution.status == "FAILED":
            blockers.append("AUTONOMOUS_EXECUTION_FAILED_RECONCILIATION_REQUIRED")

    publish_unknown_count = _publish_unknown_count(db)
    if publish_unknown_count:
        blockers.append("PUBLISH_UNKNOWN_PRESENT")

    safety_blockers = _safety_blockers(settings)
    blockers.extend(safety_blockers)

    cache = _cache_status(
        db,
        item,
        settings=settings,
        source_storage=source_storage,
    )
    if cache.get("ready") is not True and cache.get("blocker"):
        blockers.append(cache["blocker"])

    recovery = _recoverable_pre_seo_failure(
        db,
        item=item,
        destination_run=existing_destination,
        execution_run=existing_execution,
        destination_input_fingerprint=destination.get("input_fingerprint"),
        execution_input_fingerprint=execution.get("input_fingerprint"),
        expected_board_id=destination_board.get("selected_board_id"),
        cache_ready=cache.get("ready") is True,
        publish_unknown_count=publish_unknown_count,
        safety_blockers=safety_blockers,
    )
    if recovery.get("recoverable") is True:
        structural_blockers = [
            code for code in structural_blockers
            if code not in _RECOVERY_BLOCKER_CODES
        ]
        blockers = [
            code for code in blockers
            if code not in _RECOVERY_BLOCKER_CODES
        ]
        blockers.append(_RECOVERY_REQUIRED_BLOCKER)

    blockers = list(dict.fromkeys(blockers))
    non_structural = _EXPECTED_DISABLED_BLOCKERS | {_RECOVERY_REQUIRED_BLOCKER}
    structural_remaining = [
        code for code in blockers
        if code not in non_structural
    ]

    return {
        "ready": not blockers,
        "structurally_ready": not structural_remaining and not structural_blockers,
        "recoverable_provider_free_failure": recovery.get("recoverable") is True,
        "recovery": recovery,
        "blockers": blockers,
        "raw_destination_blockers": list(destination.get("blockers") or []),
        "raw_execution_blockers": list(execution.get("blockers") or []),
        "expected_disabled_blockers": sorted(_EXPECTED_DISABLED_BLOCKERS),
        "portfolio_item_id": item.id,
        "item_fingerprint": item.item_fingerprint,
        "item_status": item.status,
        "publication_id": item.publication_id,
        "plan_id": item.plan_id,
        "plan_status": plan.status if plan else None,
        "optimizer_application_id": optimizer.id if optimizer else None,
        "optimizer_fingerprint": optimizer.optimizer_fingerprint if optimizer else None,
        "recommended_position": optimizer_binding.get("recommended_position"),
        "scheduled_for": scheduled_for,
        "destination_input_fingerprint": destination.get("input_fingerprint"),
        "execution_input_fingerprint": execution.get("input_fingerprint"),
        "pinterest_board_record_id": destination_board.get("selected_board_id"),
        "external_board_id": destination_board.get("selected_external_board_id"),
        "board_key": item.board_key_snapshot,
        "source_cache": {
            "ready": cache.get("ready"),
            "required": cache.get("required"),
            "source_image_id": cache.get("source_image_id"),
            "source_sha256": cache.get("source_sha256"),
            "blocker": cache.get("blocker"),
        },
        "existing_destination_run": {
            "id": existing_destination.id if existing_destination else None,
            "status": existing_destination.status if existing_destination else None,
            "stage": existing_destination.stage if existing_destination else None,
        },
        "existing_execution_run": {
            "id": existing_execution.id if existing_execution else None,
            "status": existing_execution.status if existing_execution else None,
            "stage": existing_execution.stage if existing_execution else None,
        },
        "publish_unknown_count": publish_unknown_count,
        "gate_state": _gate_state(settings),
        "state_mutated": False,
        "provider_called": False,
        "ai_called": False,
    }

def _require_exact(expected: Any, actual: Any, code: str) -> None:
    if expected != actual:
        raise PhaseB2OperatorError(code)


def _safe_metadata_is_provider_free(metadata: dict | None) -> bool:
    metadata = metadata or {}
    return (
        metadata.get("provider_called") is False
        and metadata.get("ai_called") is False
    )


async def execute_phase_b2(
    db,
    *,
    portfolio_item_id: str,
    expected_plan_id: str,
    expected_optimizer_application_id: str,
    expected_item_fingerprint: str,
    expected_destination_input_fingerprint: str,
    expected_execution_input_fingerprint: str,
    expected_scheduled_for: datetime,
    expected_pinterest_board_record_id: str,
    expected_external_board_id: str,
    settings: Settings | None = None,
    now: datetime | None = None,
    source_storage: ProductSourceStorage | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    now = _utc(now or datetime.now(timezone.utc))
    readiness = phase_b2_readiness(
        db,
        portfolio_item_id=portfolio_item_id,
        settings=settings,
        now=now,
        source_storage=source_storage,
    )
    if readiness.get("structurally_ready") is not True:
        raise PhaseB2OperatorError(
            (readiness.get("blockers") or ["PHASE_B2_NOT_READY"])[0]
        )

    _require_exact(expected_plan_id, readiness.get("plan_id"), "PLAN_ID_MISMATCH")
    _require_exact(
        expected_optimizer_application_id,
        readiness.get("optimizer_application_id"),
        "OPTIMIZER_APPLICATION_ID_MISMATCH",
    )
    _require_exact(
        expected_item_fingerprint,
        readiness.get("item_fingerprint"),
        "ITEM_FINGERPRINT_MISMATCH",
    )
    _require_exact(
        expected_destination_input_fingerprint,
        readiness.get("destination_input_fingerprint"),
        "DESTINATION_INPUT_FINGERPRINT_MISMATCH",
    )
    _require_exact(
        expected_execution_input_fingerprint,
        readiness.get("execution_input_fingerprint"),
        "EXECUTION_INPUT_FINGERPRINT_MISMATCH",
    )
    _require_exact(
        _utc(expected_scheduled_for),
        _utc(readiness.get("scheduled_for")),
        "SCHEDULE_TIME_MISMATCH",
    )
    _require_exact(
        expected_pinterest_board_record_id,
        readiness.get("pinterest_board_record_id"),
        "PINTEREST_BOARD_RECORD_MISMATCH",
    )
    _require_exact(
        expected_external_board_id,
        readiness.get("external_board_id"),
        "PINTEREST_EXTERNAL_BOARD_MISMATCH",
    )
    if readiness.get("source_cache", {}).get("ready") is not True:
        raise PhaseB2OperatorError(
            readiness.get("source_cache", {}).get("blocker")
            or "LOCAL_PRODUCT_SOURCE_REQUIRED"
        )

    item = db.get(PinterestPortfolioPlanItem, portfolio_item_id)
    plan = db.get(PinterestPortfolioPlan, expected_plan_id)
    optimizer = db.get(PinterestOptimizerApplication, expected_optimizer_application_id)
    board = db.get(PinterestBoard, expected_pinterest_board_record_id)
    if item is None or plan is None or optimizer is None or board is None:
        raise PhaseB2OperatorError("PHASE_B2_IDENTITY_INCOMPLETE")
    if board.external_board_id != expected_external_board_id:
        raise PhaseB2OperatorError("PINTEREST_EXTERNAL_BOARD_MISMATCH")
    connection = db.get(PinterestConnection, board.connection_id)
    if (
        not board.is_active
        or not board.is_eligible
        or connection is None
        or connection.status != "CONNECTED"
    ):
        raise PhaseB2OperatorError("PINTEREST_DESTINATION_DRIFT")

    before_items = _item_snapshot(db, plan.id)
    before_provisioning_count = int(
        db.scalar(
            select(func.count()).select_from(PinterestBoardProvisioningAttempt)
        )
        or 0
    )

    operator_settings = _operator_settings(settings)
    enabled_readiness = destination_readiness(
        db,
        item.id,
        settings=operator_settings,
        now=now,
    )
    enabled_execution = execution_readiness(
        db,
        item.id,
        settings=operator_settings,
        now=now,
    )
    if enabled_readiness.get("ready") is not True:
        raise PhaseB2OperatorError(
            (enabled_readiness.get("blockers") or ["DESTINATION_NOT_READY"])[0]
        )
    if enabled_execution.get("ready") is not True:
        raise PhaseB2OperatorError(
            (enabled_execution.get("blockers") or ["EXECUTION_NOT_READY"])[0]
        )
    _require_exact(
        expected_destination_input_fingerprint,
        enabled_readiness.get("input_fingerprint"),
        "DESTINATION_INPUT_FINGERPRINT_DRIFT",
    )
    _require_exact(
        expected_execution_input_fingerprint,
        enabled_execution.get("input_fingerprint"),
        "EXECUTION_INPUT_FINGERPRINT_DRIFT",
    )
    _require_exact(
        expected_pinterest_board_record_id,
        (enabled_readiness.get("board_strategy") or {}).get("selected_board_id"),
        "PINTEREST_BOARD_RECORD_DRIFT",
    )
    _require_exact(
        expected_external_board_id,
        (enabled_readiness.get("board_strategy") or {}).get(
            "selected_external_board_id"
        ),
        "PINTEREST_EXTERNAL_BOARD_DRIFT",
    )

    cache = _cache_status(
        db,
        item,
        settings=settings,
        source_storage=source_storage,
    )
    source_bytes = cache.get("source_bytes")
    if cache.get("required") and not source_bytes:
        raise PhaseB2OperatorError(
            cache.get("blocker") or "LOCAL_PRODUCT_SOURCE_REQUIRED"
        )

    renderer = None
    if source_bytes:
        image, image_blockers = _select_authentic_image(db, item.product_id)
        if image_blockers or image is None:
            raise PhaseB2OperatorError(
                (image_blockers or ["AUTHENTIC_IMAGE_REQUIRED"])[0]
            )
        renderer = CreativeRenderService(
            downloader=cached_downloader(
                expected_url=image.source_url,
                source_bytes=source_bytes,
            )
        )

    try:
        destination_run = await ensure_autonomous_destination(
            db,
            item.id,
            settings=operator_settings,
            provisioning_client=_provider_tripwire(),
            sync_client=_provider_tripwire(),
            renderer=renderer,
            now=now,
        )
    except AutonomousDestinationError as exc:
        raise PhaseB2OperatorError(exc.code) from None

    destination_run = db.get(PinterestAutonomousDestinationRun, destination_run.id)
    if (
        destination_run is None
        or destination_run.status != "SUCCEEDED"
        or destination_run.stage != "EXECUTION_READY"
        or destination_run.pinterest_board_record_id
        != expected_pinterest_board_record_id
        or not destination_run.autonomous_execution_run_id
    ):
        raise PhaseB2OperatorError("DESTINATION_POSTCONDITION_FAILED")
    if not _safe_metadata_is_provider_free(destination_run.safe_metadata):
        raise PhaseB2OperatorError("DESTINATION_PROVIDER_METADATA_DRIFT")

    execution_run = db.get(
        PinterestAutonomousExecutionRun,
        destination_run.autonomous_execution_run_id,
    )
    if (
        execution_run is None
        or execution_run.portfolio_item_id != item.id
        or execution_run.plan_id != plan.id
        or execution_run.optimizer_application_id != optimizer.id
        or execution_run.status != "SUCCEEDED"
        or execution_run.stage != "PERMITTED"
        or execution_run.input_fingerprint
        != expected_execution_input_fingerprint
        or _utc(execution_run.scheduled_for) != _utc(expected_scheduled_for)
        or not execution_run.seo_brief_id
        or not execution_run.generation_run_id
        or not execution_run.approval_id
        or not execution_run.publication_id
        or not execution_run.routine_permit_id
    ):
        raise PhaseB2OperatorError("EXECUTION_POSTCONDITION_FAILED")
    if not _safe_metadata_is_provider_free(execution_run.safe_metadata):
        raise PhaseB2OperatorError("EXECUTION_PROVIDER_METADATA_DRIFT")

    seo = db.get(PinterestSeoBrief, execution_run.seo_brief_id)
    generation = db.get(
        PinterestAutonomousGenerationRun,
        execution_run.generation_run_id,
    )
    approval = db.get(PinApproval, execution_run.approval_id)
    publication = db.get(PinPublication, execution_run.publication_id)
    permit = db.get(RoutineDispatchPermit, execution_run.routine_permit_id)
    if seo is None or seo.portfolio_item_id != item.id or seo.status != "CURRENT":
        raise PhaseB2OperatorError("SEO_POSTCONDITION_FAILED")
    if (
        generation is None
        or generation.portfolio_item_id != item.id
        or generation.seo_brief_id != seo.id
        or generation.status != "SUCCEEDED"
        or not generation.draft_id
        or not generation.creative_id
        or not _safe_metadata_is_provider_free(generation.safe_metadata)
    ):
        raise PhaseB2OperatorError("GENERATION_POSTCONDITION_FAILED")
    creative = db.get(PinCreative, generation.creative_id)
    if creative is None or creative.render_status != "RENDERED":
        raise PhaseB2OperatorError("CREATIVE_POSTCONDITION_FAILED")
    if (
        approval is None
        or approval.decision != "APPROVED"
        or approval.decided_by != AUTONOMOUS_ACTOR
        or approval.draft_id != generation.draft_id
        or approval.creative_id != generation.creative_id
    ):
        raise PhaseB2OperatorError("APPROVAL_POSTCONDITION_FAILED")
    if (
        publication is None
        or publication.status != PublicationStatus.SCHEDULED
        or publication.approval_id != approval.id
        or publication.draft_id != generation.draft_id
        or publication.creative_id != generation.creative_id
        or publication.pinterest_board_record_id != board.id
        or publication.pinterest_board_id_snapshot != board.external_board_id
        or publication.pinterest_connection_id != connection.id
        or publication.scheduled_for is None
        or _utc(publication.scheduled_for) != _utc(expected_scheduled_for)
    ):
        raise PhaseB2OperatorError("PUBLICATION_POSTCONDITION_FAILED")
    if (
        permit is None
        or permit.publication_id != publication.id
        or permit.status != "ACTIVE"
        or permit.authorized_by != AUTONOMOUS_ACTOR
    ):
        raise PhaseB2OperatorError("PERMIT_POSTCONDITION_FAILED")

    item = db.get(PinterestPortfolioPlanItem, item.id)
    if (
        item is None
        or item.status != "SCHEDULED"
        or item.publication_id != publication.id
    ):
        raise PhaseB2OperatorError("PORTFOLIO_ITEM_POSTCONDITION_FAILED")

    after_items = _item_snapshot(db, plan.id)
    for item_id, before_state in before_items.items():
        if item_id == item.id:
            continue
        if after_items.get(item_id) != before_state:
            raise PhaseB2OperatorError("UNRELATED_PORTFOLIO_ITEM_MUTATED")

    after_provisioning_count = int(
        db.scalar(
            select(func.count()).select_from(PinterestBoardProvisioningAttempt)
        )
        or 0
    )
    if after_provisioning_count != before_provisioning_count:
        raise PhaseB2OperatorError("BOARD_PROVISIONING_STATE_CHANGED")

    return {
        "status": "SUCCEEDED",
        "portfolio_item_id": item.id,
        "plan_id": plan.id,
        "optimizer_application_id": optimizer.id,
        "item_fingerprint": item.item_fingerprint,
        "scheduled_for": publication.scheduled_for,
        "destination_input_fingerprint": destination_run.input_fingerprint,
        "execution_input_fingerprint": execution_run.input_fingerprint,
        "pinterest_board_record_id": board.id,
        "external_board_id": board.external_board_id,
        "destination_run_id": destination_run.id,
        "destination_status": destination_run.status,
        "destination_stage": destination_run.stage,
        "execution_run_id": execution_run.id,
        "execution_status": execution_run.status,
        "execution_stage": execution_run.stage,
        "seo_brief_id": seo.id,
        "generation_run_id": generation.id,
        "draft_id": generation.draft_id,
        "creative_id": generation.creative_id,
        "approval_id": approval.id,
        "publication_id": publication.id,
        "publication_status": (
            publication.status.value
            if hasattr(publication.status, "value")
            else str(publication.status)
        ),
        "routine_permit_id": permit.id,
        "routine_permit_status": permit.status,
        "source_image_id": readiness.get("source_cache", {}).get("source_image_id"),
        "source_sha256": readiness.get("source_cache", {}).get("source_sha256"),
        "provider_called": False,
        "ai_called": False,
    }
