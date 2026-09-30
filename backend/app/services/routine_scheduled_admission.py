"""PostgreSQL-only scheduled admission inside the caller's claim transaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.core.config import Settings
from app.models.domain import (
    PinPublication,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
    Product,
    PublicationStatus,
)
from app.models.routine_publishing import RoutinePublishingControl
from app.services.pinterest_publisher import normalize_persisted_utc
from app.services.routine_scheduled_commitments import assess_scheduled_quota_with_commitments
from app.services.routine_scheduled_quotas import (
    ScheduledQuotaError,
    reserve_scheduled_quota,
    scheduled_quota_limits,
)
from app.services.local_canary_admission import publication_has_pending_local_canary_media

DIMENSIONS = ("daily", "monthly", "product", "vendor", "board")


@dataclass(frozen=True)
class ScheduledAdmission:
    """Claim and reservation flushed in the caller's transaction, not committed."""

    publication_id: str
    plan_item_id: str
    reservation_id: str
    already_reserved: bool
    used: tuple[int, int, int, int, int]


def admit_scheduled_publication(
    db,
    *,
    publication_id: str,
    plan_item_id: str,
    settings: Settings,
    now: datetime | None = None,
) -> ScheduledAdmission:
    """Serialize reconciliation, reservation, and the publication claim CAS.

    The caller must commit this transaction together with the publication CAS
    and any permit/attempt writes, or roll it all back. DRY_RUN callers must
    always roll back their savepoint; this function never commits or dispatches.
    """
    if db.get_bind().dialect.name != "postgresql":
        raise ScheduledQuotaError("SCHEDULED_QUOTA_DATABASE_UNSUPPORTED")
    now = normalize_persisted_utc(now or datetime.now(timezone.utc))

    # All cooperating admission workers take this singleton row lock *before*
    # reading the canonical commitments. No pre-lock quota observation is used.
    control_id = db.scalar(
        select(RoutinePublishingControl.id)
        .where(RoutinePublishingControl.id == "default")
        .with_for_update()
    )
    if control_id is None:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_CONTROL_ROW_MISSING")
    if publication_has_pending_local_canary_media(db, publication_id):
        raise ScheduledQuotaError("LOCAL_CANARY_MEDIA_PENDING")
    item = PinterestPortfolioPlanItem
    plan = PinterestPortfolioPlan
    pub = PinPublication
    product = Product
    with db.no_autoflush:
        row = db.execute(
            select(
                item.id.label("item_id"),
                item.publication_id.label("item_publication_id"),
                item.plan_id.label("plan_id"),
                item.product_id.label("product_id"),
                item.local_board_id.label("board_id"),
                plan.month_start.label("month_start"),
                plan.month_end.label("month_end"),
                plan.target_pins.label("target_pins"),
                product.vendor.label("vendor"),
                pub.status.label("publication_status"),
                pub.scheduled_for.label("publication_scheduled_for"),
            )
            .select_from(item)
            .join(plan, plan.id == item.plan_id)
            .join(product, product.id == item.product_id)
            .join(pub, pub.id == item.publication_id)
            .where(item.id == plan_item_id)
        ).one_or_none()
    if row is None or row.item_publication_id != publication_id:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_COMMITMENT_AMBIGUOUS")
    scheduled = row.publication_scheduled_for
    if (
        row.publication_status != PublicationStatus.SCHEDULED
        or scheduled is None
        or normalize_persisted_utc(scheduled) > now
    ):
        raise ScheduledQuotaError("SCHEDULED_QUOTA_PUBLICATION_NOT_CLAIMABLE")
    day = normalize_persisted_utc(scheduled).date()
    limits = scheduled_quota_limits(row, settings, day)
    identity = dict(
        publication_id=publication_id,
        plan_id=row.plan_id,
        plan_item_id=row.item_id,
        product_id=row.product_id,
        vendor_key=row.vendor,
        board_id=row.board_id,
        scheduled_for=day,
        limits=limits,
    )
    before = assess_scheduled_quota_with_commitments(db, **identity)
    if not before.already_committed:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_COMMITMENT_STALE")
    used = tuple(getattr(before, f"{name}_used") for name in DIMENSIONS)
    # If the CAS or postcondition fails, the nested transaction removes the
    # reservation even if a caller catches the error and commits its outer work.
    with db.begin_nested():
        reservation = reserve_scheduled_quota(db, **identity)
        claimed = db.execute(
            update(PinPublication).execution_options(synchronize_session=False)
            .where(
                PinPublication.id == publication_id,
                PinPublication.status == PublicationStatus.SCHEDULED,
                PinPublication.scheduled_for.is_not(None),
                PinPublication.scheduled_for <= now,
            )
            .values(status=PublicationStatus.PUBLISHING, attempt_started_at=now)
        )
        if claimed.rowcount != 1:
            raise ScheduledQuotaError("SCHEDULED_QUOTA_CLAIM_LOST")
        after = assess_scheduled_quota_with_commitments(db, **identity)
        if not after.already_reserved or tuple(
            getattr(after, f"{name}_used") for name in DIMENSIONS
        ) != used:
            raise ScheduledQuotaError("SCHEDULED_QUOTA_ADMISSION_INCONSISTENT")
    return ScheduledAdmission(
        publication_id=publication_id,
        plan_item_id=plan_item_id,
        reservation_id=reservation.id,
        already_reserved=before.already_reserved,
        used=used,
    )