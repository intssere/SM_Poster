from sqlalchemy import select

from app.models.domain import (
    Board,
    ContentRevision,
    ContentVersionSelection,
    PinApproval,
    PinCreative,
    PinDraft,
    PinPublication,
    ProductImage,
    PublicationStatus,
)
from app.services.publication_identity import (
    PublicationIdentityError,
    PublicationIdentityService,
)
from test_pin_proposals import add_product, add_review_creative, setup_service


def _prepared(suffix="identity"):
    db, store, proposals = setup_service()
    add_product(db, store, suffix=suffix)
    report = proposals.generate_controlled_batch(product_limit=1, max_proposals_per_product=1)
    draft_id = report["representative_proposals"][0]["id"]
    creative = add_review_creative(db, draft_id, suffix=f"creative-{suffix}")
    return db, proposals, db.get(PinDraft, draft_id), creative


def _revision(db, draft, creative, version=2):
    image = db.get(ProductImage, creative.source_image_id)
    revision = ContentRevision(
        draft_id=draft.id,
        version=version,
        revision_kind="COPY",
        status="REVIEW",
        headline=f"Revision {version}",
        title=f"Revision {version} title",
        description=f"Revision {version} description",
        alt_text=f"Revision {version} alt",
        cta="Shop now",
        content_angle="Product Pick",
        content_angle_key="product_pick",
        creative_template="Product classification",
        creative_template_key="product_classification",
        destination_url=f"https://diamondshelf.test/revision-{version}",
        utm_url=f"https://diamondshelf.test/revision-{version}?utm_source=pinterest",
        keywords=[],
        facts_used={},
        warnings=[],
        missing_facts=[],
        unsupported_claims=[],
        provenance={"source": "test_fixture"},
        text_fingerprint=f"text-{version}".ljust(64, "0")[:64],
        creative_fingerprint=creative.creative_fingerprint,
        creative_id=creative.id,
        source_image_id=image.id,
        provider_mode="disabled",
        generation_mode="deterministic_fixture",
        reason="identity_test",
    )
    db.add(revision)
    db.commit()
    return revision


def _activate(db, draft, revision):
    selection = ContentVersionSelection(
        draft_id=draft.id,
        revision_id=revision.id,
        selected_by="identity_test",
    )
    db.add(selection)
    db.commit()
    return selection


def test_approval_binds_exact_revision_and_creative_and_is_immutable_after_selection_change():
    db, proposals, draft, creative = _prepared("approval")
    revision2 = _revision(db, draft, creative, 2)
    selection = _activate(db, draft, revision2)

    proposals.decide(
        draft.id,
        "APPROVED",
        "Reviewed exact revision.",
        reviewed_creative_id=creative.id,
    )
    approval = db.scalar(select(PinApproval).where(PinApproval.draft_id == draft.id))
    assert approval.revision_id == revision2.id
    assert approval.creative_id == creative.id
    assert approval.approved_version_id == revision2.id

    revision3 = _revision(db, draft, creative, 3)
    selection.revision_id = revision3.id
    db.commit()
    db.refresh(approval)
    assert approval.revision_id == revision2.id
    assert approval.approved_version_id == revision2.id
    db.close()


def test_publication_snapshot_keeps_exact_identity_when_proposal_state_changes():
    db, proposals, draft, creative = _prepared("snapshot")
    revision = _revision(db, draft, creative, 2)
    _activate(db, draft, revision)
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.scalar(select(PinApproval).where(PinApproval.draft_id == draft.id))
    board = db.scalar(select(Board).where(Board.id.is_not(None)))

    publication = PublicationIdentityService(proposals.session_factory).create_snapshot(
        approval_id=approval.id,
        board_id=board.id,
    )
    snapshot = {
        "revision_id": publication.revision_id,
        "creative_id": publication.creative_id,
        "approval_id": publication.approval_id,
        "source_image_id": publication.source_image_id,
        "template_key": publication.template_key,
        "template_version": publication.template_version,
        "text_fingerprint": publication.text_fingerprint,
        "creative_fingerprint": publication.creative_fingerprint,
        "destination_url": publication.destination_url,
        "utm_url": publication.utm_url,
        "pinterest_board_id": publication.pinterest_board_id,
    }

    revision.destination_url = "https://diamondshelf.test/changed"
    creative.creative_fingerprint = "changed".ljust(64, "0")
    board.pinterest_board_id = "changed-board"
    db.commit()
    publication = db.get(PinPublication, publication.id)
    assert snapshot == {key: getattr(publication, key) for key in snapshot}
    assert publication.pinterest_pin_id is None
    assert publication.provider_response == {}
    db.close()


