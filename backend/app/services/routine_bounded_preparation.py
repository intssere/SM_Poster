"""Autonomous preparation of one frozen five-item batch, never board creation."""
from datetime import timedelta
import hashlib
import json

import sqlalchemy as sa

from app.db.bounded_batch_schema_0034 import batches, entries
from app.models.domain import PinterestPortfolioPlan, PinterestPortfolioPlanItem
from app.services.pinterest_autonomous_execution import execution_readiness, execute_autonomous_item
from app.services.pinterest_board_strategy import board_strategy
from app.services.publication_scheduler import request_fingerprint_for
from app.services import routine_bounded_batch as batch


PREFLIGHT_CONTRACT = "FIVE_PIN_BOUNDED_PREFLIGHT_V1"


def _execution_settings(settings):
    """Exact process-local settings used for readiness and preparation execution."""
    return settings.model_copy(update={
        "pinterest_seo_brief_persistence_enabled": True,
        "pinterest_autonomous_generation_enabled": True,
        "routine_autonomous_authorization_enabled": True,
        "pinterest_autonomous_execution_enabled": True,
        "publishing_enabled": False,
        "buffer_publishing_enabled": False,
        "routine_pinterest_worker_enabled": False,
        "routine_buffer_dispatch_enabled": False,
        "pinterest_write_scope_enabled": False,
    })


def _execution_ready_items(db, plan_id, *, settings, now, limit, lock=False):
    """Return the first ordered items that actual autonomous readiness would admit."""
    statement = sa.select(PinterestPortfolioPlanItem).where(
        PinterestPortfolioPlanItem.plan_id == plan_id,
        PinterestPortfolioPlanItem.is_reserve.is_(False),
        PinterestPortfolioPlanItem.status == "PLANNED",
        PinterestPortfolioPlanItem.publication_id.is_(None),
        PinterestPortfolioPlanItem.planned_date >= now.date(),
    ).order_by(
        PinterestPortfolioPlanItem.planned_date,
        PinterestPortfolioPlanItem.slot_index,
        PinterestPortfolioPlanItem.id,
    )
    if lock:
        statement = statement.with_for_update()
    internal = _execution_settings(settings)
    ready = []
    for item in db.scalars(statement).all():
        readiness = execution_readiness(db, item.id, settings=internal, now=now)
        if readiness.get("ready") is True:
            ready.append(item)
            if len(ready) >= limit:
                break
    return ready


def _digest(payload):
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def _candidate_identity(item, route):
    metadata = item.selection_metadata or {}
    candidate_fingerprint = metadata.get("candidate_fingerprint")
    if (
        not isinstance(candidate_fingerprint, str)
        or len(candidate_fingerprint) != 64
        or any(ch not in "0123456789abcdef" for ch in candidate_fingerprint)
    ):
        raise batch.BoundedBatchError("BOUNDED_BATCH_CANDIDATE_FINGERPRINT_REQUIRED")
    identity = {
        "item_id": item.id,
        "item_fingerprint": item.item_fingerprint,
        "candidate_fingerprint": candidate_fingerprint,
        "product_id": item.product_id,
        "local_board_id": item.local_board_id,
        "pinterest_board_record_id": route["selected_board_id"],
        "external_board_id": route["selected_external_board_id"],
        "content_angle_id": item.content_angle_id,
        "planned_date": item.planned_date.isoformat(),
        "slot_index": int(item.slot_index),
    }
    identity["candidate_identity_fingerprint"] = _digest(identity)
    return identity


def _verify_expected_preflight(plan, items, routes, now, expected):
    if expected is None:
        return
    required = {
        "contract", "database_revision", "month_start", "current_date",
        "plan_id", "plan_fingerprint", "candidates", "preflight_fingerprint",
    }
    if not isinstance(expected, dict) or set(expected) != required:
        raise batch.BoundedBatchError("BOUNDED_BATCH_PREFLIGHT_RECEIPT_INVALID")
    current_date = now.date().isoformat()
    month_start = now.date().replace(day=1).isoformat()
    candidates = [_candidate_identity(item, route) for item, route in zip(items, routes)]
    payload = {
        "contract": PREFLIGHT_CONTRACT,
        "database_revision": "0034",
        "month_start": month_start,
        "current_date": current_date,
        "plan_id": plan.id,
        "plan_fingerprint": plan.plan_fingerprint,
        "candidates": candidates,
    }
    if (
        expected["contract"] != PREFLIGHT_CONTRACT
        or expected["database_revision"] != "0034"
        or expected["month_start"] != month_start
        or expected["current_date"] != current_date
        or expected["plan_id"] != plan.id
        or expected["plan_fingerprint"] != plan.plan_fingerprint
        or expected["candidates"] != candidates
        or expected["preflight_fingerprint"] != _digest(payload)
    ):
        raise batch.BoundedBatchError("BOUNDED_BATCH_PREFLIGHT_RECEIPT_MISMATCH")


