"""Task #39 authorized manual publication dispatch orchestration."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.integrations.pinterest.gateway import PinterestV5Gateway
from app.models.domain import (
    PinPublication,
    PinterestConnection,
    PublicationAttempt,
    PublicationDispatchAuthorization,
    PublicationStatus,
)
from app.services.publication_dispatch_authorization import (
    DispatchAuthorizationError,
    active_authorization,
    provider_readiness,
    validate_authorization,
)
from app.services.publication_scheduler import request_fingerprint_for
from app.services.pinterest_oauth import decrypt_token
from app.services.pinterest_publisher import publish_once, normalize_persisted_utc


class ManualDispatchError(RuntimeError):
    """Safe bounded Task #39 dispatch error."""


async def dispatch_publication(
    db: Session,
    publication: PinPublication,
    *,
    now: datetime | None = None,
    decrypt: Callable[[str], str] = decrypt_token,
    gateway_factory: Callable[..., Any] = PinterestV5Gateway,
) -> dict[str, Any]:
    """Run one authorized manual dispatch attempt if every gate remains valid."""
    now = normalize_persisted_utc(now or datetime.now(timezone.utc))

    authorization = active_authorization(db, publication.id)
    auth_result = validate_authorization(db, publication, authorization, now=now)
    if not auth_result["valid"]:
        raise ManualDispatchError(auth_result["status"])

    provider = provider_readiness(db, publication)
    if provider["status"] == "PUBLISHING_DISABLED":
        raise ManualDispatchError("PUBLISHING_DISABLED")
    if provider["status"] == "PUBLISHING_SCOPE_REQUIRED":
        raise ManualDispatchError("PUBLISHING_SCOPE_REQUIRED")

    connection = db.get(PinterestConnection, publication.pinterest_connection_id)
    if not connection or not connection.access_token_ciphertext:
        raise ManualDispatchError("TOKEN_DECRYPT_FAILED")
    try:
        token = decrypt(connection.access_token_ciphertext)
        gateway = gateway_factory(access_token=token, publishing_enabled=True)
    except Exception:
        db.rollback()
        raise ManualDispatchError("TOKEN_DECRYPT_FAILED") from None

    attempt = atomic_authorized_claim(db, publication, authorization, now=now)
    if attempt is None:
        raise ManualDispatchError("AUTHORIZED_CLAIM_FAILED")

    db.refresh(publication)
    db.refresh(authorization)
    db.refresh(attempt)
    post_claim = validate_post_claim(db, publication, authorization, attempt, now=now)
    if not post_claim["valid"]:
        attempt.status = "FAILED"
        attempt.error_code = "TASK39_POST_CLAIM_REVALIDATION_FAILED"
        attempt.completed_at = now
        publication.status = PublicationStatus.PUBLISH_FAILED
        publication.error_code = "TASK39_POST_CLAIM_REVALIDATION_FAILED"
        db.commit()
        raise ManualDispatchError("TASK39_POST_CLAIM_REVALIDATION_FAILED")

    return await publish_once(db, publication, gateway, attempt)


def atomic_authorized_claim(
    db: Session,
    publication: PinPublication,
    authorization: PublicationDispatchAuthorization,
    *,
    now: datetime | None = None,
) -> PublicationAttempt | None:
    """Consume authorization, claim publication, and create STARTED attempt once."""
    now = normalize_persisted_utc(now or datetime.now(timezone.utc))
    scheduled_for = normalize_persisted_utc(publication.scheduled_for)
    if not scheduled_for or scheduled_for > now:
        return None
    try:
        pub_result = db.execute(
            update(PinPublication)
            .execution_options(synchronize_session=False)
            .where(
                PinPublication.id == publication.id,
                PinPublication.status == PublicationStatus.SCHEDULED,
                PinPublication.scheduled_for.is_not(None),
                PinPublication.scheduled_for <= now,
            )
            .values(status=PublicationStatus.PUBLISHING, attempt_started_at=now)
        )
        if pub_result.rowcount != 1:
            db.rollback()
            return None
        auth_result = db.execute(
            update(PublicationDispatchAuthorization)
            .execution_options(synchronize_session=False)
            .where(
                PublicationDispatchAuthorization.id == authorization.id,
                PublicationDispatchAuthorization.publication_id == publication.id,
                PublicationDispatchAuthorization.status == "ACTIVE",
                PublicationDispatchAuthorization.publication_fingerprint == publication.publication_fingerprint,
                PublicationDispatchAuthorization.expires_at > now,
            )
            .values(status="CONSUMED", consumed_at=now)
        )
        if auth_result.rowcount != 1:
            db.rollback()
            return None
        attempt_no = (
            db.scalar(
                select(PublicationAttempt.attempt_number)
                .where(PublicationAttempt.publication_id == publication.id)
                .order_by(PublicationAttempt.attempt_number.desc())
                .limit(1)
            )
            or 0
        ) + 1
        claimed_publication = db.get(PinPublication, publication.id)
        attempt = PublicationAttempt(
            publication_id=publication.id,
            attempt_number=attempt_no,
            status="STARTED",
            request_fingerprint=request_fingerprint_for(claimed_publication or publication),
            safe_response_metadata={},
        )
        db.add(attempt)
        db.commit()
        db.refresh(attempt)
        return attempt
    except Exception:
        db.rollback()
        raise


def validate_post_claim(
    db: Session,
    publication: PinPublication,
    authorization: PublicationDispatchAuthorization,
    attempt: PublicationAttempt,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if authorization.status != "CONSUMED" or authorization.consumed_at is None:
        return {"valid": False, "status": "AUTHORIZATION_NOT_CONSUMED"}
    if authorization.publication_id != publication.id:
        return {"valid": False, "status": "AUTHORIZATION_MISMATCH"}
    if authorization.publication_fingerprint != publication.publication_fingerprint:
        return {"valid": False, "status": "AUTHORIZATION_MISMATCH"}
    if attempt.status != "STARTED" or attempt.publication_id != publication.id:
        return {"valid": False, "status": "ATTEMPT_MISMATCH"}
    if attempt.request_fingerprint != request_fingerprint_for(publication):
        return {"valid": False, "status": "ATTEMPT_MISMATCH"}

    # Reuse the exact snapshot validation while tolerating the expected
    # CONSUMED status for the authorization claimed by this transaction.
    previous_auth_status = authorization.status
    previous_publication_status = publication.status
    authorization.status = "ACTIVE"
    publication.status = PublicationStatus.SCHEDULED
    try:
        result = validate_authorization(db, publication, authorization, now=now or datetime.now(timezone.utc))
    finally:
        authorization.status = previous_auth_status
        publication.status = previous_publication_status
    if not result["valid"]:
        return result
    return {"valid": True, "status": "READY"}
