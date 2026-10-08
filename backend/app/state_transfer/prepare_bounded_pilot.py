"""One-shot, preflight-bound five-Pin preparation with process-local gates only."""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from uuid import UUID, uuid5

from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.models.domain import AuditLog
from app.state_transfer.production_media_certification import gate_snapshot
from app.state_transfer.catalog import digest
from app.services.bounded_pilot_preparation_operator import (
    BoundedPreparationOperatorError,
    prepare_certified_batch,
)


MODE = "ONE_SHOT_FIVE_PIN_BOUNDED_PREPARATION"
ACTOR = "bounded-pilot-preparation-cli"
INVOCATION_ENV = "BOUNDED_PREPARATION_INVOCATION_ID"
INVOCATION_ACTION = "bounded_pilot_preparation_invocation_v1"
INVOCATION_ACTOR = "bounded-preparation-invocation-v1"
INVOCATION_ENTITY_TYPE = "bounded_preparation_invocation"
INVOCATION_NAMESPACE = UUID("ebd277c7-d2a4-43ed-aea2-f04fb3739e86")
INVOCATION_RE = re.compile(r"[0-9a-f]{64}\Z")


def _base_result():
    return {
        "success": False,
        "mode": MODE,
        "terminal_stage": "PREFLIGHT",
        "code": None,
        "persistent_configuration_mutated": False,
        "process_local_bounded_override": False,
        "database_mutation_authorized": False,
        "invocation_guard_claimed": False,
        "invocation_guard_database_writes": 0,
        "invocation_receipt_id": None,
        "provider_calls": 0,
        "buffer_calls": 0,
        "pinterest_calls": 0,
        "oauth_calls": 0,
        "ai_calls": 0,
        "publishing_admission": "NOT_GRANTED",
        "preflight_fingerprint": None,
        "batch_id": None,
        "batch_manifest_sha256": None,
        "candidate_count": 0,
        "entries": [],
    }


def _receipt_from_preflight(report):
    required = (
        "database_revision",
        "month_start",
        "current_date",
        "plan_id",
        "plan_fingerprint",
        "candidates",
        "preflight_fingerprint",
    )
    if (
        not isinstance(report, dict)
        or report.get("success") is not True
        or report.get("bounded_preflight_certification") != "PASS"
        or report.get("publishing_admission") != "NOT_GRANTED"
        or report.get("candidate_count") != 5
        or not all(key in report for key in required)
    ):
        raise RuntimeError("PREFLIGHT_NOT_CERTIFIED")
    return {
        "contract": "FIVE_PIN_BOUNDED_PREFLIGHT_V1",
        "database_revision": report["database_revision"],
        "month_start": report["month_start"],
        "current_date": report["current_date"],
        "plan_id": report["plan_id"],
        "plan_fingerprint": report["plan_fingerprint"],
        "candidates": report["candidates"],
        "preflight_fingerprint": report["preflight_fingerprint"],
    }


def _scoped_settings(settings):
    return settings.model_copy(update={
        "routine_bounded_batch_enabled": True,
        "routine_pinterest_batch_size": 5,
        "routine_pinterest_daily_write_limit": 5,
        "routine_pinterest_dry_run": True,
        "publishing_enabled": False,
        "buffer_publishing_enabled": False,
        "routine_pinterest_worker_enabled": False,
        "routine_buffer_dispatch_enabled": False,
        "routine_scheduled_live_admission_enabled": False,
        "routine_pinterest_scheduler_enabled": False,
        "routine_scheduled_autonomy_enabled": False,
        "pinterest_write_scope_enabled": False,
        "pinterest_board_write_scope_enabled": False,
        "pinterest_board_provisioning_enabled": False,
        "pinterest_autonomous_board_ensure_enabled": False,
        "pinterest_single_pin_pilot_enabled": False,
        "buffer_single_pin_pilot_enabled": False,
    })



def _invocation_receipt_id(invocation_id: str) -> str:
    return str(uuid5(INVOCATION_NAMESPACE, invocation_id))


