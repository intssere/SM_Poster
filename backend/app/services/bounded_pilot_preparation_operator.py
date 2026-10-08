"""Preflight-bound operator service for provider-free five-Pin batch preparation."""
from __future__ import annotations

from uuid import UUID, uuid5

import sqlalchemy as sa

from app.db.bounded_batch_schema_0034 import batches
from app.models.domain import PinPublication, PinterestPortfolioPlan, PinterestPortfolioPlanItem, PublicationStatus
from app.models.routine_publishing import RoutinePublishingControl
from app.services import routine_bounded_batch as batch
from app.services import routine_bounded_preparation as preparation
from app.services.pinterest_board_strategy import board_strategy


BATCH_NAMESPACE = UUID("0e8b1fe9-6c55-4d96-9d80-ec17c60722fb")
PREPARATION_CONTRACT = "FIVE_PIN_BOUNDED_PREPARATION_V1"
NONTERMINAL = ("OPEN", "PREPARING", "READY", "RUNNING")
_HEX = set("0123456789abcdef")


class BoundedPreparationOperatorError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _raise(code: str):
    raise BoundedPreparationOperatorError(code)


def _sanitized_preparation_code(exc: Exception) -> str:
    class_name = exc.__class__.__name__
    if class_name == "AutonomousExecutionError":
        value = getattr(exc, "code", None)
    elif class_name in {
        "AutonomousGenerationError",
        "PinterestSeoError",
        "CreativeRenderError",
    }:
        value = str(exc).strip()
    else:
        return "BOUNDED_PREPARATION_INTERNAL_ERROR"
    if not isinstance(value, str) or not value:
        return "BOUNDED_PREPARATION_INTERNAL_ERROR"
    if (
        not value
        or len(value) > 120
        or any(not (ch.isupper() or ch.isdigit() or ch == "_") for ch in value)
    ):
        return "BOUNDED_PREPARATION_INTERNAL_ERROR"
    if value.startswith("BOUNDED_"):
        return value
    return f"BOUNDED_PREPARATION_{value}"[:120]


