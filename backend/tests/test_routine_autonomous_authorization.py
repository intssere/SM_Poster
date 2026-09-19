from datetime import datetime, timedelta, timezone
import hashlib

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import (
    ContentRevision,
    ContentVersionSelection,
    DraftStatus,
    PinApproval,
    PinConcept,
    PinCreative,
    PinDraft,
    PinPublication,
    PublicationStatus,
)
from app.models.routine_publishing import RoutineDispatchPermit
from app.services import routine_autonomous_authorization as autonomous


NOW = datetime(2026, 9, 20, 0, 30, tzinfo=timezone.utc)


def _settings(**overrides):
    values = {
        "database_url": "sqlite:///:memory:",
        "routine_autonomous_authorization_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _seed_original(db, *, draft_id="draft-1", unsupported=None, render_status="RENDERED", creative_count=1):
    concept = PinConcept(
        id=f"concept-{draft_id}",
        store_id="store-1",
        product_id=f"product-{draft_id}",
        content_angle_id="angle-1",
        fingerprint=(draft_id.replace("-", "") + "a" * 64)[:64],
        rationale={
            "unsupported_claims": list(unsupported or []),
            "warnings": [],
            "missing_facts": [],
        },
    )
    draft = PinDraft(
        id=draft_id,
        concept_id=concept.id,
        version=1,
        title="Afnan 9PM Eau de Parfum",
        description="Explore Afnan 9PM at Diamond Shelf.",
        alt_text="Afnan 9PM fragrance bottle",
        destination_url="https://diamondshelf.us/products/afnan-9pm",
        utm_url="https://diamondshelf.us/products/afnan-9pm?utm_source=pinterest",
        text_fingerprint=("b" * 63) + ("1" if draft_id == "draft-1" else "2"),
        status=DraftStatus.READY_FOR_REVIEW,
    )
    db.add_all([concept, draft])
    db.flush()

    creatives = []
    for index in range(creative_count):
        creative = PinCreative(
            id=f"creative-{draft_id}-{index}",
            draft_id=draft.id,
            template_id="template-1",
            source_image_id=f"image-{draft_id}",
            rendered_url=f"/api/pins/public-creatives/{draft_id}/{index}.png",
            sha256=hashlib.sha256(f"{draft_id}:{index}:png".encode()).hexdigest(),
            creative_fingerprint=hashlib.sha256(f"{draft_id}:{index}:creative".encode()).hexdigest(),
            width=1000,
            height=1500,
            render_status=render_status,
        )
        db.add(creative)
        creatives.append(creative)
    db.commit()
    return draft, creatives


def _seed_revision(db):
    draft, creatives = _seed_original(db)
    creative = creatives[0]
    revision = ContentRevision(
        id="revision-1",
        draft_id=draft.id,
        version=2,
        revision_kind="copy",
        status="REVIEW",
        headline="Afnan 9PM",
        title="Afnan 9PM | Arabian Fragrance",
        description="Explore Afnan 9PM at Diamond Shelf.",
        alt_text="Afnan 9PM fragrance bottle",
        cta="Shop now",
        content_angle="Arabian Fragrance",
        content_angle_key="arabian-fragrance",
        creative_template="Product Classification",
        creative_template_key="product_classification",
        destination_url=draft.destination_url,
        utm_url=draft.utm_url,
        keywords=["afnan 9pm", "arabian fragrance"],
        facts_used={"brand": "Afnan"},
        warnings=[],
        missing_facts=[],
        unsupported_claims=[],
        provenance={"source": "catalog"},
        text_fingerprint="c" * 64,
        creative_fingerprint=creative.creative_fingerprint,
        creative_id=creative.id,
        source_image_id=creative.source_image_id,
        provider_mode="deterministic",
        generation_mode="policy",
        reason="test revision",
        generation_type="copy",
        intended_channel="pinterest",
    )
    selection = ContentVersionSelection(
        id="selection-1",
        draft_id=draft.id,
        revision_id=revision.id,
        selected_by="test",
    )
    db.add_all([revision, selection])
    db.commit()
    return draft, creative, revision


def _machine_approval(db, draft, creative, *, revision_id=None, version_id="original"):
    snapshot = autonomous.autonomous_content_policy(db, draft.id)
    assert snapshot["ready"] is True
    approval = PinApproval(
        id=f"approval-{draft.id}",
        draft_id=draft.id,
        revision_id=revision_id,
        creative_id=creative.id,
        approved_version_id=version_id,
        decision="APPROVED",
        decided_by=autonomous.AUTONOMOUS_ACTOR,
        note=f"{autonomous.AUTONOMOUS_NOTE_PREFIX}{snapshot['policy_fingerprint']}",
    )
    draft.status = DraftStatus.APPROVED
    db.add(approval)
    db.commit()
    return approval


def _scheduled_publication(db, draft, creative, approval):
    row = PinPublication(
        id="publication-1",
        draft_id=draft.id,
        revision_id=approval.revision_id,
        creative_id=creative.id,
        approval_id=approval.id,
        publication_fingerprint="p" * 64,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=NOW - timedelta(minutes=5),
    )
    db.add(row)
    db.commit()
    return row


def test_safe_original_draft_policy_is_ready_and_deterministic():
    db = _db()
    draft, creatives = _seed_original(db)

    first = autonomous.autonomous_content_policy(db, draft.id)
    second = autonomous.autonomous_content_policy(db, draft.id)

    assert first["ready"] is True
    assert first["blockers"] == []
    assert first["selected_identity"] == {
        "revision_id": None,
        "creative_id": creatives[0].id,
        "approved_version_id": "original",
    }
    assert len(first["policy_fingerprint"]) == 64
    assert first["policy_fingerprint"] == second["policy_fingerprint"]
    assert first["state_mutated"] is False
    assert first["provider_called"] is False
    db.close()


def test_active_revision_identity_is_authorizable():
    db = _db()
    draft, creative, revision = _seed_revision(db)

    result = autonomous.autonomous_content_policy(db, draft.id)

    assert result["ready"] is True
    assert result["selected_identity"]["revision_id"] == revision.id
    assert result["selected_identity"]["creative_id"] == creative.id
    assert result["selected_identity"]["approved_version_id"] == revision.id
    db.close()


def test_multiple_rendered_creatives_block_instead_of_arbitrary_selection():
    db = _db()
    draft, _ = _seed_original(db, creative_count=2)

    result = autonomous.autonomous_content_policy(db, draft.id)

    assert result["ready"] is False
    assert "IDENTITY_UNAMBIGUOUS" in result["blockers"]
    identity_check = next(c for c in result["checks"] if c["code"] == "IDENTITY_UNAMBIGUOUS")
    assert identity_check["context"]["identity_blockers"] == ["CREATIVE_IDENTITY_AMBIGUOUS"]
    db.close()


def test_unrendered_or_incomplete_creative_blocks():
    db = _db()
    draft, _ = _seed_original(db, render_status="PENDING")

    result = autonomous.autonomous_content_policy(db, draft.id)

    assert result["ready"] is False
    assert "IDENTITY_UNAMBIGUOUS" in result["blockers"]
    db.close()


def test_unsupported_claims_block_machine_authorization():
    db = _db()
    draft, _ = _seed_original(db, unsupported=["best fragrance"])

    result = autonomous.autonomous_content_policy(db, draft.id)

    assert result["ready"] is False
    assert "ZERO_UNSUPPORTED_CLAIMS" in result["blockers"]
    assert result["diagnostics"]["unsupported_claim_count"] == 1
    db.close()


def test_revision_creative_mismatch_blocks():
    db = _db()
    draft, _, revision = _seed_revision(db)
    other, other_creatives = _seed_original(db, draft_id="draft-2")
    revision.creative_id = other_creatives[0].id
    db.commit()

    result = autonomous.autonomous_content_policy(db, draft.id)

    assert result["ready"] is False
    assert "IDENTITY_UNAMBIGUOUS" in result["blockers"]
    db.close()


def test_prior_manual_decision_blocks_machine_authorization():
    db = _db()
    draft, creatives = _seed_original(db)
    db.add(PinApproval(
        id="manual-decision",
        draft_id=draft.id,
        creative_id=creatives[0].id,
        approved_version_id="original",
        decision="REJECTED",
        decided_by="manual_dashboard_action",
    ))
    db.commit()

    result = autonomous.autonomous_content_policy(db, draft.id)

    assert result["ready"] is False
    assert "NO_PRIOR_DECISION" in result["blockers"]
    db.close()


def test_authorization_is_disabled_by_default():
    db = _db()
    draft, _ = _seed_original(db)

    with pytest.raises(autonomous.AutonomousAuthorizationError, match="AUTONOMOUS_AUTHORIZATION_DISABLED"):
        autonomous.authorize_draft_autonomously(db, draft.id, settings=_settings())

    assert db.get(PinDraft, draft.id).status == DraftStatus.READY_FOR_REVIEW
    assert db.scalar(select(PinApproval).where(PinApproval.draft_id == draft.id)) is None
    db.close()


def test_machine_authorization_persists_auditable_identity_and_is_idempotent():
    db = _db()
    draft, creatives = _seed_original(db)
    settings = _settings(routine_autonomous_authorization_enabled=True)

    first = autonomous.authorize_draft_autonomously(db, draft.id, settings=settings, now=NOW)
    second = autonomous.authorize_draft_autonomously(db, draft.id, settings=settings, now=NOW)

    assert first.id == second.id
    assert first.decided_by == autonomous.AUTONOMOUS_ACTOR
    assert first.decision == "APPROVED"
    assert first.creative_id == creatives[0].id
    assert first.approved_version_id == "original"
    assert first.note.startswith(autonomous.AUTONOMOUS_NOTE_PREFIX)
    assert db.get(PinDraft, draft.id).status == DraftStatus.APPROVED
    approvals = list(db.scalars(select(PinApproval).where(PinApproval.draft_id == draft.id)).all())
    assert len(approvals) == 1
    db.close()


def test_auto_permit_requires_machine_approval(monkeypatch):
    db = _db()
    draft, creatives = _seed_original(db)
    manual = PinApproval(
        id="manual-approved",
        draft_id=draft.id,
        creative_id=creatives[0].id,
        approved_version_id="original",
        decision="APPROVED",
        decided_by="manual_dashboard_action",
    )
    draft.status = DraftStatus.APPROVED
    db.add(manual)
    db.commit()
    publication = _scheduled_publication(db, draft, creatives[0], manual)

    with pytest.raises(autonomous.AutonomousAuthorizationError, match="AUTONOMOUS_APPROVAL_REQUIRED"):
        autonomous.auto_permit_publication(
            db,
            publication.id,
            settings=_settings(routine_autonomous_authorization_enabled=True),
            now=NOW,
        )
    db.close()


def test_auto_permit_reuses_existing_permit_contract(monkeypatch):
    db = _db()
    draft, creatives = _seed_original(db)
    approval = _machine_approval(db, draft, creatives[0])
    publication = _scheduled_publication(db, draft, creatives[0], approval)
    called = {"create": 0}

    def fake_create_permit(db_arg, publication_arg, *, actor, now):
        called["create"] += 1
        permit = RoutineDispatchPermit(
            id="permit-auto-1",
            publication_id=publication_arg.id,
            dispatch_provider="buffer",
            approval_id=approval.id,
            pinterest_board_record_id="board-1",
            publication_fingerprint=publication_arg.publication_fingerprint,
            request_fingerprint="r" * 64,
            scheduled_for_snapshot=publication_arg.scheduled_for,
            quality_policy_version="PINTEREST_QUALITY_V1",
            quality_snapshot={},
            duplicate_snapshot={},
            readiness_snapshot={"dispatch_provider": "buffer"},
            authorized_by=actor,
            authorized_at=now,
            expires_at=now + timedelta(hours=1),
            status="ACTIVE",
        )
        db_arg.add(permit)
        db_arg.commit()
        return permit

    monkeypatch.setattr(autonomous, "create_permit", fake_create_permit)
    monkeypatch.setattr(autonomous, "active_permit", lambda *_args, **_kwargs: None)

    permit = autonomous.auto_permit_publication(
        db,
        publication.id,
        settings=_settings(routine_autonomous_authorization_enabled=True),
        now=NOW,
    )

    assert called["create"] == 1
    assert permit.authorized_by == autonomous.AUTONOMOUS_ACTOR
    assert permit.publication_id == publication.id
    db.close()


def test_existing_valid_autonomous_permit_is_idempotent(monkeypatch):
    db = _db()
    draft, creatives = _seed_original(db)
    approval = _machine_approval(db, draft, creatives[0])
    publication = _scheduled_publication(db, draft, creatives[0], approval)
    permit = RoutineDispatchPermit(
        id="permit-existing",
        publication_id=publication.id,
        dispatch_provider="buffer",
        approval_id=approval.id,
        pinterest_board_record_id="board-1",
        publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint="r" * 64,
        scheduled_for_snapshot=publication.scheduled_for,
        quality_policy_version="PINTEREST_QUALITY_V1",
        quality_snapshot={},
        duplicate_snapshot={},
        readiness_snapshot={"dispatch_provider": "buffer"},
        authorized_by=autonomous.AUTONOMOUS_ACTOR,
        authorized_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status="ACTIVE",
    )
    db.add(permit)
    db.commit()

    monkeypatch.setattr(autonomous, "active_permit", lambda *_args, **_kwargs: permit)
    monkeypatch.setattr(autonomous, "validate_permit", lambda *_args, **_kwargs: {"valid": True, "status": "ACTIVE"})
    monkeypatch.setattr(
        autonomous,
        "create_permit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not create a second permit")),
    )

    returned = autonomous.auto_permit_publication(
        db,
        publication.id,
        settings=_settings(routine_autonomous_authorization_enabled=True),
        now=NOW,
    )
    assert returned.id == permit.id
    db.close()