def _claim_invocation(invocation_id: str | None, *, session_factory=None) -> str:
    if not isinstance(invocation_id, str) or not invocation_id:
        raise RuntimeError("PREPARATION_INVOCATION_ID_REQUIRED")
    if not INVOCATION_RE.fullmatch(invocation_id):
        raise RuntimeError("PREPARATION_INVOCATION_ID_INVALID")
    if session_factory is None:
        from app.db.session import SessionLocal
        session_factory = SessionLocal

    row_id = _invocation_receipt_id(invocation_id)
    db = session_factory()
    try:
        existing = db.get(AuditLog, row_id)
        if existing is not None:
            raise RuntimeError("PREPARATION_INVOCATION_ALREADY_CLAIMED")
        row = AuditLog(
            id=row_id,
            actor=INVOCATION_ACTOR,
            action=INVOCATION_ACTION,
            entity_type=INVOCATION_ENTITY_TYPE,
            entity_id=invocation_id,
            correlation_id=invocation_id,
            metadata_json={
                "contract": "FIVE_PIN_BOUNDED_PREPARATION_INVOCATION_V1",
                "status": "CLAIMED",
            },
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.get(AuditLog, row_id)
            if existing is not None:
                raise RuntimeError("PREPARATION_INVOCATION_ALREADY_CLAIMED") from None
            raise RuntimeError("PREPARATION_INVOCATION_GUARD_WRITE_FAILED") from None
        return row_id
    finally:
        db.close()


def run(
    *,
    preflight_runner=None,
    settings_factory=None,
    session_factory=None,
    renderer=None,
    invocation_id=None,
    invocation_session_factory=None,
    require_invocation_guard=False,
):
    result = _base_result()
    db = None
    try:
        if require_invocation_guard:
            result["terminal_stage"] = "INVOCATION_GUARD"
            result["invocation_receipt_id"] = _claim_invocation(
                invocation_id,
                session_factory=invocation_session_factory,
            )
            result["invocation_guard_claimed"] = True
            result["invocation_guard_database_writes"] = 1

        if preflight_runner is None:
            from app.state_transfer.bounded_pilot_preflight import run as preflight_runner
        preflight = preflight_runner()
        receipt = _receipt_from_preflight(preflight)
        result["preflight_fingerprint"] = receipt["preflight_fingerprint"]
        result["candidate_count"] = 5

        # Re-check the real process environment immediately before the first
        # mutating DB session. This must still be the exact closed 1/1 state.
        result["terminal_stage"] = "CLOSED_STATE_RECHECK"
        persistent_gates = gate_snapshot()
        result["persistent_gate_fingerprint"] = digest(persistent_gates)

        settings_factory = settings_factory or get_settings
        persistent_settings = settings_factory()
        scoped = _scoped_settings(persistent_settings)
        if (
            persistent_settings.routine_bounded_batch_enabled is not False
            or persistent_settings.routine_pinterest_dry_run is not True
            or persistent_settings.routine_pinterest_batch_size != 1
            or persistent_settings.routine_pinterest_daily_write_limit != 1
            or persistent_settings.publishing_enabled is not False
            or persistent_settings.buffer_publishing_enabled is not False
            or persistent_settings.routine_pinterest_worker_enabled is not False
            or persistent_settings.routine_buffer_dispatch_enabled is not False
            or persistent_settings.routine_scheduled_live_admission_enabled is not False
        ):
            raise RuntimeError("PERSISTENT_CLOSED_STATE_DRIFT")

        result["terminal_stage"] = "PREPARATION"
        result["process_local_bounded_override"] = True
        result["database_mutation_authorized"] = True
        if session_factory is None:
            from app.db.session import SessionLocal
            session_factory = SessionLocal
        db = session_factory()
        prepared = prepare_certified_batch(
            db,
            settings=scoped,
            actor=ACTOR,
            receipt=receipt,
            renderer=renderer,
        )

        # The preparation operator is required to retain provider/live admission
        # at zero/not-granted even though DB/media preparation is mutating.
        if (
            prepared.get("success") is not True
            or prepared.get("status") != "READY"
            or prepared.get("candidate_count") != 5
            or prepared.get("preflight_fingerprint") != receipt["preflight_fingerprint"]
            or prepared.get("provider_calls") != 0
            or prepared.get("buffer_calls") != 0
            or prepared.get("pinterest_calls") != 0
            or prepared.get("oauth_calls") != 0
            or prepared.get("ai_calls") != 0
            or prepared.get("publishing_admission") != "NOT_GRANTED"
        ):
            raise RuntimeError("PREPARATION_RESULT_CONTRACT_DRIFT")

        result.update({
            "success": True,
            "terminal_stage": "COMPLETE",
            "code": "READY",
            "batch_id": prepared["batch_id"],
            "batch_manifest_sha256": prepared["batch_manifest_sha256"],
            "plan_id": prepared["plan_id"],
            "plan_fingerprint": prepared["plan_fingerprint"],
            "entries": prepared["entries"],
            "idempotent": bool(prepared.get("idempotent")),
        })
    except BoundedPreparationOperatorError as exc:
        result["code"] = exc.code
    except Exception as exc:
        detail = str(exc)
        if detail in {
            "PREFLIGHT_NOT_CERTIFIED",
            "PERSISTENT_CLOSED_STATE_DRIFT",
            "PREPARATION_RESULT_CONTRACT_DRIFT",
            "PREPARATION_INVOCATION_ID_REQUIRED",
            "PREPARATION_INVOCATION_ID_INVALID",
            "PREPARATION_INVOCATION_ALREADY_CLAIMED",
            "PREPARATION_INVOCATION_GUARD_WRITE_FAILED",
        }:
            result["code"] = detail
        else:
            result["code"] = "BOUNDED_PREPARATION_UNEXPECTED_ERROR"
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                result.update(
                    success=False,
                    terminal_stage="DATABASE_CLOSE",
                    code="BOUNDED_PREPARATION_DATABASE_CLOSE_FAILED",
                )
    return result


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        if args:
            result = _base_result()
            result.update(terminal_stage="ARGUMENTS", code="ARGUMENTS_PROHIBITED")
        else:
            result = run(
                invocation_id=os.environ.get(INVOCATION_ENV),
                require_invocation_guard=True,
            )
    except Exception:
        result = _base_result()
        result.update(terminal_stage="UNEXPECTED", code="BOUNDED_PREPARATION_UNEXPECTED_ERROR")
    finally:
        logging.disable(previous)
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0 if result["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