def test_duplicate_snapshot_and_mismatched_revision_creative_fail_closed():
    db, proposals, draft, creative = _prepared("duplicate")
    revision = _revision(db, draft, creative, 2)
    _activate(db, draft, revision)
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.scalar(select(PinApproval).where(PinApproval.draft_id == draft.id))
    board = db.scalar(select(Board).where(Board.id.is_not(None)))
    identities = PublicationIdentityService(proposals.session_factory)
    identities.create_snapshot(approval_id=approval.id, board_id=board.id)
    try:
        identities.create_snapshot(approval_id=approval.id, board_id=board.id)
    except PublicationIdentityError as exc:
        assert "identical publication snapshot" in str(exc)
    else:
        raise AssertionError("Duplicate publication identity must fail closed")

    second_approval = PinApproval(
        draft_id=draft.id,
        revision_id=revision.id,
        creative_id=creative.id,
        approved_version_id=revision.id,
        decision="APPROVED",
        decided_by="second_review_event",
    )
    db.add(second_approval)
    db.commit()
    try:
        identities.create_snapshot(approval_id=second_approval.id, board_id=board.id)
    except PublicationIdentityError as exc:
        assert "identical publication snapshot" in str(exc)
    else:
        raise AssertionError("A new approval event must not bypass publication deduplication")

    db2, proposals2, other_draft, other_creative = _prepared("mismatch-other")
    other_revision = _revision(db2, other_draft, other_creative, 2)
    bad = PinApproval(
        draft_id=other_draft.id,
        revision_id=other_revision.id,
        creative_id=creative.id,
        approved_version_id=other_revision.id,
        decision="APPROVED",
        decided_by="identity_test",
    )
    db2.add(bad)
    db2.commit()
    other_board = db2.scalar(select(Board).where(Board.id.is_not(None)))
    try:
        PublicationIdentityService(proposals2.session_factory).create_snapshot(
            approval_id=bad.id, board_id=other_board.id
        )
    except PublicationIdentityError as exc:
        assert "incomplete or mismatched" in str(exc)
    else:
        raise AssertionError("Mismatched revision/creative must fail closed")
    db.close()
    db2.close()


def test_approval_requires_explicit_matching_reviewed_creative_identity():
    db, proposals, draft, creative = _prepared("explicit-creative")

    for reviewed_creative_id in (None, "not-the-reviewed-creative"):
        try:
            proposals.decide(
                draft.id,
                "APPROVED",
                reviewed_creative_id=reviewed_creative_id,
            )
        except ValueError as exc:
            assert "creative identity" in str(exc)
        else:
            raise AssertionError("Approval must fail without the exact reviewed creative ID")
        db.refresh(draft)
        assert draft.status.value == "READY_FOR_REVIEW"
        assert db.scalar(select(PinApproval).where(PinApproval.draft_id == draft.id)) is None

    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    approval = db.scalar(select(PinApproval).where(PinApproval.draft_id == draft.id))
    assert approval.creative_id == creative.id
    db.close()


def test_historical_nullable_identity_records_remain_readable():
    db, proposals, draft, creative = _prepared("historical")
    board = db.scalar(select(Board).where(Board.id.is_not(None)))
    approval = PinApproval(
        draft_id=draft.id,
        decision="REJECTED",
        decided_by="historical_import",
    )
    db.add(approval)
    db.flush()
    publication = PinPublication(
        draft_id=draft.id,
        creative_id=creative.id,
        board_id=board.id,
        publication_fingerprint="historical".ljust(64, "0"),
        status=PublicationStatus.CANCELLED,
    )
    db.add(publication)
    db.commit()
    db.expire_all()
    assert db.get(PinApproval, approval.id).revision_id is None
    loaded = db.get(PinPublication, publication.id)
    assert loaded.approval_id is None
    assert loaded.destination_url is None
    db.close()
