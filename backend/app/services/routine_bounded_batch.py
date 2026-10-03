"""Explicit, PostgreSQL-only five-pin admission. No provider I/O or hot gates.

Lock order is always routine singleton -> batch -> publication/permit. Every
claim consumes one lifetime slot in the existing quota/permit claim transaction.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from app.db.bounded_batch_schema_0034 import batches, entries
from app.models.domain import (
    PinApproval, PinConcept, PinCreative, PinDraft, PinPublication, PinterestBoard, PinterestConnection,
    PinterestPortfolioPlanItem, PublicationAttempt, PublicationStatus,
)
from app.models.routine_publishing import RoutineDispatchPermit, RoutinePublishingControl
from app.services.publication_scheduler import request_fingerprint_for
from app.services.routine_autonomous_authorization import AUTONOMOUS_ACTOR

IDENTITY = (
    "slot", "item_id", "product_id", "board_id", "external_board_id",
    "item_fingerprint", "publication_id", "permit_id",
    "publication_fingerprint", "request_fingerprint",
)
TERMINAL = {"PAUSED", "FAILED", "COMPLETED"}


class BoundedBatchError(RuntimeError):
    pass


def _now(db):
    return db.scalar(sa.select(sa.func.clock_timestamp()))


def require_gate(db, settings):
    if db.get_bind().dialect.name != "postgresql":
        raise BoundedBatchError("BOUNDED_BATCH_POSTGRES_REQUIRED")
    if not settings.routine_bounded_batch_enabled:
        raise BoundedBatchError("BOUNDED_BATCH_DISABLED")
    if (settings.routine_pinterest_scheduler_enabled
            or settings.routine_scheduled_autonomy_enabled):
        raise BoundedBatchError("BOUNDED_BATCH_RECURRING_OR_LEGACY_MODE_FORBIDDEN")
    if (settings.pinterest_board_provisioning_enabled
            or settings.pinterest_board_write_scope_enabled
            or settings.pinterest_autonomous_board_ensure_enabled):
        raise BoundedBatchError("BOUNDED_BATCH_BOARD_CREATION_FORBIDDEN")
    if (settings.pinterest_write_scope_enabled or settings.pinterest_single_pin_pilot_enabled
            or settings.buffer_single_pin_pilot_enabled):
        raise BoundedBatchError("BOUNDED_BATCH_UNRELATED_WRITE_MODE_FORBIDDEN")
    if settings.routine_pinterest_batch_size != 5 or settings.routine_pinterest_daily_write_limit != 5:
        raise BoundedBatchError("BOUNDED_BATCH_LIMITS_MUST_BE_FIVE")


def _lock(db, batch_id):
    control = db.scalar(sa.select(RoutinePublishingControl).where(
        RoutinePublishingControl.id == "default"
    ).with_for_update().execution_options(populate_existing=True))
    if control is None:
        raise BoundedBatchError("BOUNDED_BATCH_CONTROL_REQUIRED")
    batch = db.execute(sa.select(batches).where(
        batches.c.id == batch_id
    ).with_for_update()).mappings().one_or_none()
    if batch is None:
        raise BoundedBatchError("BOUNDED_BATCH_NOT_FOUND")
    return control, dict(batch)


def _rows(db, batch_id):
    return [dict(r) for r in db.execute(sa.select(entries).where(
        entries.c.batch_id == batch_id
    ).order_by(entries.c.slot)).mappings()]


def _hash(rows):
    return hashlib.sha256(json.dumps(
        [{k: r[k] for k in IDENTITY} for r in rows],
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _close(db, batch_id, state, reason, control):
    db.execute(batches.update().where(batches.c.id == batch_id).values(
        state=state, admission_closed=True, reason=reason, owner=None, lease_until=None,
    ))
    control.state = "PAUSED"
    control.pause_reason = reason
    control.paused_at = _now(db)
    control.paused_by = "bounded-batch"
    unused = sa.select(entries.c.permit_id).where(
        entries.c.batch_id == batch_id, entries.c.reserved_at.is_(None),
    )
    db.execute(sa.update(RoutineDispatchPermit).where(
        RoutineDispatchPermit.id.in_(unused), RoutineDispatchPermit.status == "ACTIVE",
    ).values(status="REVOKED", revoked_at=_now(db), revoked_by="bounded-batch", revoke_reason=reason))
    # Preparation may have committed an execution just before a crash and not
    # yet bound its returned permit. Frozen candidate lineage still owns it.
    candidate_publications = sa.select(PinterestPortfolioPlanItem.publication_id).join(
        entries, entries.c.item_id == PinterestPortfolioPlanItem.id,
    ).where(entries.c.batch_id == batch_id, entries.c.reserved_at.is_(None))
    db.execute(sa.update(RoutineDispatchPermit).where(
        RoutineDispatchPermit.publication_id.in_(candidate_publications),
        RoutineDispatchPermit.authorized_by == AUTONOMOUS_ACTOR,
        RoutineDispatchPermit.status == "ACTIVE",
    ).values(status="REVOKED", revoked_at=_now(db), revoked_by="bounded-batch", revoke_reason=reason))


def close_batch(db, batch_id, *, reason="BOUNDED_BATCH_OPERATOR_PAUSE"):
    control, batch = _lock(db, batch_id)
    if batch["state"] not in TERMINAL:
        _close(db, batch_id, "PAUSED", reason, control)
    db.commit()


def create_batch(db, *, settings, batch_id=None):
    require_gate(db, settings)
    batch_id = batch_id or str(uuid4())
    existing = db.execute(sa.select(batches).where(batches.c.id == batch_id)).mappings().one_or_none()
    if existing is None:
        db.execute(insert(batches).values(id=batch_id).on_conflict_do_nothing(index_elements=["id"]))
        db.commit()
    return batch_id


def validate_identity(db, row, *, consumed=False):
    item = db.get(PinterestPortfolioPlanItem, row["item_id"])
    publication = db.get(PinPublication, row["publication_id"])
    permit = db.get(RoutineDispatchPermit, row["permit_id"])
    board = db.get(PinterestBoard, row["board_id"])
    connection = db.get(PinterestConnection, board.connection_id) if board else None
    approval = db.get(PinApproval, publication.approval_id) if publication else None
    draft = db.get(PinDraft, publication.draft_id) if publication else None
    concept = db.get(PinConcept, draft.concept_id) if draft else None
    creative = db.get(PinCreative, publication.creative_id) if publication else None
    for entity in (item, publication, permit, board, connection, approval, draft, concept, creative):
        if entity is not None:
            db.refresh(entity)
    if (not all((item, publication, permit, board, connection, approval, draft, concept, creative))
            or item.product_id != row["product_id"]
            or concept.product_id != row["product_id"]
            or concept.board_id != item.local_board_id
            or concept.content_angle_id != item.content_angle_id
            or creative.draft_id != publication.draft_id
            or item.item_fingerprint != row["item_fingerprint"]
            or item.publication_id != publication.id
            or publication.pinterest_board_record_id != board.id
            or publication.pinterest_board_id_snapshot != row["external_board_id"]
            or board.external_board_id != row["external_board_id"]
            or publication.pinterest_connection_id != connection.id
            or not board.is_active or not board.is_eligible
            or connection.status != "CONNECTED"
            or publication.publication_fingerprint != row["publication_fingerprint"]
            or request_fingerprint_for(publication) != row["request_fingerprint"]
            or permit.publication_id != publication.id
            or permit.pinterest_board_record_id != board.id
            or permit.approval_id != publication.approval_id
            or permit.publication_fingerprint != row["publication_fingerprint"]
            or permit.request_fingerprint != row["request_fingerprint"]
            or permit.dispatch_provider != "buffer"
            or permit.authorized_by != AUTONOMOUS_ACTOR
            or approval.decided_by != AUTONOMOUS_ACTOR
            or approval.decision != "APPROVED"
            or approval.draft_id != publication.draft_id
            or approval.creative_id != publication.creative_id
            or permit.status != ("CONSUMED" if consumed else "ACTIVE")):
        raise BoundedBatchError("BOUNDED_BATCH_MANIFEST_IDENTITY_MISMATCH")
    return publication, permit


def seal_batch(db, batch_id, *, settings):
    require_gate(db, settings)
    _, batch = _lock(db, batch_id)
    rows = _rows(db, batch_id)
    if batch["state"] == "READY" and batch["manifest_sha256"] == _hash(rows):
        return
    if batch["state"] != "PREPARING" or len(rows) != 5 or [r["slot"] for r in rows] != list(range(5)):
        raise BoundedBatchError("BOUNDED_BATCH_EXACTLY_FIVE_REQUIRED")
    for row in rows:
        pub, _ = validate_identity(db, row)
        if pub.status != PublicationStatus.SCHEDULED or db.scalar(sa.select(
                PublicationAttempt.id).where(PublicationAttempt.publication_id == pub.id).limit(1)):
            raise BoundedBatchError("BOUNDED_BATCH_UNATTEMPTED_PUBLICATION_REQUIRED")
        active = db.scalars(sa.select(RoutineDispatchPermit.id).where(
            RoutineDispatchPermit.publication_id == pub.id, RoutineDispatchPermit.status == "ACTIVE",
        )).all()
        if active != [row["permit_id"]]:
            raise BoundedBatchError("BOUNDED_BATCH_EXACT_PERMIT_REQUIRED")
    db.execute(batches.update().where(batches.c.id == batch_id).values(
        state="READY", manifest_sha256=_hash(rows), admission_closed=False,
    ))
    db.commit()


def _observe_locked(db, batch, control):
    rows = _rows(db, batch["id"])
    if batch["manifest_sha256"] and (
            len(rows) != 5 or _hash(rows) != batch["manifest_sha256"]
            or sum(r["reserved_at"] is not None for r in rows) != batch["attempts_reserved"]):
        _close(db, batch["id"], "FAILED", "BOUNDED_BATCH_LEDGER_DRIFT", control)
        return "FAILED"
    outcomes = []
    for row in rows:
        if row["reserved_at"] is None:
            continue
        pub = db.get(PinPublication, row["publication_id"])
        if pub:
            db.refresh(pub)
        status = pub.status if pub else None
        outcome = {
            PublicationStatus.PUBLISHED: "PUBLISHED",
            PublicationStatus.PUBLISH_FAILED: "FAILED",
            PublicationStatus.PUBLISH_UNKNOWN: "UNKNOWN",
        }.get(status)
        if outcome:
            db.execute(entries.update().where(
                entries.c.batch_id == batch["id"], entries.c.slot == row["slot"],
            ).values(outcome=outcome))
        outcomes.append(outcome)
    if "UNKNOWN" in outcomes:
        _close(db, batch["id"], "PAUSED", "PUBLISH_UNKNOWN_CIRCUIT_BREAKER", control)
        return "PAUSED"
    if "FAILED" in outcomes:
        _close(db, batch["id"], "FAILED", "BOUNDED_BATCH_ATTEMPT_FAILED", control)
        return "FAILED"
    if len(outcomes) == 5 and outcomes == ["PUBLISHED"] * 5:
        _close(db, batch["id"], "COMPLETED", "BOUNDED_BATCH_FIVE_RECONCILED", control)
        return "COMPLETED"
    return batch["state"]


def observe_batch(db, batch_id):
    control, batch = _lock(db, batch_id)
    state = _observe_locked(db, batch, control)
    db.commit()
    return state


def acquire_batch(db, batch_id, *, settings, lease_seconds=120):
    require_gate(db, settings)
    if not 1 <= lease_seconds <= 900:
        raise BoundedBatchError("BOUNDED_BATCH_INVALID_LEASE")
    control, batch = _lock(db, batch_id)
    state = _observe_locked(db, batch, control)
    now = _now(db)
    if state in TERMINAL or batch["admission_closed"]:
        db.commit()
        raise BoundedBatchError("BOUNDED_BATCH_CLOSED")
    if batch["state"] not in {"READY", "RUNNING"}:
        raise BoundedBatchError("BOUNDED_BATCH_NOT_READY")
    if batch["owner"] and batch["lease_until"] and batch["lease_until"] > now:
        raise BoundedBatchError("BOUNDED_BATCH_LEASE_HELD")
    if any(r["reserved_at"] and r["outcome"] != "PUBLISHED" for r in _rows(db, batch_id)):
        _close(db, batch_id, "PAUSED", "BOUNDED_BATCH_INTERRUPTED_ATTEMPT", control)
        db.commit()
        raise BoundedBatchError("BOUNDED_BATCH_INTERRUPTED_ATTEMPT")
    owner = str(uuid4())
    db.execute(batches.update().where(batches.c.id == batch_id).values(
        state="RUNNING", owner=owner, lease_until=now + timedelta(seconds=lease_seconds),
    ))
    db.commit()
    return owner


def manifest_candidates(db, batch_id, owner, *, settings):
    require_gate(db, settings)
    control, batch = _lock(db, batch_id)
    state = _observe_locked(db, batch, control)
    if state in TERMINAL:
        db.commit()
        raise BoundedBatchError("BOUNDED_BATCH_CLOSED")
    if batch["owner"] != owner or not batch["lease_until"] or batch["lease_until"] <= _now(db):
        _close(db, batch_id, "PAUSED", "BOUNDED_BATCH_LEASE_LOST", control)
        db.commit()
        raise BoundedBatchError("BOUNDED_BATCH_LEASE_LOST")
    result = []
    try:
        for row in _rows(db, batch_id):
            if row["reserved_at"] is not None:
                continue
            pub, _ = validate_identity(db, row)
            if pub.status != PublicationStatus.SCHEDULED:
                raise BoundedBatchError("BOUNDED_BATCH_UNRESERVED_STATUS_DRIFT")
            scheduled_for = pub.scheduled_for
            if scheduled_for and scheduled_for <= _now(db):
                result.append(pub)
    except BoundedBatchError:
        _close(db, batch_id, "FAILED", "BOUNDED_BATCH_MANIFEST_IDENTITY_MISMATCH", control)
        db.commit()
        raise
    db.commit()
    return result


async def run_bounded_batch_once(db, batch_id, *, settings, gateway=None, media_client=None, resolver=None):
    """Explicit service entry point, not a recurring scheduler or one-shot bypass."""
    from app.services.routine_pinterest_worker import run_once
    owner = acquire_batch(db, batch_id, settings=settings)
    try:
        result = await run_once(
            db, settings=settings, gateway=gateway, media_client=media_client, resolver=resolver,
            bounded_batch_id=batch_id, bounded_owner=owner,
        )
        state = observe_batch(db, batch_id)
        if state not in TERMINAL:
            control, _ = _lock(db, batch_id)
            unresolved = any(r["reserved_at"] and r["outcome"] != "PUBLISHED"
                             for r in _rows(db, batch_id))
            if (result["status"] != "SUCCEEDED" or result.get("mode") != "LIVE"
                    or control.state != "LIVE" or unresolved):
                _close(db, batch_id, "PAUSED", "BOUNDED_BATCH_INCOMPLETE", control)
                state = "PAUSED"
            else:
                # A normal, fully reconciled tick may wait for the other
                # frozen schedule times. Release ownership, never the budget
                # or manifest; a later explicit tick resumes this same batch.
                db.execute(batches.update().where(
                    batches.c.id == batch_id, batches.c.owner == owner,
                ).values(owner=None, lease_until=None))
            db.commit()
        return {**result, "batch_id": batch_id, "batch_state": state}
    except Exception:
        db.rollback()
        close_batch(db, batch_id, reason="BOUNDED_BATCH_EXECUTION_EXCEPTION")
        raise


def member_batch(db, publication_id):
    """Protect frozen members even when the feature is subsequently disabled."""
    if db.get_bind().dialect.name != "postgresql":
        return None
    translated = db.get_bind().get_execution_options().get("schema_translate_map", {}).get(None)
    name = f"{translated}.routine_autonomous_batch_entries" if translated else "routine_autonomous_batch_entries"
    if not db.scalar(sa.text("SELECT to_regclass(:name)"), {"name": name}):
        return None
    candidate_items = sa.select(PinterestPortfolioPlanItem.id).where(
        PinterestPortfolioPlanItem.publication_id == publication_id,
    )
    return db.scalar(sa.select(entries.c.batch_id).where(sa.or_(
        entries.c.publication_id == publication_id,
        entries.c.item_id.in_(candidate_items),
    )))


def reserve_entry(db, batch_id, owner, publication_id, permit_id, *, settings):
    """Caller commits with scheduled-quota CAS, consumed permit and attempt."""
    require_gate(db, settings)
    control, batch = _lock(db, batch_id)
    state = _observe_locked(db, batch, control)
    if state in TERMINAL:
        db.commit()  # retain fail-closed evidence before raising
        raise BoundedBatchError("BOUNDED_BATCH_CLOSED")
    if (batch["state"] != "RUNNING" or batch["admission_closed"]
            or batch["attempts_reserved"] >= 5 or control.state != "LIVE"):
        raise BoundedBatchError("BOUNDED_BATCH_ADMISSION_CLOSED")
    if batch["owner"] != owner or not batch["lease_until"] or batch["lease_until"] <= _now(db):
        _close(db, batch_id, "PAUSED", "BOUNDED_BATCH_LEASE_LOST", control)
        db.commit()
        raise BoundedBatchError("BOUNDED_BATCH_LEASE_LOST")
    rows = _rows(db, batch_id)
    if any(r["reserved_at"] and r["outcome"] != "PUBLISHED" for r in rows):
        raise BoundedBatchError("BOUNDED_BATCH_ATTEMPT_PENDING")
    row = next((r for r in rows if r["publication_id"] == publication_id and r["permit_id"] == permit_id), None)
    if row is None or row["reserved_at"] is not None:
        _close(db, batch_id, "FAILED", "BOUNDED_BATCH_MANIFEST_MISMATCH", control)
        db.commit()
        raise BoundedBatchError("BOUNDED_BATCH_MANIFEST_MISMATCH")
    try:
        validate_identity(db, row)
    except BoundedBatchError:
        _close(db, batch_id, "FAILED", "BOUNDED_BATCH_MANIFEST_IDENTITY_MISMATCH", control)
        db.commit()
        raise
    db.execute(entries.update().where(
        entries.c.batch_id == batch_id, entries.c.slot == row["slot"],
    ).values(reserved_at=_now(db)))
    used = batch["attempts_reserved"] + 1
    db.execute(batches.update().where(batches.c.id == batch_id).values(
        attempts_reserved=used, admission_closed=used == 5,
        lease_until=_now(db) + timedelta(seconds=120),
    ))


def bind_attempt(db, batch_id, publication_id, attempt_id):
    db.execute(entries.update().where(
        entries.c.batch_id == batch_id, entries.c.publication_id == publication_id,
        entries.c.reserved_at.is_not(None), entries.c.attempt_id.is_(None),
    ).values(attempt_id=attempt_id))


def check_provider_boundary(db, batch_id, owner, publication_id, attempt_id):
    """Called under singleton lock immediately before committed mutation boundary."""
    control, batch = _lock(db, batch_id)
    row = db.execute(sa.select(entries).where(
        entries.c.batch_id == batch_id, entries.c.publication_id == publication_id,
        entries.c.attempt_id == attempt_id, entries.c.reserved_at.is_not(None),
    )).mappings().one_or_none()
    if (not row or batch["state"] != "RUNNING" or batch["owner"] != owner
            or not batch["lease_until"] or batch["lease_until"] <= _now(db) or control.state != "LIVE"):
        raise BoundedBatchError("BOUNDED_BATCH_PROVIDER_BOUNDARY_DENIED")
    validate_identity(db, row, consumed=True)