def _hex64(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


def _validate_receipt_shape(receipt: dict) -> None:
    required = {
        "contract", "database_revision", "month_start", "current_date",
        "plan_id", "plan_fingerprint", "candidates", "preflight_fingerprint",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        _raise("BOUNDED_PREPARATION_PREFLIGHT_RECEIPT_INVALID")
    if (
        receipt["contract"] != preparation.PREFLIGHT_CONTRACT
        or receipt["database_revision"] != "0034"
        or not isinstance(receipt["plan_id"], str)
        or not receipt["plan_id"]
        or not _hex64(receipt["plan_fingerprint"])
        or not _hex64(receipt["preflight_fingerprint"])
        or not isinstance(receipt["candidates"], list)
        or len(receipt["candidates"]) != 5
    ):
        _raise("BOUNDED_PREPARATION_PREFLIGHT_RECEIPT_INVALID")
    payload = {key: receipt[key] for key in (
        "contract", "database_revision", "month_start", "current_date",
        "plan_id", "plan_fingerprint", "candidates",
    )}
    if preparation._digest(payload) != receipt["preflight_fingerprint"]:
        _raise("BOUNDED_PREPARATION_PREFLIGHT_FINGERPRINT_MISMATCH")
    candidate_fields = {
        "item_id", "item_fingerprint", "candidate_fingerprint", "product_id",
        "local_board_id", "pinterest_board_record_id", "external_board_id",
        "content_angle_id", "planned_date", "slot_index",
        "candidate_identity_fingerprint",
    }
    identities = set()
    for candidate in receipt["candidates"]:
        if not isinstance(candidate, dict) or set(candidate) != candidate_fields:
            _raise("BOUNDED_PREPARATION_CANDIDATE_RECEIPT_INVALID")
        if (
            not all(isinstance(candidate[key], str) and candidate[key] for key in (
                "item_id", "product_id", "local_board_id", "pinterest_board_record_id",
                "external_board_id", "content_angle_id", "planned_date",
            ))
            or not all(_hex64(candidate[key]) for key in (
                "item_fingerprint", "candidate_fingerprint",
                "candidate_identity_fingerprint",
            ))
            or isinstance(candidate["slot_index"], bool)
            or not isinstance(candidate["slot_index"], int)
            or candidate["slot_index"] < 0
        ):
            _raise("BOUNDED_PREPARATION_CANDIDATE_RECEIPT_INVALID")
        raw = {key: candidate[key] for key in candidate_fields
               if key != "candidate_identity_fingerprint"}
        if preparation._digest(raw) != candidate["candidate_identity_fingerprint"]:
            _raise("BOUNDED_PREPARATION_CANDIDATE_FINGERPRINT_MISMATCH")
        identity = (
            candidate["item_id"], candidate["product_id"],
            candidate["pinterest_board_record_id"], candidate["external_board_id"],
        )
        if identity in identities:
            _raise("BOUNDED_PREPARATION_DUPLICATE_CANDIDATE")
        identities.add(identity)


def batch_id_for(preflight_fingerprint: str) -> str:
    if not _hex64(preflight_fingerprint):
        _raise("BOUNDED_PREPARATION_PREFLIGHT_FINGERPRINT_INVALID")
    return str(uuid5(BATCH_NAMESPACE, preflight_fingerprint))


def _require_closed_preparation_state(db, settings) -> None:
    batch.require_gate(db, settings)
    if (
        settings.publishing_enabled is not False
        or settings.buffer_publishing_enabled is not False
        or settings.routine_pinterest_worker_enabled is not False
        or settings.routine_buffer_dispatch_enabled is not False
        or settings.routine_scheduled_live_admission_enabled is not False
        or settings.routine_pinterest_dry_run is not True
    ):
        _raise("BOUNDED_PREPARATION_CLOSED_STATE_REQUIRED")
    control = db.get(RoutinePublishingControl, "default")
    if control is None or control.state != "PAUSED":
        _raise("BOUNDED_PREPARATION_CONTROL_PAUSED_REQUIRED")
    unknown = db.scalar(sa.select(sa.func.count()).select_from(PinPublication).where(
        PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN
    ))
    if int(unknown or 0) != 0:
        _raise("BOUNDED_PREPARATION_PUBLISH_UNKNOWN_PRESENT")


def _verify_ready_batch(db, batch_id: str, receipt: dict, *, settings) -> dict:
    current = db.execute(sa.select(batches).where(
        batches.c.id == batch_id
    )).mappings().one_or_none()
    if current is None or current["state"] != "READY":
        _raise("BOUNDED_PREPARATION_IDEMPOTENT_BATCH_NOT_READY")
    now = batch._now(db)
    if (
        receipt["current_date"] != now.date().isoformat()
        or receipt["month_start"] != now.date().replace(day=1).isoformat()
    ):
        _raise("BOUNDED_PREPARATION_PREFLIGHT_RECEIPT_STALE")
    plan = db.get(PinterestPortfolioPlan, receipt["plan_id"])
    if (
        plan is None
        or plan.status != "ACTIVE"
        or plan.plan_fingerprint != receipt["plan_fingerprint"]
    ):
        _raise("BOUNDED_PREPARATION_PLAN_DRIFT")
    rows = batch._rows(db, batch_id)
    if (
        len(rows) != 5
        or [row["slot"] for row in rows] != list(range(5))
        or current["manifest_sha256"] != batch._hash(rows)
        or int(current["attempts_reserved"]) != 0
        or current["admission_closed"] is not False
    ):
        _raise("BOUNDED_PREPARATION_READY_MANIFEST_DRIFT")

    entries = []
    for index, (row, expected) in enumerate(zip(rows, receipt["candidates"])):
        item = db.get(PinterestPortfolioPlanItem, row["item_id"])
        if item is None or item.plan_id != plan.id:
            _raise("BOUNDED_PREPARATION_READY_ITEM_DRIFT")
        route = board_strategy(db, canonical_key=item.board_key_snapshot, settings=settings)
        if route.get("status") != "ROUTE_EXISTING":
            _raise("BOUNDED_PREPARATION_READY_ROUTE_DRIFT")
        identity = preparation._candidate_identity(item, route)
        if identity != expected:
            _raise("BOUNDED_PREPARATION_READY_RECEIPT_DRIFT")
        if (
            row["slot"] != index
            or row["item_id"] != expected["item_id"]
            or row["product_id"] != expected["product_id"]
            or row["board_id"] != expected["pinterest_board_record_id"]
            or row["external_board_id"] != expected["external_board_id"]
            or row["item_fingerprint"] != expected["item_fingerprint"]
        ):
            _raise("BOUNDED_PREPARATION_READY_MANIFEST_DRIFT")
        publication, permit = batch.validate_identity(db, row)
        if publication.status != PublicationStatus.SCHEDULED:
            _raise("BOUNDED_PREPARATION_READY_PUBLICATION_DRIFT")
        entries.append({
            "slot": index,
            "item_id": row["item_id"],
            "product_id": row["product_id"],
            "pinterest_board_record_id": row["board_id"],
            "external_board_id": row["external_board_id"],
            "item_fingerprint": row["item_fingerprint"],
            "publication_id": publication.id,
            "permit_id": permit.id,
            "publication_fingerprint": row["publication_fingerprint"],
            "request_fingerprint": row["request_fingerprint"],
        })
    return {
        "success": True,
        "contract": PREPARATION_CONTRACT,
        "status": "READY",
        "batch_id": batch_id,
        "batch_manifest_sha256": current["manifest_sha256"],
        "preflight_fingerprint": receipt["preflight_fingerprint"],
        "plan_id": receipt["plan_id"],
        "plan_fingerprint": receipt["plan_fingerprint"],
        "candidate_count": 5,
        "entries": entries,
        "provider_calls": 0,
        "buffer_calls": 0,
        "pinterest_calls": 0,
        "oauth_calls": 0,
        "ai_calls": 0,
        "publishing_admission": "NOT_GRANTED",
    }


def prepare_certified_batch(db, *, settings, actor: str, receipt: dict, renderer=None) -> dict:
    """Create/prepare exactly one deterministic batch bound to one preflight receipt."""
    if not isinstance(actor, str) or not actor:
        _raise("BOUNDED_PREPARATION_ACTOR_REQUIRED")
    _validate_receipt_shape(receipt)
    _require_closed_preparation_state(db, settings)
    identity = batch_id_for(receipt["preflight_fingerprint"])

    conflict = db.scalar(sa.select(batches.c.id).where(
        batches.c.state.in_(NONTERMINAL),
        batches.c.id != identity,
    ).limit(1))
    if conflict is not None:
        _raise("BOUNDED_PREPARATION_CONFLICTING_BATCH")

    current = db.execute(sa.select(batches).where(
        batches.c.id == identity
    )).mappings().one_or_none()
    if current is not None:
        if current["state"] == "READY":
            result = _verify_ready_batch(db, identity, receipt, settings=settings)
            return {**result, "idempotent": True}
        if current["state"] != "OPEN":
            _raise("BOUNDED_PREPARATION_EXISTING_BATCH_NOT_RETRYABLE")

    # This is deliberately before create_batch(): stale/altered receipts leave no
    # empty batch row behind. prepare_batch() repeats the same verification under
    # its own locked selection immediately before freezing the manifest.
    try:
        preparation.validate_preflight_receipt(
            db,
            receipt["plan_id"],
            settings=settings,
            expected_preflight=receipt,
        )
    except batch.BoundedBatchError as exc:
        db.rollback()
        raise BoundedPreparationOperatorError(str(exc)) from None

    if current is None:
        batch.create_batch(db, settings=settings, batch_id=identity)

    try:
        preparation.prepare_batch(
            db,
            identity,
            receipt["plan_id"],
            settings=settings,
            renderer=renderer,
            expected_preflight=receipt,
        )
    except batch.BoundedBatchError as exc:
        db.rollback()
        raise BoundedPreparationOperatorError(str(exc)) from None
    except Exception as exc:
        db.rollback()
        raise BoundedPreparationOperatorError(
            _sanitized_preparation_code(exc)
        ) from None

    result = _verify_ready_batch(db, identity, receipt, settings=settings)
    return {**result, "idempotent": False}
