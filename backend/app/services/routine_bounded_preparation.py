"""Autonomous preparation of one frozen five-item batch, never board creation."""
from datetime import timedelta

import sqlalchemy as sa

from app.db.bounded_batch_schema_0034 import batches, entries
from app.models.domain import PinterestPortfolioPlan, PinterestPortfolioPlanItem
from app.services.pinterest_autonomous_execution import execute_autonomous_item
from app.services.pinterest_board_strategy import board_strategy
from app.services.publication_scheduler import request_fingerprint_for
from app.services import routine_bounded_batch as batch


def prepare_batch(db, batch_id, plan_id, *, settings, renderer=None):
    """Use an applied portfolio's ordering; never accept replacement IDs.

    Existing execution services retain SEO/content policy and machine permit
    creation. Internal gates are copied only for this explicitly gated scope.
    """
    batch.require_gate(db, settings)
    control, current = batch._lock(db, batch_id)
    if current["state"] == "READY":
        return batch_id
    if current["state"] != "OPEN":
        raise batch.BoundedBatchError("BOUNDED_BATCH_PREPARATION_NOT_OPEN")
    plan = db.get(PinterestPortfolioPlan, plan_id)
    if not plan or plan.status != "ACTIVE":
        raise batch.BoundedBatchError("BOUNDED_BATCH_ACTIVE_PLAN_REQUIRED")
    now = batch._now(db)
    items = db.scalars(sa.select(PinterestPortfolioPlanItem).where(
        PinterestPortfolioPlanItem.plan_id == plan_id,
        PinterestPortfolioPlanItem.is_reserve.is_(False),
        PinterestPortfolioPlanItem.status == "PLANNED",
        PinterestPortfolioPlanItem.publication_id.is_(None),
        PinterestPortfolioPlanItem.planned_date >= now.date(),
    ).order_by(
        PinterestPortfolioPlanItem.planned_date,
        PinterestPortfolioPlanItem.slot_index,
        PinterestPortfolioPlanItem.id,
    ).limit(5).with_for_update()).all()
    if len(items) != 5:
        raise batch.BoundedBatchError("BOUNDED_BATCH_EXACTLY_FIVE_REQUIRED")
    frozen = []
    for slot, item in enumerate(items):
        route = board_strategy(db, canonical_key=item.board_key_snapshot, settings=settings)
        if route.get("status") != "ROUTE_EXISTING":
            raise batch.BoundedBatchError("BOUNDED_BATCH_EXISTING_BOARD_REQUIRED")
        frozen.append(dict(
            batch_id=batch_id, slot=slot, item_id=item.id, product_id=item.product_id,
            board_id=route["selected_board_id"],
            external_board_id=route["selected_external_board_id"],
            item_fingerprint=item.item_fingerprint,
        ))
    # Candidate/product/route identities cannot subsequently be replaced.
    db.execute(batches.update().where(batches.c.id == batch_id).values(
        state="PREPARING", lease_until=now + timedelta(seconds=120),
    ))
    db.execute(entries.insert(), frozen)
    db.commit()
    internal = settings.model_copy(update={
        "pinterest_seo_brief_persistence_enabled": True,
        "pinterest_autonomous_generation_enabled": True,
        "routine_autonomous_authorization_enabled": True,
        "pinterest_autonomous_execution_enabled": True,
        "publishing_enabled": False, "buffer_publishing_enabled": False,
        "routine_pinterest_worker_enabled": False, "routine_buffer_dispatch_enabled": False,
        "pinterest_write_scope_enabled": False,
    })
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