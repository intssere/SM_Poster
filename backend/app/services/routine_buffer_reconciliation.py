from __future__ import annotations

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.integrations.buffer.gateway import BufferGateway
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
from app.models.routine_publishing import RoutineAttemptBoundary
from app.services.buffer_publication_reconciliation import BufferReconciliationError, reconcile_buffer


async def run_reconciliation_once(db, *, settings: Settings | None = None, gateway: BufferGateway | None = None, limit=25):
    settings = settings or get_settings()
    if limit < 1 or limit > 25:
        raise ValueError("limit must be between 1 and 25")
    rows = db.execute(select(PinPublication, PublicationAttempt).join(
        PublicationAttempt, PublicationAttempt.publication_id == PinPublication.id
    ).join(
        RoutineAttemptBoundary, RoutineAttemptBoundary.attempt_id == PublicationAttempt.id
    ).where(
        PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN,
        PublicationAttempt.status == "UNKNOWN",
        PublicationAttempt.dispatch_provider == "buffer",
        PublicationAttempt.provider_operation_id.is_not(None),
    ).order_by(PublicationAttempt.provider_last_observed_at, PublicationAttempt.id).limit(limit)).all()
    checked = reconciled = unresolved = 0
    for publication, attempt in rows:
        checked += 1
        try:
            result = await reconcile_buffer(
                db, publication.id, actor="routine-reconciler", settings=settings, gateway=gateway,
            )
            if result.status in {PublicationStatus.PUBLISHED, PublicationStatus.PUBLISH_FAILED}:
                reconciled += 1
            else:
                unresolved += 1
        except BufferReconciliationError:
            db.rollback()
            unresolved += 1
    return {"checked": checked, "reconciled": reconciled, "unresolved": unresolved, "dispatched": 0}
