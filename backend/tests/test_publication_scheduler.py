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

def test_request_fingerprint_binds_utm_url_as_provider_link():
    from app.services.publication_scheduler import request_fingerprint_for
    publication = PinPublication(
        draft_id="fingerprint-draft",
        creative_id="fingerprint-creative",
        publication_fingerprint="f" * 64,
        pinterest_board_id_snapshot="board",
        title_snapshot="Title",
        description_snapshot="Description",
        alt_text_snapshot="Alt",
        destination_url="https://diamondshelf.us/products/item",
        utm_url="https://diamondshelf.us/products/item?utm_source=pinterest",
        media_url_snapshot="https://cdn.shopify.com/image.jpg",
    )
    original = request_fingerprint_for(publication)
    publication.destination_url = "https://diamondshelf.us/products/item?ignored=1"
    assert request_fingerprint_for(publication) == original
    publication.utm_url = "https://diamondshelf.us/products/item?utm_source=pinterest&utm_campaign=changed"
    assert request_fingerprint_for(publication) != original

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

def test_publish_failed_explicit_reschedule_then_claim_creates_attempt_two():
    from app.services.publication_scheduler import claim, request_fingerprint_for, schedule

    db, _, _ = setup_service()
    publication = PinPublication(
        draft_id="retry-draft",
        creative_id="retry-creative",
        publication_fingerprint="r" * 64,
        status=PublicationStatus.PUBLISH_FAILED,
        title_snapshot="Retry title",
        description_snapshot="Retry description",
        destination_url="https://example.test/retry",
        media_url_snapshot="https://cdn.example.test/retry.png",
    )
    db.add(publication); db.commit(); db.refresh(publication)
    attempt_1 = PublicationAttempt(
        publication_id=publication.id,
        attempt_number=1,
        status="FAILED",
        request_fingerprint=request_fingerprint_for(publication),
        error_code="PROVIDER_REJECTED",
        safe_response_metadata={},
    )
    db.add(attempt_1); db.commit(); db.refresh(attempt_1)
    initial_attempts = db.query(PublicationAttempt).filter_by(publication_id=publication.id).order_by(PublicationAttempt.attempt_number).all()
    assert publication.status == PublicationStatus.PUBLISH_FAILED
    assert [(item.attempt_number, item.status) for item in initial_attempts] == [(1, "FAILED")]

    retry_time_1 = datetime(2020, 1, 15, 11, 50, 0, tzinfo=timezone.utc)
    schedule(db, publication, retry_time_1)
    attempts_after_first_schedule = db.query(PublicationAttempt).filter_by(publication_id=publication.id).order_by(PublicationAttempt.attempt_number).all()
    assert publication.status == PublicationStatus.SCHEDULED
    assert publication.scheduled_for == retry_time_1.astimezone(timezone.utc)
    assert [(item.attempt_number, item.status) for item in attempts_after_first_schedule] == [(1, "FAILED")]

    retry_time_2 = datetime(2020, 1, 15, 11, 55, 0, tzinfo=timezone.utc)
    schedule(db, publication, retry_time_2)
    attempts_before_claim = db.query(PublicationAttempt).filter_by(publication_id=publication.id).order_by(PublicationAttempt.attempt_number).all()
    assert publication.status == PublicationStatus.SCHEDULED
    assert publication.scheduled_for == retry_time_2.astimezone(timezone.utc)
    assert len(attempts_before_claim) == 1
    assert attempts_before_claim[0].attempt_number == 1
    assert attempts_before_claim[0].status == "FAILED" and attempts_before_claim[0].error_code == "PROVIDER_REJECTED"

    attempt_2 = claim(db, publication)
    assert attempt_2 is not None
    assert attempt_2.attempt_number == 2 and attempt_2.status == "STARTED"
    assert attempt_2.publication_id == publication.id
    assert attempt_2.request_fingerprint and attempt_2.request_fingerprint == request_fingerprint_for(publication)
    db.refresh(publication)
    assert publication.status == PublicationStatus.PUBLISHING
    final_attempts = db.query(PublicationAttempt).filter_by(publication_id=publication.id).order_by(PublicationAttempt.attempt_number).all()
    assert [(item.attempt_number, item.status) for item in final_attempts] == [(1, "FAILED"), (2, "STARTED")]
    assert final_attempts[0].error_code == "PROVIDER_REJECTED"
    assert all(item.attempt_number != 3 for item in final_attempts)
    db.close()

