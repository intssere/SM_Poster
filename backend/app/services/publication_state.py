from app.models.domain import PublicationStatus


ALLOWED_TRANSITIONS: dict[PublicationStatus, set[PublicationStatus]] = {
    PublicationStatus.APPROVED: {PublicationStatus.SCHEDULED, PublicationStatus.CANCELLED},
    PublicationStatus.SCHEDULED: {PublicationStatus.PUBLISHING, PublicationStatus.CANCELLED},
    PublicationStatus.PUBLISHING: {
        PublicationStatus.PUBLISHED,
        PublicationStatus.PUBLISH_FAILED,
        PublicationStatus.PUBLISH_UNKNOWN,
    },
    PublicationStatus.PUBLISH_FAILED: {PublicationStatus.SCHEDULED, PublicationStatus.CANCELLED},
    # Failed is only resolved by explicit, verified provider reconciliation.
    PublicationStatus.PUBLISH_UNKNOWN: {PublicationStatus.PUBLISHED, PublicationStatus.PUBLISH_FAILED, PublicationStatus.CANCELLED},
    PublicationStatus.PUBLISHED: set(),
    PublicationStatus.CANCELLED: set(),
}


def can_transition(current: PublicationStatus, target: PublicationStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def require_transition(current: PublicationStatus, target: PublicationStatus) -> None:
    if not can_transition(current, target):
        raise ValueError(f"Invalid publication transition: {current.value} -> {target.value}")
