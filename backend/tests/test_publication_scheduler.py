from datetime import datetime, timezone, timedelta
import pytest
from app.models.domain import PublicationStatus
from app.models.domain import PinPublication
from test_pin_proposals import setup_service
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

def test_db_backed_due_claim_creates_single_started_attempt():
    db, _, _ = setup_service()
    p = PinPublication(draft_id="d", creative_id="c", publication_fingerprint="x"*64, status=PublicationStatus.SCHEDULED, scheduled_for=datetime.now(timezone.utc)-timedelta(minutes=1), title_snapshot="t", description_snapshot="d", destination_url="https://example.test", media_url_snapshot="https://cdn.example.test/a.png")
    db.add(p); db.commit(); db.refresh(p)
    from app.services.publication_scheduler import claim, request_fingerprint_for
    attempt = claim(db, p)
    assert attempt and attempt.status == "STARTED"
    db.refresh(p)
    assert p.status == PublicationStatus.PUBLISHING
    assert attempt.request_fingerprint == request_fingerprint_for(p)
    assert claim(db, p) is None
    db.close()
