"""Deterministic publication duplicate safety checks for Task #39.

The service is deliberately offline: no provider HTTP, no AI, no fuzzy
similarity.  It evaluates immutable publication dispatch identity only.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain import PinPublication, PublicationStatus

SAFE_TO_CONTINUE = "SAFE_TO_CONTINUE"
ALREADY_PUBLISHED = "ALREADY_PUBLISHED"
UNKNOWN_OUTCOME_BLOCKS_RETRY = "UNKNOWN_OUTCOME_BLOCKS_RETRY"
DUPLICATE_PUBLICATION = "DUPLICATE_PUBLICATION"
POSSIBLE_DUPLICATE_PIN = "POSSIBLE_DUPLICATE_PIN"


def _status_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _match(publication: PinPublication, reason_code: str) -> dict[str, str]:
    return {
        "publication_id": publication.id,
        "status": _status_value(publication.status),
        "reason_code": reason_code,
    }


def _same_operational_identity(left: PinPublication, right: PinPublication) -> bool:
    return (
        left.pinterest_board_id_snapshot == right.pinterest_board_id_snapshot
        and left.utm_url == right.utm_url
        and left.creative_fingerprint == right.creative_fingerprint
        and left.text_fingerprint == right.text_fingerprint
    )


def evaluate_publication_duplicates(db: Session, publication: PinPublication) -> dict[str, Any]:
    """Return a safe, deterministic duplicate/readiness blocker."""
    current_status = _status_value(publication.status)
    if current_status == PublicationStatus.PUBLISHED.value or publication.pinterest_pin_id:
        return {
            "status": ALREADY_PUBLISHED,
            "blocking": True,
            "matches": [_match(publication, ALREADY_PUBLISHED)],
        }
    if current_status == PublicationStatus.PUBLISH_UNKNOWN.value:
        return {
            "status": UNKNOWN_OUTCOME_BLOCKS_RETRY,
            "blocking": True,
            "matches": [_match(publication, UNKNOWN_OUTCOME_BLOCKS_RETRY)],
        }

    others = db.scalars(select(PinPublication).where(PinPublication.id != publication.id)).all()
    for other in others:
        if other.publication_fingerprint and other.publication_fingerprint == publication.publication_fingerprint:
            return {
                "status": DUPLICATE_PUBLICATION,
                "blocking": True,
                "matches": [_match(other, DUPLICATE_PUBLICATION)],
            }

    for other in others:
        status = _status_value(other.status)
        if status == PublicationStatus.PUBLISH_UNKNOWN.value and _same_operational_identity(publication, other):
            return {
                "status": UNKNOWN_OUTCOME_BLOCKS_RETRY,
                "blocking": True,
                "matches": [_match(other, UNKNOWN_OUTCOME_BLOCKS_RETRY)],
            }

    for other in others:
        status = _status_value(other.status)
        if status != PublicationStatus.PUBLISHED.value and not other.pinterest_pin_id:
            continue
        same_board_link_creative = (
            other.pinterest_board_id_snapshot == publication.pinterest_board_id_snapshot
            and other.utm_url == publication.utm_url
            and other.creative_fingerprint == publication.creative_fingerprint
        )
        if same_board_link_creative and other.text_fingerprint == publication.text_fingerprint:
            return {
                "status": DUPLICATE_PUBLICATION,
                "blocking": True,
                "matches": [_match(other, DUPLICATE_PUBLICATION)],
            }
        if same_board_link_creative:
            return {
                "status": POSSIBLE_DUPLICATE_PIN,
                "blocking": True,
                "matches": [_match(other, POSSIBLE_DUPLICATE_PIN)],
            }

    return {"status": SAFE_TO_CONTINUE, "blocking": False, "matches": []}
