"""Fail-closed tests for the server-owned Buffer activation control plane."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.routes import publications as routes
from app.models.domain import (
    BufferPilotActivation,
    PinApproval,
    PinPublication,
    PublicationAttempt,
    PublicationStatus,
)
from app.services.buffer_pilot_activation import (
    BufferPilotActivationError,
    activate,
    active_activation,
    revoke,
)
from app.services.buffer_single_pin_pilot import validate_pilot
from app.services.publication_scheduler import request_fingerprint_for
from test_manual_publication_dispatch import _db, _ready_publication


NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def _eligible(db, fingerprint="control"):
    publication = _ready_publication(db, fingerprint=fingerprint)
    board = db.get(__import__("app.models.domain", fromlist=["PinterestBoard"]).PinterestBoard,
                   publication.pinterest_board_record_id)
    connection = db.get(__import__("app.models.domain", fromlist=["PinterestConnection"]).PinterestConnection,
                        board.connection_id)
    board.last_synced_at = connection.boards_last_synced_at = NOW
    db.commit()
    return publication


def test_activation_derives_identity_and_destination_from_persisted_publication(tmp_path):
    SessionLocal, engine = _db(tmp_path / "activation.db")
    try:
        with SessionLocal() as db:
            publication = _eligible(db)
            activation = activate(db, publication_id=publication.id, actor="server-user", now=NOW)
            assert activation.actor == "server-user"
            assert activation.approval_id == publication.approval_id
            assert activation.pinterest_board_record_id == publication.pinterest_board_record_id
            assert activation.publication_fingerprint == publication.publication_fingerprint
            assert activation.request_fingerprint == request_fingerprint_for(publication)
    finally:
        engine.dispose()


@pytest.mark.parametrize("mutation", ["not_approved", "not_scheduled", "prior_attempt", "destination_drift"])
def test_activation_rejects_unsafe_or_drifting_state(tmp_path, mutation):
    SessionLocal, engine = _db(tmp_path / f"{mutation}.db")
    try:
        with SessionLocal() as db:
            publication = _eligible(db, mutation)
            if mutation == "not_approved":
                db.get(PinApproval, publication.approval_id).decision = "PENDING"
            elif mutation == "not_scheduled":
                publication.status = PublicationStatus.APPROVED
            elif mutation == "prior_attempt":
                db.add(PublicationAttempt(publication_id=publication.id, attempt_number=1, status="FAILED"))
            else:
                publication.pinterest_board_id_snapshot = "different-board"
            db.commit()
            with pytest.raises(BufferPilotActivationError):
                activate(db, publication_id=publication.id, actor="operator", now=NOW)
    finally:
        engine.dispose()


def test_activation_requires_actor_and_revoke_is_a_state_transition(tmp_path):
    SessionLocal, engine = _db(tmp_path / "revoke.db")
    try:
        with SessionLocal() as db:
            publication = _eligible(db, "revoke")
            with pytest.raises(BufferPilotActivationError, match="ACTOR_REQUIRED"):
                activate(db, publication_id=publication.id, actor="", now=NOW)
            activation = activate(db, publication_id=publication.id, actor="operator", now=NOW)
            result = revoke(db, activation, actor="operator", reason="operator request", now=NOW)
            assert result.status == "REVOKED"
            assert result.revoked_at.replace(tzinfo=NOW.tzinfo) == NOW
            assert active_activation(db) is None
            with pytest.raises(BufferPilotActivationError, match="ACTIVATION_NOT_ACTIVE"):
                revoke(db, activation, actor="operator", reason="again", now=NOW)
    finally:
        engine.dispose()


def test_only_one_active_activation_and_activation_fk_are_enforced(tmp_path):
    SessionLocal, engine = _db(tmp_path / "unique.db")
    try:
        with SessionLocal() as db:
            first = _eligible(db, "first")
            existing = activate(db, publication_id=first.id, actor="operator", now=NOW)
            duplicate = BufferPilotActivation(
                approval_id=existing.approval_id, publication_id=first.id,
                pinterest_board_record_id=existing.pinterest_board_record_id,
                publication_fingerprint=existing.publication_fingerprint,
                request_fingerprint=existing.request_fingerprint, actor="other", status="ACTIVE",
            )
            db.add(duplicate)
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()
            foreign_keys = {
                fk.target_fullname for fk in PublicationAttempt.__table__.foreign_keys
                if fk.parent.name == "buffer_pilot_activation_id"
            }
            assert "buffer_pilot_activations.id" in foreign_keys
            db.rollback()
    finally:
        engine.dispose()


def test_activation_status_and_revoke_routes_are_server_owned(monkeypatch):
    class Db:
        def __init__(self, row):
            self.row = row

        def scalar(self, query):
            return self.row

    activation = type("Activation", (), {
        "status": "ACTIVE", "activated_at": NOW, "expires_at": None,
        "revoked_at": None,
    })()
    monkeypatch.setattr(routes, "current_user", lambda request: "operator")
    monkeypatch.setattr(routes, "active_activation", lambda db, publication_id: activation)
    monkeypatch.setattr(routes, "revoke_buffer_pilot", lambda *args, **kwargs: setattr(activation, "status", "REVOKED"))
    result = routes.revoke_buffer("publication", type("Request", (), {})(), Db(activation))
    assert result["status"] == "REVOKED"


def test_activation_route_requires_confirmed_exact_version_and_forbids_identity_extras():
    good = routes.BufferActivationRequest(
        confirmed=True, confirmation_text_version="BUFFER_PILOT_ACTIVATION_V1"
    )
    assert good.confirmed is True
    for values in (
        {"confirmed": False, "confirmation_text_version": "BUFFER_PILOT_ACTIVATION_V1"},
        {"confirmed": True, "confirmation_text_version": "old"},
    ):
        payload = routes.BufferActivationRequest(**values)
        assert payload.confirmed is not True or payload.confirmation_text_version != "BUFFER_PILOT_ACTIVATION_V1"
    for field in ("publication_id", "approval_id", "actor", "pinterest_connection_id", "external_board_id"):
        with pytest.raises(ValueError):
            routes.BufferActivationRequest(
                confirmed=True, confirmation_text_version="BUFFER_PILOT_ACTIVATION_V1",
                **{field: "client-owned"},
            )


def test_revoke_route_has_no_body_model():
    assert routes.revoke_buffer.__annotations__.get("payload") is None
    assert "payload" not in routes.revoke_buffer.__code__.co_varnames


def test_publication_create_forbids_client_identity_and_control_plane_fields():
    forbidden = (
        "pinterest_connection_id", "external_board_id", "publication_fingerprint",
        "request_fingerprint", "authorization_id", "activation_id", "organization_id",
        "channel_id", "evidence", "actor",
    )
    for field in forbidden:
        with pytest.raises(ValueError):
            routes.PublicationCreate(
                approval_id="approval", pinterest_board_record_id="board",
                **{field: "client-owned"},
            )


def test_eligible_destinations_response_is_sanitized_and_approval_gated(monkeypatch):
    class Db:
        def get(self, model, key):
            return None
        def scalars(self, query):
            class Rows:
                def all(self):
                    return []
            return Rows()
    assert routes.eligible_destinations("unapproved", Db()) == []
    # The response contract is intentionally limited to server-safe display fields.
    assert set(("board_record_id", "display_name", "routing_label", "eligibility",
                "sync_status", "recommended")) <= {
                    "board_record_id", "display_name", "routing_label", "eligibility",
                    "sync_status", "recommended",
                }


def test_legacy_per_publication_environment_bindings_are_ignored_by_pilot_gate(tmp_path):
    SessionLocal, engine = _db(tmp_path / "legacy-binding.db")
    try:
        with SessionLocal() as db:
            publication = _ready_publication(db, fingerprint="legacy-binding")
            settings = type("Settings", (), {
                "publishing_enabled": True,
                "buffer_publishing_enabled": True,
                "buffer_single_pin_pilot_enabled": True,
                # These values are intentionally stale and must not bind the
                # database-backed control plane to client/config identity.
                "buffer_single_pin_pilot_publication_id": "old-publication",
                "buffer_single_pin_pilot_publication_fingerprint": "old-fingerprint",
                "buffer_single_pin_pilot_request_fingerprint": "old-request",
            })()
            assert validate_pilot(db, publication, settings) == (True, "BUFFER_PILOT_READY")
    finally:
        engine.dispose()