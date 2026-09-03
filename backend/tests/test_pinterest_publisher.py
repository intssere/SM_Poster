from app.services.pinterest_publisher import media_publishable

def test_media_url_rejects_private_and_accepts_public():
    assert not media_publishable("https://localhost/a")
    assert not media_publishable("http://example.test/a")
    assert not media_publishable("https://127.0.0.1/a")
    assert media_publishable("https://cdn.example.test/a")

import pytest
from app.integrations.pinterest.gateway import PinterestAmbiguousFailure, PinterestDefinitiveRejection
from app.services.pinterest_publisher import sanitize_metadata
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
from app.services.publication_scheduler import request_fingerprint_for
from test_pin_proposals import add_review_creative, setup_service
import asyncio
from app.models.domain import PinterestConnection, PinterestBoard, PinApproval, ProductImage
from app.services.publication_identity import PublicationIdentityService
from test_publication_identity import _prepared, _revision, _activate

@pytest.mark.parametrize("value", ["http://x", "https://localhost/x", "https://127.0.0.1/x", "https://10.0.0.1/x", "https://x.local/a"])
def test_media_publishability_rejects_non_public(value):
    assert not media_publishable(value)

@pytest.mark.parametrize("value", ["https://cdn.example.test/a", "https://images.example.org/pin.png"])
def test_media_publishability_accepts_public_https(value):
    assert media_publishable(value)

def test_metadata_allowlist_removes_credentials_and_raw_payloads():
    data = {"access_token":"secret", "refresh_token":"secret2", "Authorization":"Bearer x", "raw_body":"x", "validated_pin_id":"pin123", "http_status":201, "request_id":"r"}
    assert sanitize_metadata(data) == {"validated_pin_id":"pin123", "http_status":201, "request_id":"r"}

@pytest.mark.parametrize("value", [None, [], ["secret"], "secret", 123])
def test_metadata_allowlist_handles_none_and_non_mapping(value):
    assert sanitize_metadata(value) == {}