def test_cancel_approved_and_scheduled_publications_is_terminal_and_creates_no_attempts():
    from app.services.publication_scheduler import cancel, claim, schedule

    db, _, _ = setup_service()
    now = datetime(2030, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    approved_publication = PinPublication(
        id="cancellation-approved",
        draft_id="cancel-approved-draft",
        creative_id="cancel-approved-creative",
        publication_fingerprint="u" * 64,
        status=PublicationStatus.APPROVED,
        scheduled_for=None,
        title_snapshot="Approved cancellation",
        description_snapshot="Approved cancellation description",
        destination_url="https://example.test/cancel-approved",
        media_url_snapshot="https://cdn.example.test/cancel-approved.png",
    )
    scheduled_publication = PinPublication(
        id="cancellation-scheduled",
        draft_id="cancel-scheduled-draft",
        creative_id="cancel-scheduled-creative",
        publication_fingerprint="v" * 64,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=now - timedelta(minutes=5),
        title_snapshot="Scheduled cancellation",
        description_snapshot="Scheduled cancellation description",
        destination_url="https://example.test/cancel-scheduled",
        media_url_snapshot="https://cdn.example.test/cancel-scheduled.png",
    )
    db.add_all([approved_publication, scheduled_publication]); db.commit()
    publication_ids = {approved_publication.id, scheduled_publication.id}
    pre_cancel_due_ids = {publication.id for publication in due_publications(db, now=now, limit=25)}
    assert scheduled_publication.id in pre_cancel_due_ids
    assert approved_publication.id not in pre_cancel_due_ids
    assert db.query(PublicationAttempt).filter(PublicationAttempt.publication_id.in_(publication_ids)).count() == 0

    approved_result = cancel(db, approved_publication)
    db.refresh(approved_publication)
    assert approved_result is approved_publication
    assert approved_publication.status == PublicationStatus.CANCELLED and approved_publication.scheduled_for is None

    scheduled_result = cancel(db, scheduled_publication)
    db.refresh(scheduled_publication)
    assert scheduled_result is scheduled_publication
    assert scheduled_publication.status == PublicationStatus.CANCELLED and scheduled_publication.scheduled_for is None

    post_cancel_due_ids = {publication.id for publication in due_publications(db, now=now, limit=25)}
    assert scheduled_publication.id not in post_cancel_due_ids
    assert approved_publication.id not in post_cancel_due_ids
    assert claim(db, scheduled_publication) is None
    assert claim(db, approved_publication) is None
    assert db.query(PublicationAttempt).filter(PublicationAttempt.publication_id.in_(publication_ids)).count() == 0

    future_time = now + timedelta(hours=1)
    with pytest.raises(ValueError, match="^Invalid publication transition: CANCELLED -> SCHEDULED$"):
        schedule(db, scheduled_publication, future_time)
    with pytest.raises(ValueError, match="^Invalid publication transition: CANCELLED -> SCHEDULED$"):
        schedule(db, approved_publication, future_time)
    db.expire_all()
    final_approved = db.get(PinPublication, approved_publication.id)
    final_scheduled = db.get(PinPublication, scheduled_publication.id)
    assert final_approved.status == PublicationStatus.CANCELLED and final_scheduled.status == PublicationStatus.CANCELLED
    assert final_approved.scheduled_for is None and final_scheduled.scheduled_for is None
    assert db.query(PublicationAttempt).filter(PublicationAttempt.publication_id.in_(publication_ids)).count() == 0
    db.close()
