import pytest
from app.models.domain import PublicationStatus
from app.services.publication_state import can_transition, require_transition


def test_unknown_publish_cannot_blindly_retry():
    assert not can_transition(PublicationStatus.PUBLISH_UNKNOWN, PublicationStatus.PUBLISHING)


def test_normal_publish_path():
    assert can_transition(PublicationStatus.SCHEDULED, PublicationStatus.PUBLISHING)
    assert can_transition(PublicationStatus.PUBLISHING, PublicationStatus.PUBLISHED)


def test_invalid_transition_raises():
    with pytest.raises(ValueError):
        require_transition(PublicationStatus.PUBLISHED, PublicationStatus.SCHEDULED)