def test_db_backed_publish_success_records_attempt_and_provider_pin(monkeypatch):
    db, _, _ = setup_service()
    publication = PinPublication(draft_id="draft", creative_id="creative", approval_id="approval", source_image_id="image", publication_fingerprint="f"*64, status=PublicationStatus.PUBLISHING, pinterest_board_id_snapshot="board", title_snapshot="Title", description_snapshot="Description", destination_url="https://example.test", media_url_snapshot="https://cdn.example.test/image.png")
    db.add(publication); db.commit(); db.refresh(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=request_fingerprint_for(publication), safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    monkeypatch.setattr("app.services.pinterest_publisher.execution_publish_readiness", lambda db, pub, att: (True, None))
    class Gateway:
        calls = 0
        async def create_pin(self, payload):
            self.calls += 1
            return {"id": "pin123"}
    gateway = Gateway()
    result = asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    assert result["id"] == "pin123" and gateway.calls == 1
    assert publication.status == PublicationStatus.PUBLISHED
    assert attempt.status == "SUCCEEDED" and attempt.provider_pin_id == "pin123"
    db.close()

def test_db_backed_publish_blocks_external_board_id_mismatch(monkeypatch):
    db, proposals, draft, creative = _prepared("board-id-mismatch")
    revision = _revision(db, draft, creative, 2); _activate(db, draft, revision)
    creative.rendered_url = "https://cdn.example.test/source.png"; db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.query(PinApproval).filter_by(draft_id=draft.id).one()
    connection = PinterestConnection(external_user_id="user-1", access_token_ciphertext="enc-a", refresh_token_ciphertext="enc-r", granted_scopes=["pins:write"], status="CONNECTED")
    db.add(connection); db.flush()
    board = PinterestBoard(connection_id=connection.id, external_board_id="ext-board", name="Board", is_active=True, is_eligible=True)
    db.add(board); db.commit(); db.refresh(board)
    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(approval_id=approval.id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=None)
    db = proposals.session_factory(); publication = db.get(PinPublication, publication.id); publication.status = PublicationStatus.PUBLISHING; db.commit(); db.refresh(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=request_fingerprint_for(publication), safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt); original_fingerprint = attempt.request_fingerprint
    board = db.get(PinterestBoard, board.id); board.external_board_id = "ext-board-new"; db.commit()
    monkeypatch.setattr("app.services.pinterest_publisher.get_settings", lambda: type("S", (), {"publishing_enabled": True})())
    class FakeGateway:
        calls = 0
        async def create_pin(self, payload): self.calls += 1; return {"id": "must-not-exist"}
    gateway = FakeGateway()
    with pytest.raises(RuntimeError, match="DESTINATION_MISMATCH"):
        asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    db.expire_all(); persisted = db.get(PinPublication, publication.id); persisted_attempt = db.get(PublicationAttempt, attempt.id); persisted_board = db.get(PinterestBoard, board.id)
    assert persisted_board.is_active is True and persisted_board.is_eligible is True and persisted_board.external_board_id == "ext-board-new"
    assert db.get(PinterestConnection, connection.id).status == "CONNECTED" and "pins:write" in (db.get(PinterestConnection, connection.id).granted_scopes or [])
    assert persisted_attempt.request_fingerprint == original_fingerprint == request_fingerprint_for(persisted)
    assert gateway.calls == 0
    assert persisted.status == PublicationStatus.PUBLISH_UNKNOWN and persisted_attempt.status == "UNKNOWN"
    assert persisted.error_code == persisted_attempt.error_code == "DESTINATION_MISMATCH"
    assert persisted.pinterest_pin_id is None and persisted_attempt.provider_pin_id is None and persisted_attempt.safe_response_metadata == {}
    db.close()

def test_db_backed_publish_blocks_invalidated_approval(monkeypatch):
    db, proposals, draft, creative = _prepared("invalidated-approval")
    revision = _revision(db, draft, creative, 2); _activate(db, draft, revision)
    creative.rendered_url = "https://cdn.example.test/source.png"; db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.query(PinApproval).filter_by(draft_id=draft.id).one()
    connection = PinterestConnection(external_user_id="user-1", access_token_ciphertext="enc-a", refresh_token_ciphertext="enc-r", granted_scopes=["pins:write"], status="CONNECTED")
    db.add(connection); db.flush()
    board = PinterestBoard(connection_id=connection.id, external_board_id="ext-board", name="Board", is_active=True, is_eligible=True)
    db.add(board); db.commit(); db.refresh(board)
    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(approval_id=approval.id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=None)
    db = proposals.session_factory(); publication = db.get(PinPublication, publication.id); publication.status = PublicationStatus.PUBLISHING; db.commit(); db.refresh(publication)
    original_fingerprint = publication.publication_fingerprint
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=request_fingerprint_for(publication), safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    approval = db.get(PinApproval, approval.id); approval.decision = "REJECTED"; db.commit()
    monkeypatch.setattr("app.services.pinterest_publisher.get_settings", lambda: type("S", (), {"publishing_enabled": True})())
    class FakeGateway:
        calls = 0
        async def create_pin(self, payload): self.calls += 1; return {"id": "must-not-exist"}
    gateway = FakeGateway()
    with pytest.raises(RuntimeError, match="INVALID_APPROVAL"):
        asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    db.expire_all(); persisted = db.get(PinPublication, publication.id); persisted_attempt = db.get(PublicationAttempt, attempt.id)
    assert gateway.calls == 0
    assert persisted.publication_fingerprint == original_fingerprint and persisted_attempt.request_fingerprint == request_fingerprint_for(persisted)
    assert persisted.status == PublicationStatus.PUBLISH_UNKNOWN and persisted_attempt.status == "UNKNOWN"
    assert persisted.error_code == persisted_attempt.error_code == "INVALID_APPROVAL"
    assert persisted.pinterest_pin_id is None and persisted_attempt.provider_pin_id is None and persisted_attempt.safe_response_metadata == {}
    db.close()

def test_db_backed_publish_blocks_approval_revision_mismatch(monkeypatch):
    db, proposals, draft, creative = _prepared("approval-revision-mismatch")
    revision = _revision(db, draft, creative, 2); _activate(db, draft, revision)
    alternate_revision = _revision(db, draft, creative, 3)
    creative.rendered_url = "https://cdn.example.test/source.png"; db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.query(PinApproval).filter_by(draft_id=draft.id).one()
    connection = PinterestConnection(external_user_id="user-1", access_token_ciphertext="enc-a", refresh_token_ciphertext="enc-r", granted_scopes=["pins:write"], status="CONNECTED")
    db.add(connection); db.flush()
    board = PinterestBoard(connection_id=connection.id, external_board_id="ext-board", name="Board", is_active=True, is_eligible=True)
    db.add(board); db.commit(); db.refresh(board)
    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(approval_id=approval.id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=None)
    db = proposals.session_factory(); publication = db.get(PinPublication, publication.id); publication.status = PublicationStatus.PUBLISHING; db.commit(); db.refresh(publication)
    original_publication_revision_id = publication.revision_id
    original_fingerprint = request_fingerprint_for(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=original_fingerprint, safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    assert attempt.request_fingerprint == request_fingerprint_for(publication)
    approval = db.get(PinApproval, approval.id)
    approval.revision_id = alternate_revision.id
    db.commit()
    monkeypatch.setattr("app.services.pinterest_publisher.get_settings", lambda: type("S", (), {"publishing_enabled": True})())
    class FakeGateway:
        calls = 0
        async def create_pin(self, payload): self.calls += 1; return {"id": "must-not-exist"}
    gateway = FakeGateway()
    with pytest.raises(RuntimeError, match="INVALID_APPROVAL"):
        asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    db.expire_all(); persisted = db.get(PinPublication, publication.id); persisted_attempt = db.get(PublicationAttempt, attempt.id); persisted_approval = db.get(PinApproval, approval.id)
    assert persisted_approval.revision_id == alternate_revision.id and persisted.revision_id == original_publication_revision_id
    assert persisted_attempt.request_fingerprint == original_fingerprint == request_fingerprint_for(persisted)
    assert gateway.calls == 0
    assert persisted.status == PublicationStatus.PUBLISH_UNKNOWN and persisted_attempt.status == "UNKNOWN"
    assert persisted.error_code == persisted_attempt.error_code == "INVALID_APPROVAL"
    assert persisted.pinterest_pin_id is None and persisted_attempt.provider_pin_id is None and persisted_attempt.safe_response_metadata == {}
    db.close()

def test_db_backed_publish_blocks_approval_creative_mismatch(monkeypatch):
    db, proposals, draft, creative = _prepared("approval-creative-mismatch")
    revision = _revision(db, draft, creative, 2); _activate(db, draft, revision)
    creative.rendered_url = "https://cdn.example.test/source.png"; db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.query(PinApproval).filter_by(draft_id=draft.id).one()
    connection = PinterestConnection(external_user_id="user-1", access_token_ciphertext="enc-a", refresh_token_ciphertext="enc-r", granted_scopes=["pins:write"], status="CONNECTED")
    db.add(connection); db.flush()
    board = PinterestBoard(connection_id=connection.id, external_board_id="ext-board", name="Board", is_active=True, is_eligible=True)
    db.add(board); db.commit(); db.refresh(board)
    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(approval_id=approval.id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=None)
    db = proposals.session_factory(); publication = db.get(PinPublication, publication.id); publication.status = PublicationStatus.PUBLISHING; db.commit(); db.refresh(publication)
    original_publication_creative_id = publication.creative_id
    original_approval_revision_id = approval.revision_id
    original_fingerprint = request_fingerprint_for(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=original_fingerprint, safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    assert attempt.request_fingerprint == request_fingerprint_for(publication)
    alternate_creative = add_review_creative(db, draft.id, suffix="alternate-approval-creative")
    approval = db.get(PinApproval, approval.id)
    approval.creative_id = alternate_creative.id
    db.commit()
    monkeypatch.setattr("app.services.pinterest_publisher.get_settings", lambda: type("S", (), {"publishing_enabled": True})())
    class FakeGateway:
        calls = 0
        async def create_pin(self, payload): self.calls += 1; return {"id": "must-not-exist"}
    gateway = FakeGateway()
    with pytest.raises(RuntimeError, match="INVALID_APPROVAL"):
        asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    db.expire_all(); persisted = db.get(PinPublication, publication.id); persisted_attempt = db.get(PublicationAttempt, attempt.id); persisted_approval = db.get(PinApproval, approval.id)
    assert persisted_approval.creative_id == alternate_creative.id and persisted.creative_id == original_publication_creative_id
    assert persisted_approval.revision_id == original_approval_revision_id == persisted.revision_id
    assert persisted_attempt.request_fingerprint == original_fingerprint == request_fingerprint_for(persisted)
    assert gateway.calls == 0
    assert persisted.status == PublicationStatus.PUBLISH_UNKNOWN and persisted_attempt.status == "UNKNOWN"
    assert persisted.error_code == persisted_attempt.error_code == "INVALID_APPROVAL"
    assert persisted.pinterest_pin_id is None and persisted_attempt.provider_pin_id is None and persisted_attempt.safe_response_metadata == {}
    db.close()

def test_db_backed_publish_blocks_creative_source_image_mismatch(monkeypatch):
    db, proposals, draft, creative = _prepared("creative-source-image-mismatch")
    revision = _revision(db, draft, creative, 2); _activate(db, draft, revision)
    creative.rendered_url = "https://cdn.example.test/source.png"; db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.query(PinApproval).filter_by(draft_id=draft.id).one()
    connection = PinterestConnection(external_user_id="user-1", access_token_ciphertext="enc-a", refresh_token_ciphertext="enc-r", granted_scopes=["pins:write"], status="CONNECTED")
    db.add(connection); db.flush()
    board = PinterestBoard(connection_id=connection.id, external_board_id="ext-board", name="Board", is_active=True, is_eligible=True)
    db.add(board); db.commit(); db.refresh(board)
    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(approval_id=approval.id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=None)
    db = proposals.session_factory(); publication = db.get(PinPublication, publication.id); publication.status = PublicationStatus.PUBLISHING; db.commit(); db.refresh(publication)
    original_publication_source_image_id = publication.source_image_id
    original_publication_creative_id = publication.creative_id
    original_fingerprint = request_fingerprint_for(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=original_fingerprint, safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    assert attempt.request_fingerprint == request_fingerprint_for(publication)
    creative = db.get(type(creative), creative.id)
    original_image = db.get(ProductImage, creative.source_image_id)
    alternate_image = ProductImage(product_id=original_image.product_id, shopify_media_id="gid://shopify/MediaImage/alternate-source", source_url="https://cdn.shopify.com/alternate-source.jpg", width=1200, height=1500, is_primary=False, editorial_eligible=True)
    db.add(alternate_image); db.commit(); db.refresh(alternate_image)
    creative.source_image_id = alternate_image.id
    db.commit()
    assert creative.draft_id == publication.draft_id
    assert creative.source_image_id != publication.source_image_id
    monkeypatch.setattr("app.services.pinterest_publisher.get_settings", lambda: type("S", (), {"publishing_enabled": True})())
    class FakeGateway:
        calls = 0
        async def create_pin(self, payload): self.calls += 1; return {"id": "must-not-exist"}
    gateway = FakeGateway()
    with pytest.raises(RuntimeError, match="INVALID_CREATIVE"):
        asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    db.expire_all(); persisted = db.get(PinPublication, publication.id); persisted_attempt = db.get(PublicationAttempt, attempt.id); persisted_approval = db.get(PinApproval, approval.id); persisted_creative = db.get(type(creative), creative.id)
    assert persisted.source_image_id == original_publication_source_image_id and persisted_creative.source_image_id == alternate_image.id
    assert persisted.creative_id == original_publication_creative_id == persisted_approval.creative_id
    assert persisted_creative.draft_id == persisted.draft_id
    assert persisted_attempt.request_fingerprint == original_fingerprint == request_fingerprint_for(persisted)
    assert gateway.calls == 0
    assert persisted.status == PublicationStatus.PUBLISH_UNKNOWN and persisted_attempt.status == "UNKNOWN"
    assert persisted.error_code == persisted_attempt.error_code == "INVALID_CREATIVE"
    assert persisted.pinterest_pin_id is None and persisted_attempt.provider_pin_id is None and persisted_attempt.safe_response_metadata == {}
    db.close()

def test_db_backed_publish_blocks_non_publishable_media(monkeypatch):
    db, proposals, draft, creative = _prepared("non-publishable-media")
    revision = _revision(db, draft, creative, 2); _activate(db, draft, revision)
    creative.rendered_url = "https://cdn.example.test/source.png"; db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.query(PinApproval).filter_by(draft_id=draft.id).one()
    connection = PinterestConnection(external_user_id="user-1", access_token_ciphertext="enc-a", refresh_token_ciphertext="enc-r", granted_scopes=["pins:write"], status="CONNECTED")
    db.add(connection); db.flush()
    board = PinterestBoard(connection_id=connection.id, external_board_id="ext-board", name="Board", is_active=True, is_eligible=True)
    db.add(board); db.commit(); db.refresh(board)
    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(approval_id=approval.id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=None)
    db = proposals.session_factory(); publication = db.get(PinPublication, publication.id)
    publication.status = PublicationStatus.PUBLISHING
    publication.media_url_snapshot = "https://127.0.0.1/image.jpg"
    db.commit(); db.refresh(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=request_fingerprint_for(publication), safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    connection = db.get(PinterestConnection, connection.id); board = db.get(PinterestBoard, board.id); approval = db.get(PinApproval, approval.id); creative = db.get(type(creative), creative.id)
    assert attempt.request_fingerprint == request_fingerprint_for(publication)
    assert connection.status == "CONNECTED" and "pins:write" in (connection.granted_scopes or [])
    assert board.is_active is True and board.is_eligible is True
    assert approval.decision == "APPROVED"
    assert creative.source_image_id == publication.source_image_id
    monkeypatch.setattr("app.services.pinterest_publisher.get_settings", lambda: type("S", (), {"publishing_enabled": True})())
    class FakeGateway:
        calls = 0
        async def create_pin(self, payload): self.calls += 1; return {"id": "must-not-exist"}
    gateway = FakeGateway()
    with pytest.raises(RuntimeError, match="MEDIA_NOT_PUBLISHABLE"):
        asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    db.expire_all(); persisted = db.get(PinPublication, publication.id); persisted_attempt = db.get(PublicationAttempt, attempt.id)
    assert persisted_attempt.request_fingerprint == request_fingerprint_for(persisted)
    assert gateway.calls == 0
    assert persisted.status == PublicationStatus.PUBLISH_UNKNOWN and persisted_attempt.status == "UNKNOWN"
    assert persisted.error_code == persisted_attempt.error_code == "MEDIA_NOT_PUBLISHABLE"
    assert persisted.pinterest_pin_id is None and persisted_attempt.provider_pin_id is None and persisted_attempt.safe_response_metadata == {}
    db.close()

def test_db_backed_publish_success_uses_real_execution_readiness(monkeypatch):
    db, proposals, draft, creative = _prepared("real-readiness")
    revision = _revision(db, draft, creative, 2); _activate(db, draft, revision)
    creative.rendered_url = "https://cdn.example.test/source.png"
    db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.query(PinApproval).filter_by(draft_id=draft.id).one()
    connection = PinterestConnection(external_user_id="user-1", access_token_ciphertext="enc-a", refresh_token_ciphertext="enc-r", granted_scopes=["pins:write"], status="CONNECTED")
    db.add(connection); db.flush()
    board = PinterestBoard(connection_id=connection.id, external_board_id="ext-board", name="Board", is_active=True, is_eligible=True)
    db.add(board); db.commit(); db.refresh(board)
    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(approval_id=approval.id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=None)
    db = proposals.session_factory(); publication = db.get(PinPublication, publication.id)
    publication.status = PublicationStatus.PUBLISHING; db.commit(); db.refresh(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=request_fingerprint_for(publication), safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    monkeypatch.setattr("app.services.pinterest_publisher.get_settings", lambda: type("S", (), {"publishing_enabled": True})())
    class FakeGateway:
        calls = 0
        async def create_pin(self, payload): self.calls += 1; return {"id": "pin123"}
    gateway = FakeGateway()
    asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    db.expire_all(); persisted = db.get(PinPublication, publication.id); persisted_attempt = db.get(PublicationAttempt, attempt.id)
    assert gateway.calls == 1
    assert persisted.status == PublicationStatus.PUBLISHED and persisted.pinterest_pin_id == "pin123"
    assert persisted_attempt.status == "SUCCEEDED" and persisted_attempt.provider_pin_id == "pin123"
    db.close()

def test_db_backed_publish_classifies_definitive_provider_rejection(monkeypatch):
    db, proposals, draft, creative = _prepared("definitive-provider-rejection")
    revision = _revision(db, draft, creative, 2); _activate(db, draft, revision)
    creative.rendered_url = "https://cdn.example.test/source.png"; db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.query(PinApproval).filter_by(draft_id=draft.id).one()
    connection = PinterestConnection(external_user_id="user-1", access_token_ciphertext="enc-a", refresh_token_ciphertext="enc-r", granted_scopes=["pins:write"], status="CONNECTED")
    db.add(connection); db.flush()
    board = PinterestBoard(connection_id=connection.id, external_board_id="ext-board", name="Board", is_active=True, is_eligible=True)
    db.add(board); db.commit(); db.refresh(board)
    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(approval_id=approval.id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=None)
    db = proposals.session_factory(); publication = db.get(PinPublication, publication.id); publication.status = PublicationStatus.PUBLISHING; db.commit(); db.refresh(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=request_fingerprint_for(publication), safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    connection = db.get(PinterestConnection, connection.id); board = db.get(PinterestBoard, board.id); approval = db.get(PinApproval, approval.id); creative = db.get(type(creative), creative.id)
    assert attempt.request_fingerprint == request_fingerprint_for(publication)
    assert connection.status == "CONNECTED" and "pins:write" in (connection.granted_scopes or [])
    assert board.connection_id == connection.id and board.external_board_id == publication.pinterest_board_id_snapshot
    assert board.is_active is True and board.is_eligible is True
    assert approval.decision == "APPROVED" and approval.draft_id == publication.draft_id
    assert approval.revision_id == publication.revision_id and approval.creative_id == publication.creative_id
    assert creative.source_image_id == publication.source_image_id
    assert media_publishable(publication.media_url_snapshot)
    monkeypatch.setattr("app.services.pinterest_publisher.get_settings", lambda: type("S", (), {"publishing_enabled": True})())
    class FakeGateway:
        calls = 0
        async def create_pin(self, payload):
            self.calls += 1
            raise PinterestDefinitiveRejection(code="PROVIDER_REJECTED", status_code=400)
    gateway = FakeGateway()
    with pytest.raises(RuntimeError, match="^PROVIDER_REJECTED$"):
        asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    db.expire_all(); persisted = db.get(PinPublication, publication.id); persisted_attempt = db.get(PublicationAttempt, attempt.id)
    assert gateway.calls == 1
    assert persisted.status == PublicationStatus.PUBLISH_FAILED and persisted_attempt.status == "FAILED"
    assert persisted.error_code == persisted_attempt.error_code == "PROVIDER_REJECTED"
    assert persisted.pinterest_pin_id is None and persisted_attempt.provider_pin_id is None and persisted_attempt.safe_response_metadata == {}
    db.close()

def test_db_backed_publish_classifies_ambiguous_provider_failure(monkeypatch):
    db, proposals, draft, creative = _prepared("ambiguous-provider-failure")
    revision = _revision(db, draft, creative, 2); _activate(db, draft, revision)
    creative.rendered_url = "https://cdn.example.test/source.png"; db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.query(PinApproval).filter_by(draft_id=draft.id).one()
    connection = PinterestConnection(external_user_id="user-1", access_token_ciphertext="enc-a", refresh_token_ciphertext="enc-r", granted_scopes=["pins:write"], status="CONNECTED")
    db.add(connection); db.flush()
    board = PinterestBoard(connection_id=connection.id, external_board_id="ext-board", name="Board", is_active=True, is_eligible=True)
    db.add(board); db.commit(); db.refresh(board)
    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(approval_id=approval.id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=None)
    db = proposals.session_factory(); publication = db.get(PinPublication, publication.id); publication.status = PublicationStatus.PUBLISHING; db.commit(); db.refresh(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=request_fingerprint_for(publication), safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    connection = db.get(PinterestConnection, connection.id); board = db.get(PinterestBoard, board.id); approval = db.get(PinApproval, approval.id); creative = db.get(type(creative), creative.id)
    assert publication.status == PublicationStatus.PUBLISHING and attempt.status == "STARTED" and attempt.publication_id == publication.id
    assert attempt.request_fingerprint == request_fingerprint_for(publication)
    assert connection.status == "CONNECTED" and "pins:write" in (connection.granted_scopes or [])
    assert board.connection_id == connection.id and board.external_board_id == publication.pinterest_board_id_snapshot
    assert board.is_active is True and board.is_eligible is True
    assert approval.decision == "APPROVED" and approval.draft_id == publication.draft_id
    assert approval.revision_id == publication.revision_id and approval.creative_id == publication.creative_id
    assert creative.source_image_id == publication.source_image_id
    assert media_publishable(publication.media_url_snapshot)
    monkeypatch.setattr("app.services.pinterest_publisher.get_settings", lambda: type("S", (), {"publishing_enabled": True})())
    class FakeGateway:
        calls = 0
        async def create_pin(self, payload):
            self.calls += 1
            raise PinterestAmbiguousFailure(code="PROVIDER_AMBIGUOUS", status_code=503)
    gateway = FakeGateway()
    with pytest.raises(RuntimeError, match="^PUBLISH_UNKNOWN$"):
        asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    db.expire_all(); persisted = db.get(PinPublication, publication.id); persisted_attempt = db.get(PublicationAttempt, attempt.id)
    assert gateway.calls == 1
    assert persisted.status == PublicationStatus.PUBLISH_UNKNOWN and persisted_attempt.status == "UNKNOWN"
    assert persisted.error_code == persisted_attempt.error_code == "PUBLISH_UNKNOWN"
    assert persisted.pinterest_pin_id is None and persisted_attempt.provider_pin_id is None and persisted_attempt.safe_response_metadata == {}
    db.close()

def test_db_backed_publish_classifies_timeout_as_unknown(monkeypatch):
    db, proposals, draft, creative = _prepared("provider-timeout")
    revision = _revision(db, draft, creative, 2); _activate(db, draft, revision)
    creative.rendered_url = "https://cdn.example.test/source.png"; db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.query(PinApproval).filter_by(draft_id=draft.id).one()
    connection = PinterestConnection(external_user_id="user-1", access_token_ciphertext="enc-a", refresh_token_ciphertext="enc-r", granted_scopes=["pins:write"], status="CONNECTED")
    db.add(connection); db.flush()
    board = PinterestBoard(connection_id=connection.id, external_board_id="ext-board", name="Board", is_active=True, is_eligible=True)
    db.add(board); db.commit(); db.refresh(board)
    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(approval_id=approval.id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=None)
    db = proposals.session_factory(); publication = db.get(PinPublication, publication.id); publication.status = PublicationStatus.PUBLISHING; db.commit(); db.refresh(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=request_fingerprint_for(publication), safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    connection = db.get(PinterestConnection, connection.id); board = db.get(PinterestBoard, board.id); approval = db.get(PinApproval, approval.id); creative = db.get(type(creative), creative.id)
    assert publication.status == PublicationStatus.PUBLISHING and attempt.status == "STARTED" and attempt.publication_id == publication.id
    assert attempt.request_fingerprint == request_fingerprint_for(publication)
    assert connection.status == "CONNECTED" and "pins:write" in (connection.granted_scopes or [])
    assert board.connection_id == connection.id and board.external_board_id == publication.pinterest_board_id_snapshot
    assert board.is_active is True and board.is_eligible is True
    assert approval.decision == "APPROVED" and approval.draft_id == publication.draft_id
    assert approval.revision_id == publication.revision_id and approval.creative_id == publication.creative_id
    assert creative.source_image_id == publication.source_image_id
    assert media_publishable(publication.media_url_snapshot)
    monkeypatch.setattr("app.services.pinterest_publisher.get_settings", lambda: type("S", (), {"publishing_enabled": True})())
    class FakeGateway:
        calls = 0
        async def create_pin(self, payload):
            self.calls += 1
            raise TimeoutError("simulated post-dispatch timeout")
    gateway = FakeGateway()
    with pytest.raises(RuntimeError, match="^PUBLISH_UNKNOWN$"):
        asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    db.expire_all(); persisted = db.get(PinPublication, publication.id); persisted_attempt = db.get(PublicationAttempt, attempt.id)
    assert gateway.calls == 1
    assert persisted.status == PublicationStatus.PUBLISH_UNKNOWN and persisted_attempt.status == "UNKNOWN"
    assert persisted.error_code == persisted_attempt.error_code == "PUBLISH_UNKNOWN"
    assert persisted.pinterest_pin_id is None and persisted_attempt.provider_pin_id is None and persisted_attempt.safe_response_metadata == {}
    db.close()

def test_db_backed_publish_classifies_connection_reset_as_unknown(monkeypatch):
    db, proposals, draft, creative = _prepared("provider-connection-reset")
    revision = _revision(db, draft, creative, 2); _activate(db, draft, revision)
    creative.rendered_url = "https://cdn.example.test/source.png"; db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.query(PinApproval).filter_by(draft_id=draft.id).one()
    connection = PinterestConnection(external_user_id="user-1", access_token_ciphertext="enc-a", refresh_token_ciphertext="enc-r", granted_scopes=["pins:write"], status="CONNECTED")
    db.add(connection); db.flush()
    board = PinterestBoard(connection_id=connection.id, external_board_id="ext-board", name="Board", is_active=True, is_eligible=True)
    db.add(board); db.commit(); db.refresh(board)
    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(approval_id=approval.id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=None)
    db = proposals.session_factory(); publication = db.get(PinPublication, publication.id); publication.status = PublicationStatus.PUBLISHING; db.commit(); db.refresh(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=request_fingerprint_for(publication), safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    connection = db.get(PinterestConnection, connection.id); board = db.get(PinterestBoard, board.id); approval = db.get(PinApproval, approval.id); creative = db.get(type(creative), creative.id)
    assert publication.status == PublicationStatus.PUBLISHING and attempt.status == "STARTED" and attempt.publication_id == publication.id
    assert attempt.request_fingerprint == request_fingerprint_for(publication)
    assert connection.status == "CONNECTED" and "pins:write" in (connection.granted_scopes or [])
    assert board.connection_id == connection.id and board.external_board_id == publication.pinterest_board_id_snapshot
    assert board.is_active is True and board.is_eligible is True
    assert approval.decision == "APPROVED" and approval.draft_id == publication.draft_id
    assert approval.revision_id == publication.revision_id and approval.creative_id == publication.creative_id
    assert creative.source_image_id == publication.source_image_id
    assert media_publishable(publication.media_url_snapshot)
    monkeypatch.setattr("app.services.pinterest_publisher.get_settings", lambda: type("S", (), {"publishing_enabled": True})())
    class FakeGateway:
        calls = 0
        async def create_pin(self, payload):
            self.calls += 1
            raise ConnectionResetError("simulated post-dispatch connection reset")
    gateway = FakeGateway()
    with pytest.raises(RuntimeError, match="^PUBLISH_UNKNOWN$"):
        asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    db.expire_all(); persisted = db.get(PinPublication, publication.id); persisted_attempt = db.get(PublicationAttempt, attempt.id)
    assert gateway.calls == 1
    assert persisted.status == PublicationStatus.PUBLISH_UNKNOWN and persisted_attempt.status == "UNKNOWN"
    assert persisted.error_code == persisted_attempt.error_code == "PUBLISH_UNKNOWN"
    assert persisted.pinterest_pin_id is None and persisted_attempt.provider_pin_id is None and persisted_attempt.safe_response_metadata == {}
    db.close()

def test_db_backed_publish_classifies_empty_success_body_as_unknown(monkeypatch):
    db, proposals, draft, creative = _prepared("empty-provider-result")
    revision = _revision(db, draft, creative, 2); _activate(db, draft, revision)
    creative.rendered_url = "https://cdn.example.test/source.png"; db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.query(PinApproval).filter_by(draft_id=draft.id).one()
    connection = PinterestConnection(external_user_id="user-1", access_token_ciphertext="enc-a", refresh_token_ciphertext="enc-r", granted_scopes=["pins:write"], status="CONNECTED")
    db.add(connection); db.flush()
    board = PinterestBoard(connection_id=connection.id, external_board_id="ext-board", name="Board", is_active=True, is_eligible=True)
    db.add(board); db.commit(); db.refresh(board)
    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(approval_id=approval.id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=None)
    db = proposals.session_factory(); publication = db.get(PinPublication, publication.id); publication.status = PublicationStatus.PUBLISHING; db.commit(); db.refresh(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=request_fingerprint_for(publication), safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    connection = db.get(PinterestConnection, connection.id); board = db.get(PinterestBoard, board.id); approval = db.get(PinApproval, approval.id); creative = db.get(type(creative), creative.id)
    assert publication.status == PublicationStatus.PUBLISHING and attempt.status == "STARTED" and attempt.publication_id == publication.id
    assert attempt.request_fingerprint == request_fingerprint_for(publication)
    assert connection.status == "CONNECTED" and "pins:write" in (connection.granted_scopes or [])
    assert board.connection_id == connection.id and board.external_board_id == publication.pinterest_board_id_snapshot
    assert board.is_active is True and board.is_eligible is True
    assert approval.decision == "APPROVED" and approval.draft_id == publication.draft_id
    assert approval.revision_id == publication.revision_id and approval.creative_id == publication.creative_id
    assert creative.draft_id == publication.draft_id and creative.source_image_id == publication.source_image_id
    assert media_publishable(publication.media_url_snapshot)
    monkeypatch.setattr("app.services.pinterest_publisher.get_settings", lambda: type("S", (), {"publishing_enabled": True})())
    class FakeGateway:
        calls = 0
        async def create_pin(self, payload):
            self.calls += 1
            return {}
    gateway = FakeGateway()
    with pytest.raises(RuntimeError, match="^PUBLISH_UNKNOWN$"):
        asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    db.expire_all(); persisted = db.get(PinPublication, publication.id); persisted_attempt = db.get(PublicationAttempt, attempt.id)
    assert gateway.calls == 1
    assert persisted.status == PublicationStatus.PUBLISH_UNKNOWN and persisted_attempt.status == "UNKNOWN"
    assert persisted.error_code == persisted_attempt.error_code == "PUBLISH_UNKNOWN"
    assert persisted.pinterest_pin_id is None and persisted_attempt.provider_pin_id is None
    assert persisted.published_at is None and persisted_attempt.safe_response_metadata == {}
    db.close()

def test_db_backed_publish_blocks_request_fingerprint_mismatch(monkeypatch):
    db, proposals, draft, creative = _prepared("fingerprint-mismatch")
    revision = _revision(db, draft, creative, 2); _activate(db, draft, revision)
    creative.rendered_url = "https://cdn.example.test/source.png"
    db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.query(PinApproval).filter_by(draft_id=draft.id).one()
    connection = PinterestConnection(external_user_id="user-1", access_token_ciphertext="enc-a", refresh_token_ciphertext="enc-r", granted_scopes=["pins:write"], status="CONNECTED")
    db.add(connection); db.flush()
    board = PinterestBoard(connection_id=connection.id, external_board_id="ext-board", name="Board", is_active=True, is_eligible=True)
    db.add(board); db.commit(); db.refresh(board)
    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(approval_id=approval.id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=None)
    db = proposals.session_factory(); publication = db.get(PinPublication, publication.id)
    publication.status = PublicationStatus.PUBLISHING; db.commit(); db.refresh(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint="0" * 64, safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    monkeypatch.setattr("app.services.pinterest_publisher.get_settings", lambda: type("S", (), {"publishing_enabled": True})())
    class FakeGateway:
        calls = 0
        async def create_pin(self, payload): self.calls += 1; return {"id": "must-not-exist"}
    gateway = FakeGateway()
    with pytest.raises(RuntimeError, match="ATTEMPT_MISMATCH"):
        asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    db.expire_all(); persisted = db.get(PinPublication, publication.id); persisted_attempt = db.get(PublicationAttempt, attempt.id)
    assert gateway.calls == 0
    assert persisted.status == PublicationStatus.PUBLISH_UNKNOWN
    assert persisted_attempt.status == "UNKNOWN"
    assert persisted.error_code == persisted_attempt.error_code == "ATTEMPT_MISMATCH"
    assert persisted.pinterest_pin_id is None and persisted_attempt.provider_pin_id is None
    assert persisted_attempt.safe_response_metadata == {}
    db.close()

def test_db_backed_publish_blocks_disconnected_connection(monkeypatch):
    db, proposals, draft, creative = _prepared("disconnected-connection")
    revision = _revision(db, draft, creative, 2); _activate(db, draft, revision)
    creative.rendered_url = "https://cdn.example.test/source.png"
    db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.query(PinApproval).filter_by(draft_id=draft.id).one()
    connection = PinterestConnection(external_user_id="user-1", access_token_ciphertext="enc-a", refresh_token_ciphertext="enc-r", granted_scopes=["pins:write"], status="CONNECTED")
    db.add(connection); db.flush()
    board = PinterestBoard(connection_id=connection.id, external_board_id="ext-board", name="Board", is_active=True, is_eligible=True)
    db.add(board); db.commit(); db.refresh(board)
    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(approval_id=approval.id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=None)
    db = proposals.session_factory(); publication = db.get(PinPublication, publication.id)
    publication.status = PublicationStatus.PUBLISHING; db.commit(); db.refresh(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=request_fingerprint_for(publication), safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    connection = db.get(PinterestConnection, connection.id)
    connection.status = "DISCONNECTED"; db.commit()
    monkeypatch.setattr("app.services.pinterest_publisher.get_settings", lambda: type("S", (), {"publishing_enabled": True})())
    class FakeGateway:
        calls = 0
        async def create_pin(self, payload): self.calls += 1; return {"id": "must-not-exist"}
    gateway = FakeGateway()
    with pytest.raises(RuntimeError, match="PUBLISHING_SCOPE_REQUIRED"):
        asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    db.expire_all(); persisted = db.get(PinPublication, publication.id); persisted_attempt = db.get(PublicationAttempt, attempt.id)
    assert gateway.calls == 0
    assert persisted.status == PublicationStatus.PUBLISH_UNKNOWN
    assert persisted_attempt.status == "UNKNOWN"
    assert persisted.error_code == persisted_attempt.error_code == "PUBLISHING_SCOPE_REQUIRED"
    assert persisted.pinterest_pin_id is None and persisted_attempt.provider_pin_id is None
    assert persisted_attempt.safe_response_metadata == {}
    db.close()

def test_db_backed_publish_blocks_revoked_pins_write_scope(monkeypatch):
    db, proposals, draft, creative = _prepared("revoked-pins-write")
    revision = _revision(db, draft, creative, 2); _activate(db, draft, revision)
    creative.rendered_url = "https://cdn.example.test/source.png"
    db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.query(PinApproval).filter_by(draft_id=draft.id).one()
    connection = PinterestConnection(external_user_id="user-1", access_token_ciphertext="enc-a", refresh_token_ciphertext="enc-r", granted_scopes=["pins:write"], status="CONNECTED")
    db.add(connection); db.flush()
    board = PinterestBoard(connection_id=connection.id, external_board_id="ext-board", name="Board", is_active=True, is_eligible=True)
    db.add(board); db.commit(); db.refresh(board)
    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(approval_id=approval.id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=None)
    db = proposals.session_factory(); publication = db.get(PinPublication, publication.id)
    publication.status = PublicationStatus.PUBLISHING; db.commit(); db.refresh(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=request_fingerprint_for(publication), safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    connection = db.get(PinterestConnection, connection.id)
    connection.granted_scopes = []
    db.commit()
    monkeypatch.setattr("app.services.pinterest_publisher.get_settings", lambda: type("S", (), {"publishing_enabled": True})())
    class FakeGateway:
        calls = 0
        async def create_pin(self, payload): self.calls += 1; return {"id": "must-not-exist"}
    gateway = FakeGateway()
    with pytest.raises(RuntimeError, match="PUBLISHING_SCOPE_REQUIRED"):
        asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    db.expire_all(); persisted = db.get(PinPublication, publication.id); persisted_attempt = db.get(PublicationAttempt, attempt.id); persisted_connection = db.get(PinterestConnection, connection.id)
    assert persisted_connection.status == "CONNECTED" and "pins:write" not in (persisted_connection.granted_scopes or [])
    assert gateway.calls == 0
    assert persisted.status == PublicationStatus.PUBLISH_UNKNOWN and persisted_attempt.status == "UNKNOWN"
    assert persisted.error_code == persisted_attempt.error_code == "PUBLISHING_SCOPE_REQUIRED"
    assert persisted.pinterest_pin_id is None and persisted_attempt.provider_pin_id is None
    assert persisted_attempt.safe_response_metadata == {}
    db.close()

def test_db_backed_publish_blocks_inactive_board(monkeypatch):
    db, proposals, draft, creative = _prepared("inactive-board")
    revision = _revision(db, draft, creative, 2); _activate(db, draft, revision)
    creative.rendered_url = "https://cdn.example.test/source.png"
    db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.query(PinApproval).filter_by(draft_id=draft.id).one()
    connection = PinterestConnection(external_user_id="user-1", access_token_ciphertext="enc-a", refresh_token_ciphertext="enc-r", granted_scopes=["pins:write"], status="CONNECTED")
    db.add(connection); db.flush()
    board = PinterestBoard(connection_id=connection.id, external_board_id="ext-board", name="Board", is_active=True, is_eligible=True)
    db.add(board); db.commit(); db.refresh(board)
    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(approval_id=approval.id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=None)
    db = proposals.session_factory(); publication = db.get(PinPublication, publication.id)
    publication.status = PublicationStatus.PUBLISHING; db.commit(); db.refresh(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=request_fingerprint_for(publication), safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    board = db.get(PinterestBoard, board.id)
    board.is_active = False; db.commit()
    monkeypatch.setattr("app.services.pinterest_publisher.get_settings", lambda: type("S", (), {"publishing_enabled": True})())
    class FakeGateway:
        calls = 0
        async def create_pin(self, payload): self.calls += 1; return {"id": "must-not-exist"}
    gateway = FakeGateway()
    with pytest.raises(RuntimeError, match="INVALID_DESTINATION"):
        asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    db.expire_all(); persisted = db.get(PinPublication, publication.id); persisted_attempt = db.get(PublicationAttempt, attempt.id); persisted_board = db.get(PinterestBoard, board.id)
    assert persisted_board.is_active is False and persisted_board.is_eligible is True
    assert gateway.calls == 0
    assert persisted.status == PublicationStatus.PUBLISH_UNKNOWN and persisted_attempt.status == "UNKNOWN"
    assert persisted.error_code == persisted_attempt.error_code == "INVALID_DESTINATION"
    assert persisted.pinterest_pin_id is None and persisted_attempt.provider_pin_id is None
    assert persisted_attempt.safe_response_metadata == {}
    db.close()

def test_db_backed_publish_blocks_ineligible_board(monkeypatch):
    db, proposals, draft, creative = _prepared("ineligible-board")
    revision = _revision(db, draft, creative, 2); _activate(db, draft, revision)
    creative.rendered_url = "https://cdn.example.test/source.png"
    db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.query(PinApproval).filter_by(draft_id=draft.id).one()
    connection = PinterestConnection(external_user_id="user-1", access_token_ciphertext="enc-a", refresh_token_ciphertext="enc-r", granted_scopes=["pins:write"], status="CONNECTED")
    db.add(connection); db.flush()
    board = PinterestBoard(connection_id=connection.id, external_board_id="ext-board", name="Board", is_active=True, is_eligible=True)
    db.add(board); db.commit(); db.refresh(board)
    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(approval_id=approval.id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=None)
    db = proposals.session_factory(); publication = db.get(PinPublication, publication.id)
    publication.status = PublicationStatus.PUBLISHING; db.commit(); db.refresh(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=request_fingerprint_for(publication), safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    board = db.get(PinterestBoard, board.id)
    board.is_eligible = False; db.commit()
    monkeypatch.setattr("app.services.pinterest_publisher.get_settings", lambda: type("S", (), {"publishing_enabled": True})())
    class FakeGateway:
        calls = 0
        async def create_pin(self, payload): self.calls += 1; return {"id": "must-not-exist"}
    gateway = FakeGateway()
    with pytest.raises(RuntimeError, match="INVALID_DESTINATION"):
        asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    db.expire_all(); persisted = db.get(PinPublication, publication.id); persisted_attempt = db.get(PublicationAttempt, attempt.id); persisted_board = db.get(PinterestBoard, board.id)
    assert persisted_board.is_active is True and persisted_board.is_eligible is False
    assert gateway.calls == 0
    assert persisted.status == PublicationStatus.PUBLISH_UNKNOWN and persisted_attempt.status == "UNKNOWN"
    assert persisted.error_code == persisted_attempt.error_code == "INVALID_DESTINATION"
    assert persisted.pinterest_pin_id is None and persisted_attempt.provider_pin_id is None
    assert persisted_attempt.safe_response_metadata == {}
    db.close()