def test_invalid_active_permit_blocks_without_replacement(monkeypatch):
    db = _db()
    draft, creatives = _seed_original(db)
    approval = _machine_approval(db, draft, creatives[0])
    publication = _scheduled_publication(db, draft, creatives[0], approval)
    permit = RoutineDispatchPermit(
        id="permit-drifted",
        publication_id=publication.id,
        dispatch_provider="buffer",
        approval_id=approval.id,
        pinterest_board_record_id="board-1",
        publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint="r" * 64,
        scheduled_for_snapshot=publication.scheduled_for,
        quality_policy_version="PINTEREST_QUALITY_V1",
        quality_snapshot={},
        duplicate_snapshot={},
        readiness_snapshot={"dispatch_provider": "buffer"},
        authorized_by=autonomous.AUTONOMOUS_ACTOR,
        authorized_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status="ACTIVE",
    )
    db.add(permit)
    db.commit()

    monkeypatch.setattr(autonomous, "active_permit", lambda *_args, **_kwargs: permit)
    monkeypatch.setattr(
        autonomous,
        "validate_permit",
        lambda *_args, **_kwargs: {"valid": False, "status": "ROUTINE_PERMIT_SNAPSHOT_DRIFT"},
    )
    monkeypatch.setattr(
        autonomous,
        "create_permit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not replace drifted permit")),
    )

    with pytest.raises(
        autonomous.AutonomousAuthorizationError,
        match="AUTONOMOUS_ACTIVE_PERMIT_INVALID:ROUTINE_PERMIT_SNAPSHOT_DRIFT",
    ):
        autonomous.auto_permit_publication(
            db,
            publication.id,
            settings=_settings(routine_autonomous_authorization_enabled=True),
            now=NOW,
        )
    db.close()


