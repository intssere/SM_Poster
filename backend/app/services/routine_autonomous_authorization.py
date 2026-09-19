from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.models.domain import (
    AuditLog,
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
from app.services.routine_dispatch_authorization import (
    active_permit,
    create_permit,
    validate_permit,
)

AUTONOMOUS_POLICY_VERSION = "AUTONOMOUS_POLICY_V1"
AUTONOMOUS_ACTOR = "autonomous-policy-v1"
AUTONOMOUS_NOTE_PREFIX = f"{AUTONOMOUS_POLICY_VERSION}:"


class AutonomousAuthorizationError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check(code: str, passed: bool, message: str, **context) -> dict:
    return {
        "code": code,
        "passed": bool(passed),
        "blocking": not bool(passed),
        "message": message,
        "context": context,
    }


def _policy_fingerprint(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _existing_machine_approval(db, draft_id: str):
    return db.scalar(
        select(PinApproval)
        .where(
            PinApproval.draft_id == draft_id,
            PinApproval.decision == "APPROVED",
            PinApproval.decided_by == AUTONOMOUS_ACTOR,
        )
        .order_by(PinApproval.created_at.desc(), PinApproval.id)
        .limit(1)
    )


def _prior_decision_count(db, draft_id: str) -> int:
    return len(list(db.scalars(
        select(PinApproval.id).where(PinApproval.draft_id == draft_id)
    ).all()))


def _resolve_autonomous_identity(db, draft: PinDraft) -> dict:
    selection = db.scalar(
        select(ContentVersionSelection)
        .where(ContentVersionSelection.draft_id == draft.id)
    )

    revision = None
    version_id = "original"
    creative = None
    blockers: list[str] = []

    if selection is not None:
        revision = db.get(ContentRevision, selection.revision_id)
        if (
            revision is None
            or revision.draft_id != draft.id
            or revision.status != "REVIEW"
        ):
            blockers.append("ACTIVE_REVISION_INVALID")
        elif not revision.creative_id:
            blockers.append("ACTIVE_REVISION_CREATIVE_REQUIRED")
        else:
            creative = db.get(PinCreative, revision.creative_id)
            version_id = revision.id
            if creative is None or creative.draft_id != draft.id:
                blockers.append("REVISION_CREATIVE_MISMATCH")
    else:
        rendered = list(db.scalars(
            select(PinCreative)
            .where(
                PinCreative.draft_id == draft.id,
                PinCreative.render_status == "RENDERED",
            )
            .order_by(PinCreative.created_at, PinCreative.id)
        ).all())
        if not rendered:
            blockers.append("RENDERED_CREATIVE_REQUIRED")
        elif len(rendered) != 1:
            blockers.append("CREATIVE_IDENTITY_AMBIGUOUS")
        else:
            creative = rendered[0]

    return {
        "revision": revision,
        "creative": creative,
        "version_id": version_id,
        "identity_blockers": blockers,
    }


def autonomous_content_policy(db, draft_id: str) -> dict:
    draft = db.get(PinDraft, draft_id)
    if draft is None:
        return {
            "policy_version": AUTONOMOUS_POLICY_VERSION,
            "ready": False,
            "already_authorized": False,
            "draft_id": draft_id,
            "blockers": ["DRAFT_NOT_FOUND"],
            "checks": [
                _check("DRAFT_EXISTS", False, "Draft must exist."),
            ],
            "policy_fingerprint": None,
            "selected_identity": None,
            "state_mutated": False,
            "provider_called": False,
        }

    existing_machine = _existing_machine_approval(db, draft.id)
    prior_count = _prior_decision_count(db, draft.id)
    publication_count = len(list(db.scalars(
        select(PinPublication.id).where(PinPublication.draft_id == draft.id)
    ).all()))

    if existing_machine is not None:
        revision = db.get(ContentRevision, existing_machine.revision_id) if existing_machine.revision_id else None
        creative = db.get(PinCreative, existing_machine.creative_id) if existing_machine.creative_id else None
        version_id = existing_machine.approved_version_id or ("original" if revision is None else revision.id)
        note = existing_machine.note or ""
        fingerprint = note[len(AUTONOMOUS_NOTE_PREFIX):] if note.startswith(AUTONOMOUS_NOTE_PREFIX) else None
        concept = db.get(PinConcept, draft.concept_id)
        source = revision if revision is not None else draft
        unsupported_claims = (
            list(revision.unsupported_claims or [])
            if revision is not None
            else list((concept.rationale or {}).get("unsupported_claims") or []) if concept else []
        )
        creative_complete = bool(
            creative
            and creative.draft_id == draft.id
            and creative.render_status == "RENDERED"
            and creative.rendered_url
            and creative.sha256
            and creative.creative_fingerprint
            and creative.source_image_id
            and creative.template_id
            and creative.width == 1000
            and creative.height == 1500
        )
        content_complete = bool(
            concept
            and source
            and source.title
            and source.description
            and source.alt_text
            and source.destination_url
            and source.utm_url
            and source.text_fingerprint
        )
        binding_ok = bool(
            draft.status == DraftStatus.APPROVED
            and creative_complete
            and content_complete
            and len(unsupported_claims) == 0
            and (
                (revision is None and existing_machine.revision_id is None and version_id == "original")
                or (
                    revision
                    and revision.draft_id == draft.id
                    and existing_machine.revision_id == revision.id
                    and version_id == revision.id
                    and revision.status == "REVIEW"
                    and revision.creative_id == creative.id
                )
            )
        )
        expected_payload = {
            "policy_version": AUTONOMOUS_POLICY_VERSION,
            "draft_id": draft.id,
            "concept_id": draft.concept_id,
            "revision_id": revision.id if revision else None,
            "creative_id": creative.id if creative else None,
            "approved_version_id": version_id,
            "text_fingerprint": source.text_fingerprint if source else None,
            "creative_fingerprint": creative.creative_fingerprint if creative else None,
            "creative_sha256": creative.sha256 if creative else None,
            "source_image_id": creative.source_image_id if creative else None,
            "template_id": creative.template_id if creative else None,
            "unsupported_claims": sorted(str(item) for item in unsupported_claims),
        }
        expected_fingerprint = _policy_fingerprint(expected_payload) if binding_ok else None
        identity_ok = bool(binding_ok and fingerprint and fingerprint == expected_fingerprint)
        return {
            "policy_version": AUTONOMOUS_POLICY_VERSION,
            "ready": identity_ok,
            "already_authorized": True,
            "draft_id": draft.id,
            "blockers": [] if identity_ok else ["AUTONOMOUS_APPROVAL_DRIFT"],
            "checks": [
                _check(
                    "AUTONOMOUS_APPROVAL_STILL_BOUND",
                    identity_ok,
                    "Existing autonomous approval must remain bound to the same immutable content and creative identity.",
                )
            ],
            "policy_fingerprint": fingerprint,
            "selected_identity": {
                "revision_id": revision.id if revision else None,
                "creative_id": creative.id if creative else None,
                "approved_version_id": version_id,
                "approval_id": existing_machine.id,
            },
            "state_mutated": False,
            "provider_called": False,
        }

    identity = _resolve_autonomous_identity(db, draft)
    revision = identity["revision"]
    creative = identity["creative"]
    concept = db.get(PinConcept, draft.concept_id)

    source = revision if revision is not None else draft
    unsupported_claims = (
        list(revision.unsupported_claims or [])
        if revision is not None
        else list((concept.rationale or {}).get("unsupported_claims") or []) if concept else []
    )
    warnings = (
        list(revision.warnings or [])
        if revision is not None
        else list((concept.rationale or {}).get("warnings") or []) if concept else []
    )
    missing_facts = (
        list(revision.missing_facts or [])
        if revision is not None
        else list((concept.rationale or {}).get("missing_facts") or []) if concept else []
    )

    content_complete = bool(
        concept
        and source
        and source.title
        and source.description
        and source.alt_text
        and source.destination_url
        and source.utm_url
        and source.text_fingerprint
    )
    creative_complete = bool(
        creative
        and creative.draft_id == draft.id
        and creative.render_status == "RENDERED"
        and creative.rendered_url
        and creative.sha256
        and creative.creative_fingerprint
        and creative.source_image_id
        and creative.template_id
        and creative.width == 1000
        and creative.height == 1500
    )
    revision_binding = bool(
        revision is None
        or (
            revision.draft_id == draft.id
            and revision.status == "REVIEW"
            and revision.creative_id == (creative.id if creative else None)
        )
    )

    checks = [
        _check(
            "DRAFT_READY_FOR_REVIEW",
            draft.status == DraftStatus.READY_FOR_REVIEW,
            "Autonomous policy only evaluates drafts in READY_FOR_REVIEW.",
            current_status=draft.status.value if hasattr(draft.status, "value") else str(draft.status),
        ),
        _check(
            "NO_PRIOR_DECISION",
            prior_count == 0,
            "Draft must not have a prior manual or conflicting approval decision.",
            prior_decision_count=prior_count,
        ),
        _check(
            "NO_EXISTING_PUBLICATION",
            publication_count == 0,
            "Draft must not already have a publication snapshot.",
            publication_count=publication_count,
        ),
        _check(
            "IDENTITY_UNAMBIGUOUS",
            not identity["identity_blockers"],
            "Active revision/creative identity must be unambiguous.",
            identity_blockers=identity["identity_blockers"],
        ),
        _check(
            "CONTENT_SNAPSHOT_COMPLETE",
            content_complete,
            "Draft or active revision must contain complete publish-relevant content.",
        ),
        _check(
            "CREATIVE_RENDER_COMPLETE",
            creative_complete,
            "Selected creative must be a complete 1000x1500 rendered creative with immutable provenance.",
        ),
        _check(
            "REVISION_CREATIVE_BOUND",
            revision_binding,
            "Active revision and selected creative must be bound to one another.",
        ),
        _check(
            "ZERO_UNSUPPORTED_CLAIMS",
            len(unsupported_claims) == 0,
            "Autonomous policy does not authorize unsupported claims.",
            unsupported_claim_count=len(unsupported_claims),
        ),
    ]

    blockers = [check["code"] for check in checks if check["blocking"]]
    selected_identity = {
        "revision_id": revision.id if revision else None,
        "creative_id": creative.id if creative else None,
        "approved_version_id": identity["version_id"],
    }
    safe_payload = {
        "policy_version": AUTONOMOUS_POLICY_VERSION,
        "draft_id": draft.id,
        "concept_id": draft.concept_id,
        "revision_id": selected_identity["revision_id"],
        "creative_id": selected_identity["creative_id"],
        "approved_version_id": selected_identity["approved_version_id"],
        "text_fingerprint": source.text_fingerprint if source else None,
        "creative_fingerprint": creative.creative_fingerprint if creative else None,
        "creative_sha256": creative.sha256 if creative else None,
        "source_image_id": creative.source_image_id if creative else None,
        "template_id": creative.template_id if creative else None,
        "unsupported_claims": sorted(str(item) for item in unsupported_claims),
    }
    fingerprint = _policy_fingerprint(safe_payload) if not blockers else None

    return {
        "policy_version": AUTONOMOUS_POLICY_VERSION,
        "ready": not blockers,
        "already_authorized": False,
        "draft_id": draft.id,
        "blockers": blockers,
        "checks": checks,
        "policy_fingerprint": fingerprint,
        "selected_identity": selected_identity,
        "diagnostics": {
            "warning_count": len(warnings),
            "missing_fact_count": len(missing_facts),
            "unsupported_claim_count": len(unsupported_claims),
        },
        "state_mutated": False,
        "provider_called": False,
    }


def authorize_draft_autonomously(
    db,
    draft_id: str,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
):
    settings = settings or get_settings()
    if settings.routine_autonomous_authorization_enabled is not True:
        raise AutonomousAuthorizationError("AUTONOMOUS_AUTHORIZATION_DISABLED")

    snapshot = autonomous_content_policy(db, draft_id)
    if snapshot.get("already_authorized") and snapshot.get("ready"):
        approval_id = snapshot["selected_identity"]["approval_id"]
        approval = db.get(PinApproval, approval_id)
        if approval is None:
            raise AutonomousAuthorizationError("AUTONOMOUS_APPROVAL_DRIFT")
        return approval

    if snapshot.get("ready") is not True:
        blocker = (snapshot.get("blockers") or ["AUTONOMOUS_POLICY_BLOCKED"])[0]
        raise AutonomousAuthorizationError(blocker)

    draft = db.scalar(
        select(PinDraft)
        .where(PinDraft.id == draft_id)
        .with_for_update()
    )
    if draft is None:
        raise AutonomousAuthorizationError("DRAFT_NOT_FOUND")

    # Re-evaluate after acquiring the row lock so identity/policy drift cannot
    # occur between evaluation and persistence.
    locked_snapshot = autonomous_content_policy(db, draft_id)
    if locked_snapshot.get("ready") is not True or locked_snapshot.get("already_authorized"):
        if locked_snapshot.get("already_authorized") and locked_snapshot.get("ready"):
            return db.get(PinApproval, locked_snapshot["selected_identity"]["approval_id"])
        blocker = (locked_snapshot.get("blockers") or ["AUTONOMOUS_POLICY_BLOCKED"])[0]
        raise AutonomousAuthorizationError(blocker)

    identity = locked_snapshot["selected_identity"]
    approval = PinApproval(
        draft_id=draft.id,
        revision_id=identity["revision_id"],
        creative_id=identity["creative_id"],
        approved_version_id=identity["approved_version_id"],
        decision="APPROVED",
        decided_by=AUTONOMOUS_ACTOR,
        note=f"{AUTONOMOUS_NOTE_PREFIX}{locked_snapshot['policy_fingerprint']}",
    )
    draft.status = DraftStatus.APPROVED
    db.add(approval)
    db.flush()
    db.add(AuditLog(
        actor=AUTONOMOUS_ACTOR,
        action="AUTONOMOUS_CONTENT_AUTHORIZED",
        entity_type="PinDraft",
        entity_id=draft.id,
        metadata_json={
            "policy_version": AUTONOMOUS_POLICY_VERSION,
            "policy_fingerprint": locked_snapshot["policy_fingerprint"],
            "approval_id": approval.id,
            "revision_id": approval.revision_id,
            "creative_id": approval.creative_id,
            "approved_version_id": approval.approved_version_id,
        },
    ))
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(approval)
    return approval


def auto_permit_publication(
    db,
    publication_id: str,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
):
    settings = settings or get_settings()
    if settings.routine_autonomous_authorization_enabled is not True:
        raise AutonomousAuthorizationError("AUTONOMOUS_AUTHORIZATION_DISABLED")

    publication = db.get(PinPublication, publication_id)
    if publication is None:
        raise AutonomousAuthorizationError("PUBLICATION_NOT_FOUND")
    if publication.status != PublicationStatus.SCHEDULED or publication.scheduled_for is None:
        raise AutonomousAuthorizationError("PUBLICATION_NOT_SCHEDULED")

    approval = db.get(PinApproval, publication.approval_id) if publication.approval_id else None
    if (
        approval is None
        or approval.decision != "APPROVED"
        or approval.decided_by != AUTONOMOUS_ACTOR
        or not (approval.note or "").startswith(AUTONOMOUS_NOTE_PREFIX)
        or approval.draft_id != publication.draft_id
        or approval.revision_id != publication.revision_id
        or approval.creative_id != publication.creative_id
    ):
        raise AutonomousAuthorizationError("AUTONOMOUS_APPROVAL_REQUIRED")

    policy = autonomous_content_policy(db, publication.draft_id)
    if (
        policy.get("ready") is not True
        or policy.get("already_authorized") is not True
        or policy.get("selected_identity", {}).get("approval_id") != approval.id
    ):
        raise AutonomousAuthorizationError("AUTONOMOUS_APPROVAL_DRIFT")

    now = now or _now()
    permit = active_permit(db, publication.id)
    if permit is not None:
        validated = validate_permit(db, publication, permit, now=now, require_due=False)
        if validated.get("valid") is not True:
            raise AutonomousAuthorizationError(
                f"AUTONOMOUS_ACTIVE_PERMIT_INVALID:{validated.get('status') or 'UNKNOWN'}"
            )
        if permit.authorized_by != AUTONOMOUS_ACTOR:
            raise AutonomousAuthorizationError("AUTONOMOUS_ACTIVE_PERMIT_CONFLICT")
        return permit

    try:
        permit = create_permit(
            db,
            publication,
            actor=AUTONOMOUS_ACTOR,
            now=now,
        )
    except Exception as exc:
        code = str(exc) or exc.__class__.__name__
        raise AutonomousAuthorizationError(code) from None

    db.add(AuditLog(
        actor=AUTONOMOUS_ACTOR,
        action="AUTONOMOUS_ROUTINE_PERMIT_CREATED",
        entity_type="RoutineDispatchPermit",
        entity_id=permit.id,
        metadata_json={
            "policy_version": AUTONOMOUS_POLICY_VERSION,
            "publication_id": publication.id,
            "approval_id": approval.id,
            "publication_fingerprint": publication.publication_fingerprint,
        },
    ))
    db.commit()
    db.refresh(permit)
    return permit


def autonomous_authorization_status(db, draft_id: str, *, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    result = autonomous_content_policy(db, draft_id)
    return {
        "enabled": settings.routine_autonomous_authorization_enabled,
        **result,
    }
