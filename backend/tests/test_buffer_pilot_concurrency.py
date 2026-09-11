"""Concurrent control-plane races against an isolated, file-backed database."""
import threading
from datetime import datetime, timezone

from sqlalchemy import func, select, text

from app.models.domain import BufferPilotActivation
from app.services import buffer_pilot_activation as activation_service
from app.services.buffer_pilot_activation import BufferPilotActivationError, consume, revoke
from test_buffer_pilot_control_plane import _eligible
from test_manual_publication_dispatch import _db


NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def test_simultaneous_arm_attempts_have_one_persisted_winner(tmp_path, monkeypatch):
    """Both contenders precheck ACTIVE absence; the partial unique index arbitrates."""
    SessionLocal, engine = _db(tmp_path / "arm-race.db")
    try:
        with SessionLocal() as db:
            publication = _eligible(db, "arm-race")

        prechecks = threading.Barrier(2)
        winner_committed = threading.Event()
        real_active_activation = activation_service.active_activation

        def coordinated_active_activation(db, publication_id=None):
            result = real_active_activation(db, publication_id)
            prechecks.wait(timeout=10)
            # End the read transaction for the delayed contender.  This is
            # SQLite-specific coordination; the production unique index and
            # IntegrityError handling remain the authority for the race.
            if threading.current_thread().name == "loser":
                db.rollback()
                winner_committed.wait(timeout=10)
            return result

        monkeypatch.setattr(activation_service, "active_activation", coordinated_active_activation)
        outcomes = {}

        def arm(actor):
            with SessionLocal() as db:
                try:
                    activation_service.activate(
                        db, publication_id=publication.id, actor=actor, now=NOW
                    )
                    outcomes[actor] = "won"
                except BufferPilotActivationError as error:
                    outcomes[actor] = str(error)
                finally:
                    if actor == "winner":
                        winner_committed.set()

        threads = [
            threading.Thread(target=arm, args=("winner",), name="winner"),
            threading.Thread(target=arm, args=("loser",), name="loser"),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive()

        assert outcomes == {"winner": "won", "loser": "ACTIVE_ACTIVATION_EXISTS"}
        with SessionLocal() as audit:
            activations = audit.scalars(select(BufferPilotActivation)).all()
            assert len(activations) == 1
            assert activations[0].status == "ACTIVE"
            assert activations[0].actor == "winner"
            assert audit.scalar(
                select(func.count()).select_from(BufferPilotActivation).where(
                    BufferPilotActivation.status == "ACTIVE"
                )
            ) == 1
    finally:
        engine.dispose()


def test_simultaneous_consume_and_revoke_have_one_cas_winner(tmp_path):
    """Stale ACTIVE contenders race their public CAS operations, never both win."""
    SessionLocal, engine = _db(tmp_path / "consume-revoke-race.db")
    try:
        with SessionLocal() as db:
            publication = _eligible(db, "consume-revoke-race")
            activation = activation_service.activate(
                db, publication_id=publication.id, actor="arm-operator", now=NOW
            )
            activation_id = activation.id
            publication_id = publication.id

        # WAL plus a busy timeout lets SQLite serialize the two UPDATEs rather
        # than turning the loser into a database-lock error.
        with engine.connect() as connection:
            connection.execute(text("PRAGMA journal_mode=WAL"))
            connection.execute(text("PRAGMA busy_timeout=10000"))

        loaded = threading.Barrier(2)
        start_cas = threading.Barrier(2)
        outcomes = {}
        consumed_at = datetime(2026, 9, 6, 12, 1, tzinfo=timezone.utc)
        revoked_at = datetime(2026, 9, 6, 12, 2, tzinfo=timezone.utc)

        def contend(kind):
            with SessionLocal() as db:
                activation = db.get(BufferPilotActivation, activation_id)
                publication = db.get(
                    __import__("app.models.domain", fromlist=["PinPublication"]).PinPublication,
                    publication_id,
                )
                assert activation.status == "ACTIVE"
                loaded.wait(timeout=10)
                # Finish the shared initial-state read before the CAS race.
                db.rollback()
                start_cas.wait(timeout=10)
                activation = db.get(BufferPilotActivation, activation_id)
                publication = db.get(type(publication), publication_id)
                if kind == "consume":
                    outcomes[kind] = consume(
                        db, activation, publication, now=consumed_at
                    )
                    db.commit()
                else:
                    try:
                        outcomes[kind] = revoke(
                            db, activation, actor="revoker", reason="race", now=revoked_at
                        ) is not None
                    except BufferPilotActivationError as error:
                        outcomes[kind] = str(error)

        threads = [
            threading.Thread(target=contend, args=("consume",)),
            threading.Thread(target=contend, args=("revoke",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive()

        assert sorted(value is True for value in outcomes.values()) == [False, True]
        with SessionLocal() as audit:
            final = audit.get(BufferPilotActivation, activation_id)
            assert final.status in {"CONSUMED", "REVOKED"}
            if final.status == "CONSUMED":
                assert final.consumed_at.replace(tzinfo=timezone.utc) == consumed_at
                assert final.revoked_at is None
                assert final.revoked_by is None
                assert final.actor == "arm-operator"
            else:
                assert final.revoked_at.replace(tzinfo=timezone.utc) == revoked_at
                assert final.revoked_by == "revoker"
                assert final.revoke_reason == "race"
                assert final.consumed_at is None
    finally:
        engine.dispose()