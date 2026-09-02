from datetime import datetime, timezone, timedelta
import pytest
from app.models.domain import PublicationStatus
from app.services.publication_scheduler import due_publications

def test_due_limit_validation():
    for limit in (0, -1, 26):
        with pytest.raises(ValueError): due_publications(None, limit=limit)

def test_naive_schedule_is_rejected():
    from app.services.publication_scheduler import schedule
    class P: status = __import__('app.models.domain', fromlist=['PublicationStatus']).PublicationStatus.APPROVED
    with pytest.raises(ValueError): schedule(None, P(), datetime.now())

@pytest.mark.parametrize("status", [PublicationStatus.PUBLISH_UNKNOWN, PublicationStatus.PUBLISHED, PublicationStatus.CANCELLED])
def test_terminal_publications_cannot_be_rescheduled(status):
    from app.services.publication_scheduler import schedule
    class P: pass
    p=P(); p.status=status
    with pytest.raises(ValueError): schedule(None, p, datetime.now(timezone.utc))

def test_aware_schedule_is_normalized_to_utc():
    from app.services.publication_scheduler import schedule
    class DB:
        def commit(self): pass
    class P:
        status=PublicationStatus.APPROVED
        scheduled_for=None
    p=P(); schedule(DB(), p, datetime(2030,1,1,12,tzinfo=timezone(timedelta(hours=3))))
    assert p.scheduled_for.tzinfo == timezone.utc
    assert p.scheduled_for.hour == 9