def validate_preflight_receipt(db, plan_id, *, settings, expected_preflight):
    """Recompute the exact five candidates without writing any preparation state."""
    plan = db.get(PinterestPortfolioPlan, plan_id)
    if not plan or plan.status != "ACTIVE":
        raise batch.BoundedBatchError("BOUNDED_BATCH_ACTIVE_PLAN_REQUIRED")
    now = batch._now(db)
    items = _execution_ready_items(
        db, plan_id, settings=settings, now=now, limit=5, lock=True,
    )
    if len(items) != 5:
        raise batch.BoundedBatchError("BOUNDED_BATCH_EXACTLY_FIVE_REQUIRED")
    routes = []
    for item in items:
        route = board_strategy(db, canonical_key=item.board_key_snapshot, settings=settings)
        if route.get("status") != "ROUTE_EXISTING":
            raise batch.BoundedBatchError("BOUNDED_BATCH_EXISTING_BOARD_REQUIRED")
        routes.append(route)
    _verify_expected_preflight(plan, items, routes, now, expected_preflight)
    return plan, now, items, routes


def prepare_batch(db, batch_id, plan_id, *, settings, renderer=None, expected_preflight=None):
    """Use an applied portfolio's ordering; never accept replacement IDs.

    Existing execution services retain SEO/content policy and machine permit
    creation. Internal gates are copied only for this explicitly gated scope.
    """
    batch.require_gate(db, settings)
    control, current = batch._lock(db, batch_id)
    if current["state"] == "READY":
        if expected_preflight is not None:
            raise batch.BoundedBatchError("BOUNDED_BATCH_ALREADY_READY")
        return batch_id
    if current["state"] != "OPEN":
        raise batch.BoundedBatchError("BOUNDED_BATCH_PREPARATION_NOT_OPEN")
    if expected_preflight is not None:
        plan, now, items, routes = validate_preflight_receipt(
            db, plan_id, settings=settings, expected_preflight=expected_preflight,
        )
    else:
        plan = db.get(PinterestPortfolioPlan, plan_id)
        if not plan or plan.status != "ACTIVE":
            raise batch.BoundedBatchError("BOUNDED_BATCH_ACTIVE_PLAN_REQUIRED")
        now = batch._now(db)
        items = _execution_ready_items(
            db, plan_id, settings=settings, now=now, limit=5, lock=True,
        )
        if len(items) != 5:
            raise batch.BoundedBatchError("BOUNDED_BATCH_EXACTLY_FIVE_REQUIRED")
        routes = []
        for item in items:
            route = board_strategy(db, canonical_key=item.board_key_snapshot, settings=settings)
            if route.get("status") != "ROUTE_EXISTING":
                raise batch.BoundedBatchError("BOUNDED_BATCH_EXISTING_BOARD_REQUIRED")
            routes.append(route)
    frozen = []
    for slot, (item, route) in enumerate(zip(items, routes)):
        frozen.append(dict(
            batch_id=batch_id, slot=slot, item_id=item.id, product_id=item.product_id,
            board_id=route["selected_board_id"],
            external_board_id=route["selected_external_board_id"],
            item_fingerprint=item.item_fingerprint,
        ))
    _verify_expected_preflight(plan, items, routes, now, expected_preflight)
    # Candidate/product/route identities cannot subsequently be replaced.
    db.execute(batches.update().where(batches.c.id == batch_id).values(
        state="PREPARING", lease_until=now + timedelta(seconds=120),
    ))
    db.execute(entries.insert(), frozen)
    db.commit()
    internal = _execution_settings(settings)
    try:
        for candidate in frozen:
            _, current = batch._lock(db, batch_id)
            if current["state"] != "PREPARING":
                raise batch.BoundedBatchError("BOUNDED_BATCH_PREPARATION_CLOSED")
            db.commit()
            execution = execute_autonomous_item(
                db, candidate["item_id"], settings=internal, renderer=renderer,
            )
            publication = db.get(batch.PinPublication, execution.publication_id)
            if execution.status != "SUCCEEDED" or execution.stage != "PERMITTED" or publication is None:
                raise batch.BoundedBatchError("BOUNDED_BATCH_PREPARATION_FAILED")
            batch._lock(db, batch_id)
            db.execute(entries.update().where(
                entries.c.batch_id == batch_id, entries.c.slot == candidate["slot"],
            ).values(
                publication_id=publication.id, permit_id=execution.routine_permit_id,
                publication_fingerprint=publication.publication_fingerprint,
                request_fingerprint=request_fingerprint_for(publication),
            ))
            batch.validate_identity(db, {**candidate,
                "publication_id": publication.id, "permit_id": execution.routine_permit_id,
                "publication_fingerprint": publication.publication_fingerprint,
                "request_fingerprint": request_fingerprint_for(publication),
            })
            db.commit()
        batch.seal_batch(db, batch_id, settings=settings)
    except Exception:
        db.rollback()
        control, _ = batch._lock(db, batch_id)
        batch._close(db, batch_id, "FAILED", "BOUNDED_BATCH_PREPARATION_FAILED", control)
        db.commit()
        raise
    return batch_id