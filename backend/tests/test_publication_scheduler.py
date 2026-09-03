from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
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

def test_file_backed_independent_sessions_allow_only_one_claim(tmp_path):
    from app.services.publication_scheduler import claim, request_fingerprint_for

    database_path = tmp_path / "scheduler-cas.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    seed = SessionLocal()
    publication = PinPublication(
        draft_id="scheduler-cas-draft",
        creative_id="scheduler-cas-creative",
        publication_fingerprint="c" * 64,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1),
        title_snapshot="CAS title",
        description_snapshot="CAS description",
        destination_url="https://example.test/cas",
        media_url_snapshot="https://cdn.example.test/cas.png",
    )
    seed.add(publication); seed.commit()
    publication_id = publication.id
    seed.close()
    session_a = SessionLocal(); session_b = SessionLocal(); session_c = None
    try:
        publication_a = session_a.get(PinPublication, publication_id)
        publication_b = session_b.get(PinPublication, publication_id)
        assert session_a is not session_b and publication_a is not publication_b
        assert publication_a.status == PublicationStatus.SCHEDULED
        assert publication_b.status == PublicationStatus.SCHEDULED
        session_b.commit()
        assert publication_b.status == PublicationStatus.SCHEDULED

        attempt_a = claim(session_a, publication_a)
        assert attempt_a is not None
        assert attempt_a.attempt_number == 1 and attempt_a.status == "STARTED"
        assert attempt_a.request_fingerprint
        session_a.refresh(publication_a)
        assert attempt_a.request_fingerprint == request_fingerprint_for(publication_a)
        assert publication_a.status == PublicationStatus.PUBLISHING

        assert publication_b.status == PublicationStatus.SCHEDULED
        attempt_b = claim(session_b, publication_b)
        assert attempt_b is None
        session_b.rollback()

        session_c = SessionLocal()
        assert session_a is not session_c and session_b is not session_c
        publication_c = session_c.get(PinPublication, publication_id)
        attempts = session_c.query(PublicationAttempt).filter_by(publication_id=publication_id).order_by(PublicationAttempt.attempt_number).all()
        assert publication_c.status == PublicationStatus.PUBLISHING
        assert len(attempts) == 1
        assert attempts[0].publication_id == publication_id
        assert attempts[0].attempt_number == 1 and attempts[0].status == "STARTED"
        assert attempts[0].request_fingerprint
        assert attempts[0].request_fingerprint == request_fingerprint_for(publication_c)
    finally:
        session_a.close(); session_b.close()
        if session_c is not None:
            session_c.close()
        engine.dispose()

def test_due_publications_filters_orders_and_limits_deterministically():
    db, _, _ = setup_service()
    now = datetime(2030, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    cases = {
        "A": (PublicationStatus.SCHEDULED, now - timedelta(minutes=10), "a"),
        "B": (PublicationStatus.SCHEDULED, now - timedelta(minutes=5), "b"),
        "C": (PublicationStatus.SCHEDULED, now - timedelta(minutes=5), "c"),
        "D": (PublicationStatus.SCHEDULED, now, "d"),
        "E": (PublicationStatus.SCHEDULED, now + timedelta(minutes=1), "e"),
        "F": (PublicationStatus.PUBLISHING, now - timedelta(minutes=20), "f"),
        "G": (PublicationStatus.PUBLISH_FAILED, now - timedelta(minutes=30), "g"),
    }
    publications = {}
    for label, (status, scheduled_for, fingerprint_char) in cases.items():
        publication = PinPublication(
            id=f"due-publication-{label.lower()}",
            draft_id=f"due-draft-{label}",
            creative_id=f"due-creative-{label}",
            publication_fingerprint=fingerprint_char * 64,
            status=status,
            scheduled_for=scheduled_for,
            title_snapshot=f"Due title {label}",
            description_snapshot=f"Due description {label}",
            destination_url=f"https://example.test/due-{label.lower()}",
            media_url_snapshot=f"https://cdn.example.test/due-{label.lower()}.png",
        )
        db.add(publication)
        publications[label] = publication
    db.commit()
    seeded_ids = {publication.id for publication in publications.values()}
    db.expire_all()
    original_times = {publication_id: db.get(PinPublication, publication_id).scheduled_for for publication_id in seeded_ids}

    full = due_publications(db, now=now, limit=25)
    full_ids = [publication.id for publication in full]
    expected_ids = [publications["A"].id, *sorted([publications["B"].id, publications["C"].id]), publications["D"].id]
    assert full_ids == expected_ids
    assert publications["D"].id in full_ids
    assert publications["E"].id not in full_ids
    assert publications["F"].id not in full_ids
    assert publications["G"].id not in full_ids

    limited_ids = [publication.id for publication in due_publications(db, now=now, limit=2)]
    assert len(limited_ids) == 2 and limited_ids == full_ids[:2]

    db.expire_all()
    for label in ("A", "B", "C", "D"):
        persisted = db.get(PinPublication, publications[label].id)
        assert persisted.status == PublicationStatus.SCHEDULED
        assert persisted.scheduled_for == original_times[persisted.id]
    attempts = db.query(PublicationAttempt).filter(PublicationAttempt.publication_id.in_(seeded_ids)).all()
    assert attempts == []
    db.close()