def test_readiness_endpoint_payload_exposes_no_provider_action_or_secret():
    db = _db()
    draft, _ = _seed_original(db)
    result = autonomous.autonomous_authorization_status(
        db,
        draft.id,
        settings=_settings(buffer_api_key="super-secret"),
    )
    assert result["enabled"] is False
    assert result["ready"] is True
    assert result["provider_called"] is False
    assert "super-secret" not in repr(result)
    db.close()


def test_existing_machine_approval_fails_closed_on_creative_drift():
    db = _db()
    draft, creatives = _seed_original(db)
    approval = _machine_approval(db, draft, creatives[0])

    creatives[0].sha256 = "e" * 64
    db.commit()

    result = autonomous.autonomous_content_policy(db, draft.id)
    assert result["already_authorized"] is True
    assert result["ready"] is False
    assert result["blockers"] == ["AUTONOMOUS_APPROVAL_DRIFT"]

    publication = _scheduled_publication(db, draft, creatives[0], approval)
    with pytest.raises(autonomous.AutonomousAuthorizationError, match="AUTONOMOUS_APPROVAL_DRIFT"):
        autonomous.auto_permit_publication(
            db,
            publication.id,
            settings=_settings(routine_autonomous_authorization_enabled=True),
            now=NOW,
        )
    db.close()
