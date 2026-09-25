"""Explicit exact-post observation only. No polling, mutation or automatic retry."""
from datetime import datetime, timezone
import re
from urllib.parse import urlsplit

from sqlalchemy import select, update
from app.core.config import get_settings
from app.integrations.buffer.gateway import BufferGateway, public_url, BufferReadError, BufferConfigurationError
from app.models.domain import (
    Board,
    ContentRevision,
    CreativeTemplate,
    PinApproval,
    PinConcept,
    PinCreative,
    PinDraft,
    PinPublication,
    PinterestBoard,
    PinterestConnection,
    PublicationAttempt,
    PublicationReconciliationEvent,
    PublicationStatus,
)
from app.models.routine_publishing import RoutineDispatchPermit
from app.services.fingerprints import publication_identity_fingerprint
from app.services.publication_scheduler import request_fingerprint_for
from app.services.pinterest_publisher import PublicationReconciliationError
from app.services.routine_autonomous_authorization import (
    AUTONOMOUS_ACTOR,
    AUTONOMOUS_NOTE_PREFIX,
)


TASK58_17_MERGED_AT = datetime(2026, 9, 24, 7, 40, 8, tzinfo=timezone.utc)
HISTORICAL_MIXED_DESTINATION_MODE = "historical_mixed_modern_v1"
RECONCILIATION_RECEIPT_VERSION = "BUFFER_RECONCILIATION_RECEIPT_V1"
RECONCILIATION_RECEIPTS_KEY = "buffer_reconciliation_receipts"
RECONCILIATION_RECEIPT_TOTAL_KEY = "buffer_reconciliation_receipt_total"
RECONCILIATION_RECEIPT_HISTORY_LIMIT = 8


def _utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class BufferReconciliationError(RuntimeError):
    """A bounded failure; never contains a provider body or credential."""

    def __init__(self, code: str, *, stage: str | None = None, field: str | None = None):
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.field = field

    def safe_diagnostic(self) -> dict[str, str]:
        diagnostic = {"code": self.code}
        if self.stage:
            diagnostic["stage"] = self.stage
        if self.field:
            diagnostic["field"] = self.field
        return diagnostic


def pinterest_pin_id(link):
    try:
        scheme, host, port, path = public_url(link)
        parsed = urlsplit(link)
        match = re.fullmatch(r"/pin/([0-9]{1,80})/?", path)
        if host not in {"pinterest.com", "www.pinterest.com"} or port != 443 or parsed.query or parsed.fragment or not match:
            return None
        return match.group(1)
    except (ValueError, TypeError, BufferConfigurationError):
        return None


def snapshot_mismatch_field(publication, snapshot, settings) -> str | None:
    comparisons = (
        ("buffer_channel_id", settings.buffer_pinterest_channel_id, snapshot.channel_id),
        ("channel_service", "pinterest", snapshot.channel_service),
        ("description", publication.description_snapshot, snapshot.text),
        ("board_id", publication.pinterest_board_id_snapshot, snapshot.pinterest_board_service_id),
        ("title", publication.title_snapshot, snapshot.pinterest_title),
        ("pinterest_url", publication.utm_url, snapshot.pinterest_url),
        ("media_url", publication.media_url_snapshot, snapshot.image_url),
        ("alt_text", publication.alt_text_snapshot, snapshot.image_alt_text),
    )
    for field, expected, observed in comparisons:
        if not expected or expected != observed:
            return field
    return None


def snapshot_matches(publication, snapshot, settings):
    return snapshot_mismatch_field(publication, snapshot, settings) is None


def _legacy_board_identity_valid(db, publication):
    """Validate the immutable local Board/concept route for legacy publications."""
    board = db.get(Board, publication.board_id) if publication.board_id else None
    draft = db.get(PinDraft, publication.draft_id) if publication.draft_id else None
    concept = db.get(PinConcept, draft.concept_id) if draft else None
    return bool(
        board is not None
        and board.active
        and bool(board.pinterest_board_id)
        and bool(publication.pinterest_board_id_snapshot)
        and board.pinterest_board_id == publication.pinterest_board_id_snapshot
        and concept is not None
        and concept.board_id == publication.board_id
        and concept.store_id == board.store_id
    )


def _historical_legacy_route_valid(db, publication):
    """Validate the immutable local route for pre-58.17 mixed publications.

    Historical autonomous publications could carry the local Board identity
    before that Board had an external Pinterest ID. The local route itself must
    still be exact. If an external ID is present, it must match the immutable
    publication board snapshot; absence is tolerated only on this historical
    reconciliation compatibility path.
    """
    board = db.get(Board, publication.board_id) if publication.board_id else None
    draft = db.get(PinDraft, publication.draft_id) if publication.draft_id else None
    concept = db.get(PinConcept, draft.concept_id) if draft else None
    if (
        board is None
        or not board.active
        or not publication.pinterest_board_id_snapshot
        or concept is None
        or concept.board_id != publication.board_id
        or concept.store_id != board.store_id
    ):
        return False
    return bool(
        not board.pinterest_board_id
        or board.pinterest_board_id == publication.pinterest_board_id_snapshot
    )


def _modern_destination_lineage_valid(db, publication):
    connection = db.get(PinterestConnection, publication.pinterest_connection_id)
    board_record = db.get(PinterestBoard, publication.pinterest_board_record_id)
    return bool(
        connection is not None
        and board_record is not None
        and connection.provider == "pinterest"
        and connection.status == "CONNECTED"
        and board_record.connection_id == connection.id
        and board_record.is_active
        and board_record.is_eligible
        and board_record.last_synced_at is not None
        and connection.boards_last_synced_at is not None
        and board_record.last_synced_at == connection.boards_last_synced_at
        and bool(publication.pinterest_board_id_snapshot)
        and board_record.external_board_id == publication.pinterest_board_id_snapshot
    )


def _modern_destination_identity_valid(db, publication):
    return bool(
        publication.board_id is None
        and _modern_destination_lineage_valid(db, publication)
    )


def _historical_publication_fingerprint_valid(publication):
    required = (
        publication.draft_id,
        publication.creative_id,
        publication.source_image_id,
        publication.board_id,
        publication.pinterest_connection_id,
        publication.pinterest_board_record_id,
        publication.pinterest_board_id_snapshot,
        publication.destination_url,
        publication.utm_url,
        publication.publication_fingerprint,
    )
    if not all(required):
        return False
    expected = publication_identity_fingerprint(
        draft_id=publication.draft_id,
        revision_id=publication.revision_id,
        creative_id=publication.creative_id,
        source_image_id=publication.source_image_id,
        board_id=publication.board_id,
        integration_account_id=publication.integration_account_id,
        destination_url=publication.destination_url,
        utm_url=publication.utm_url,
        pinterest_connection_id=publication.pinterest_connection_id,
        pinterest_board_record_id=publication.pinterest_board_record_id,
        pinterest_board_id_snapshot=publication.pinterest_board_id_snapshot,
    )
    return publication.publication_fingerprint == expected


def _historical_mixed_destination_identity_valid(db, publication, attempt):
    """Permit only the pre-58.17 autonomous Phase C mixed-identity shape.

    This is reconciliation compatibility, never identity repair: the immutable
    publication and request fingerprints must already match the historical row.
    The sole routine permit must already be consumed, proving this path cannot
    authorize or redispatch another Buffer mutation.
    """
    if (
        publication.board_id is None
        or _utc(publication.created_at) is None
        or _utc(publication.created_at) >= TASK58_17_MERGED_AT
        or not publication.pinterest_connection_id
        or not publication.pinterest_board_record_id
        or publication.error_code != "BUFFER_SENT_LINK_UNVERIFIED"
        or attempt.error_code != "BUFFER_SENT_LINK_UNVERIFIED"
        or attempt.provider_operation_status != "sent"
        or not attempt.provider_operation_id
        or pinterest_pin_id(attempt.provider_external_link) is None
        or attempt.request_fingerprint != request_fingerprint_for(publication)
        or not _historical_legacy_route_valid(db, publication)
        or not _modern_destination_lineage_valid(db, publication)
        or not _historical_publication_fingerprint_valid(publication)
    ):
        return False

    approval = db.get(PinApproval, publication.approval_id) if publication.approval_id else None
    if (
        approval is None
        or approval.decided_by != AUTONOMOUS_ACTOR
        or not (approval.note or "").startswith(AUTONOMOUS_NOTE_PREFIX)
    ):
        return False

    permits = list(db.scalars(
        select(RoutineDispatchPermit).where(
            RoutineDispatchPermit.publication_id == publication.id
        )
    ).all())
    if len(permits) != 1:
        return False
    permit = permits[0]
    return bool(
        permit.status == "CONSUMED"
        and permit.consumed_at is not None
        and permit.dispatch_provider == "buffer"
        and permit.authorized_by == AUTONOMOUS_ACTOR
        and permit.approval_id == publication.approval_id
        and permit.pinterest_board_record_id == publication.pinterest_board_record_id
        and permit.publication_fingerprint == publication.publication_fingerprint
        and permit.request_fingerprint == attempt.request_fingerprint
        and permit.request_fingerprint == request_fingerprint_for(publication)
        and permit.scheduled_for_snapshot == publication.scheduled_for
    )


def _destination_identity_mode(db, publication, *, attempt=None):
    """Choose one exact destination mode; historical compatibility is explicit."""
    connection_id = publication.pinterest_connection_id
    board_record_id = publication.pinterest_board_record_id
    if bool(connection_id) != bool(board_record_id):
        return None
    if connection_id and board_record_id:
        if _modern_destination_identity_valid(db, publication):
            return "modern"
        if attempt is not None and _historical_mixed_destination_identity_valid(
            db, publication, attempt
        ):
            return HISTORICAL_MIXED_DESTINATION_MODE
        return None
    return "legacy" if _legacy_board_identity_valid(db, publication) else None


def _approved_content_identity_valid(db, publication, *, destination_mode):
    """Validate the exact approved content and creative identities behind the snapshot.

    Publications must resolve either the explicitly approved original draft or the
    exact concrete ContentRevision referenced by the immutable publication.
    """
    approval = db.get(PinApproval, publication.approval_id) if publication.approval_id else None
    draft = db.get(PinDraft, publication.draft_id) if publication.draft_id else None
    creative = db.get(PinCreative, publication.creative_id) if publication.creative_id else None
    template = db.get(CreativeTemplate, publication.template_id) if publication.template_id else None
    if not all((approval, draft, creative, template)):
        return False
    if (
        approval.decision != "APPROVED"
        or approval.draft_id != publication.draft_id
        or approval.creative_id != publication.creative_id
        or approval.revision_id != publication.revision_id
        or creative.draft_id != publication.draft_id
        or not publication.source_image_id
        or creative.source_image_id != publication.source_image_id
        or creative.template_id != publication.template_id
        or not publication.creative_fingerprint
        or creative.creative_fingerprint != publication.creative_fingerprint
        or template.key != publication.template_key
        or template.version != publication.template_version
        or not publication.media_url_snapshot
    ):
        return False

    if publication.revision_id is None:
        if approval.revision_id is not None or approval.approved_version_id != "original":
            return False
        source = draft
    else:
        revision = db.get(ContentRevision, publication.revision_id)
        if revision is None:
            return False
        if (
            revision.draft_id != publication.draft_id
            or approval.approved_version_id != revision.id
            or (revision.creative_id and revision.creative_id != publication.creative_id)
        ):
            return False
        source = revision

    return bool(
        publication.title_snapshot == source.title
        and publication.description_snapshot == source.description
        and publication.alt_text_snapshot == source.alt_text
        and publication.destination_url == source.destination_url
        and publication.utm_url == source.utm_url
        and publication.text_fingerprint == source.text_fingerprint
    )


def _entry(db, publication_id, settings, *, stage="pre_provider"):
    publication = db.get(PinPublication, publication_id, populate_existing=True)
    if not publication or publication.status != PublicationStatus.PUBLISH_UNKNOWN:
        raise BufferReconciliationError("RECONCILIATION_REQUIRES_PUBLISH_UNKNOWN")
    attempts = db.scalars(select(PublicationAttempt).where(PublicationAttempt.publication_id == publication_id)
                          .execution_options(populate_existing=True)).all()
    operations = {a.provider_operation_id for a in attempts if a.provider_operation_id}
    if not operations:
        raise BufferReconciliationError("BUFFER_OPERATION_ID_REQUIRED")
    buffers = [a for a in attempts if a.dispatch_provider == "buffer"]
    if len(operations) != 1 or len(buffers) != 1 or not buffers[0].provider_operation_id or buffers[0].status != "UNKNOWN":
        raise BufferReconciliationError("BUFFER_OPERATION_CONFLICT")
    attempt = buffers[0]
    if db.scalar(select(PublicationAttempt.id).where(PublicationAttempt.dispatch_provider == "buffer",
            PublicationAttempt.provider_operation_id == attempt.provider_operation_id, PublicationAttempt.id != attempt.id)):
        raise BufferReconciliationError("BUFFER_OPERATION_CONFLICT")
    known = {a.provider_pin_id for a in attempts if a.provider_pin_id}
    if publication.pinterest_pin_id:
        known.add(publication.pinterest_pin_id)
    if len(known) > 1:
        raise BufferReconciliationError("CONFLICTING_KNOWN_PROVIDER_PIN_IDS")

    metadata = attempt.safe_response_metadata or {}
    destination_mode = _destination_identity_mode(db, publication, attempt=attempt)
    if destination_mode == HISTORICAL_MIXED_DESTINATION_MODE:
        known.add(pinterest_pin_id(attempt.provider_external_link))
        if len(known) > 1:
            raise BufferReconciliationError("CONFLICTING_KNOWN_PROVIDER_PIN_IDS")
    guards = (
        ("publication_fingerprint", bool(publication.publication_fingerprint)),
        ("creative_id", bool(publication.creative_id)),
        ("destination_identity", destination_mode is not None),
        ("approved_content_identity", destination_mode is not None and _approved_content_identity_valid(
            db, publication, destination_mode=destination_mode
        )),
        ("request_fingerprint", attempt.request_fingerprint == request_fingerprint_for(publication)),
        ("buffer_organization_configuration", bool(settings.buffer_organization_id)),
        ("buffer_channel_configuration", bool(settings.buffer_pinterest_channel_id)),
        ("buffer_organization_identity", metadata.get("buffer_organization_id") == settings.buffer_organization_id),
        ("buffer_channel_identity", metadata.get("buffer_channel_id") == settings.buffer_pinterest_channel_id),
    )
    for field, passed in guards:
        if not passed:
            raise BufferReconciliationError(
                "BUFFER_POST_SNAPSHOT_MISMATCH",
                stage=stage,
                field=field,
            )
    return publication, attempt, known


def _receipt_metadata(metadata, sequence, *, phase=None, provider_read_started=None,
                      provider_read_completed=None, provider_operation_status=None,
                      fallback_selected=None, code=None, stage=None, field=None):
    """Return a copied metadata document with one bounded safe receipt updated."""
    safe = dict(metadata or {})
    raw_history = safe.get(RECONCILIATION_RECEIPTS_KEY)
    history = [dict(item) for item in raw_history if isinstance(item, dict)] if isinstance(raw_history, list) else []
    target = None
    for item in history:
        if item.get("version") == RECONCILIATION_RECEIPT_VERSION and item.get("sequence") == sequence:
            target = item
            break
    if target is None:
        raise PublicationReconciliationError("BUFFER_RECONCILIATION_RECEIPT_MISSING")
    updates = {
        "phase": phase,
        "provider_read_started": provider_read_started,
        "provider_read_completed": provider_read_completed,
        "provider_operation_status": provider_operation_status,
        "fallback_selected": fallback_selected,
        "code": code,
        "stage": stage,
        "field": field,
    }
    for key, value in updates.items():
        if value is not None:
            target[key] = value
    safe[RECONCILIATION_RECEIPTS_KEY] = history[-RECONCILIATION_RECEIPT_HISTORY_LIMIT:]
    return safe


def _begin_reconciliation_receipt(db, publication_id):
    """Persist request-arrival evidence before any provider I/O when one Buffer attempt is identifiable."""
    publication = db.get(PinPublication, publication_id, populate_existing=True)
    if publication is None or publication.status != PublicationStatus.PUBLISH_UNKNOWN:
        return None, None
    attempts = list(db.scalars(
        select(PublicationAttempt).where(
            PublicationAttempt.publication_id == publication_id,
            PublicationAttempt.dispatch_provider == "buffer",
        ).with_for_update()
    ).all())
    if len(attempts) != 1:
        return None, None
    attempt = attempts[0]
    metadata = dict(attempt.safe_response_metadata or {})
    raw_history = metadata.get(RECONCILIATION_RECEIPTS_KEY)
    history = [dict(item) for item in raw_history if isinstance(item, dict)] if isinstance(raw_history, list) else []
    raw_total = metadata.get(RECONCILIATION_RECEIPT_TOTAL_KEY)
    total = raw_total if type(raw_total) is int and raw_total >= 0 else len(history)
    sequence = total + 1
    history.append({
        "version": RECONCILIATION_RECEIPT_VERSION,
        "sequence": sequence,
        "phase": "INVOCATION_RECEIVED",
        "provider_read_started": False,
        "provider_read_completed": False,
        "provider_operation_status": None,
        "fallback_selected": False,
        "code": None,
        "stage": None,
        "field": None,
    })
    metadata[RECONCILIATION_RECEIPTS_KEY] = history[-RECONCILIATION_RECEIPT_HISTORY_LIMIT:]
    metadata[RECONCILIATION_RECEIPT_TOTAL_KEY] = sequence
    attempt.safe_response_metadata = metadata
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise PublicationReconciliationError("BUFFER_RECONCILIATION_RECEIPT_PERSISTENCE_FAILED") from None
    return attempt.id, sequence


def _persist_receipt_phase(db, attempt_id, sequence, *, phase, provider_read_started=None,
                           provider_read_completed=None, provider_operation_status=None,
                           fallback_selected=None, code=None, stage=None, field=None):
    if attempt_id is None or sequence is None:
        return
    try:
        attempt = db.scalar(
            select(PublicationAttempt).where(PublicationAttempt.id == attempt_id)
            .execution_options(populate_existing=True).with_for_update()
        )
        if attempt is None:
            raise PublicationReconciliationError("BUFFER_RECONCILIATION_RECEIPT_MISSING")
        attempt.safe_response_metadata = _receipt_metadata(
            attempt.safe_response_metadata,
            sequence,
            phase=phase,
            provider_read_started=provider_read_started,
            provider_read_completed=provider_read_completed,
            provider_operation_status=provider_operation_status,
            fallback_selected=fallback_selected,
            code=code,
            stage=stage,
            field=field,
        )
        db.commit()
    except PublicationReconciliationError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise PublicationReconciliationError("BUFFER_RECONCILIATION_RECEIPT_PERSISTENCE_FAILED") from None


def _persist_rejection_receipt(db, attempt_id, sequence, phase, exc, *, provider_status=None,
                               read_started=False, read_completed=False, fallback_selected=False):
    diagnostic = exc.safe_diagnostic()
    _persist_receipt_phase(
        db,
        attempt_id,
        sequence,
        phase=phase,
        provider_read_started=read_started,
        provider_read_completed=read_completed,
        provider_operation_status=provider_status,
        fallback_selected=fallback_selected,
        code=diagnostic.get("code"),
        stage=diagnostic.get("stage"),
        field=diagnostic.get("field"),
    )


def attest_buffer_reconciliation_receipt(db, publication_id):
    """Return only bounded execution evidence already persisted in attempt metadata."""
    with db.no_autoflush:
        attempts = list(db.scalars(select(PublicationAttempt).where(
            PublicationAttempt.publication_id == publication_id,
            PublicationAttempt.dispatch_provider == "buffer",
        )).all())
        if len(attempts) != 1:
            return {
                "provider_free": True,
                "read_only": True,
                "receipt_present": False,
                "invocation_count": 0,
                "sequence": None,
                "phase": None,
                "provider_read_started": False,
                "provider_read_completed": False,
                "provider_operation_status": None,
                "fallback_selected": False,
                "code": None,
                "stage": None,
                "field": None,
            }
        metadata = attempts[0].safe_response_metadata or {}
        raw_history = metadata.get(RECONCILIATION_RECEIPTS_KEY)
        history = [item for item in raw_history if isinstance(item, dict)] if isinstance(raw_history, list) else []
        history = [item for item in history if item.get("version") == RECONCILIATION_RECEIPT_VERSION]
        latest = history[-1] if history else None
        raw_total = metadata.get(RECONCILIATION_RECEIPT_TOTAL_KEY)
        count = raw_total if type(raw_total) is int and raw_total >= 0 else len(history)
        return {
            "provider_free": True,
            "read_only": True,
            "receipt_present": latest is not None,
            "invocation_count": count,
            "sequence": latest.get("sequence") if latest else None,
            "phase": latest.get("phase") if latest else None,
            "provider_read_started": latest.get("provider_read_started") is True if latest else False,
            "provider_read_completed": latest.get("provider_read_completed") is True if latest else False,
            "provider_operation_status": latest.get("provider_operation_status") if latest else None,
            "fallback_selected": latest.get("fallback_selected") is True if latest else False,
            "code": latest.get("code") if latest else None,
            "stage": latest.get("stage") if latest else None,
            "field": latest.get("field") if latest else None,
        }


def attest_buffer_reconciliation_preflight(db, publication_id, *, settings=None):
    """Evaluate the exact pre-provider reconciliation guards without provider I/O.

    This attestation is intentionally read-only: it never constructs a gateway,
    never reconciles state, and never persists an audit/event. Only bounded safe
    diagnostics are returned.
    """
    settings = settings or get_settings()
    try:
        with db.no_autoflush:
            _entry(db, publication_id, settings, stage="pre_provider")
    except BufferReconciliationError as exc:
        diagnostic = exc.safe_diagnostic()
        return {
            "eligible": False,
            "pre_provider_eligible": False,
            "provider_free": True,
            "read_only": True,
            "reconciliation_performed": False,
            "code": diagnostic.get("code"),
            "stage": diagnostic.get("stage"),
            "field": diagnostic.get("field"),
        }
    return {
        "eligible": True,
        "pre_provider_eligible": True,
        "provider_free": True,
        "read_only": True,
        "reconciliation_performed": False,
        "code": None,
        "stage": None,
        "field": None,
    }


async def reconcile_buffer(db, publication_id, *, actor, settings=None, gateway=None):
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 255 or any(ord(c) < 32 for c in actor):
        raise BufferReconciliationError("ACTOR_REQUIRED")
    settings = settings or get_settings()
    receipt_attempt_id, receipt_sequence = _begin_reconciliation_receipt(db, publication_id)
    try:
        publication, attempt, known = _entry(db, publication_id, settings, stage="pre_provider")
    except BufferReconciliationError as exc:
        _persist_rejection_receipt(
            db, receipt_attempt_id, receipt_sequence, "PRE_PROVIDER_REJECTED", exc
        )
        raise

    operation_id = attempt.provider_operation_id
    expected_request = attempt.request_fingerprint
    gateway = gateway or BufferGateway(settings)

    # This durable boundary is committed before provider I/O. If execution becomes
    # externally ambiguous, receipt attestation can prove whether a read could have occurred.
    _persist_receipt_phase(
        db,
        receipt_attempt_id,
        receipt_sequence,
        phase="PROVIDER_READ_STARTED",
        provider_read_started=True,
    )
    try:
        snapshot = await gateway.post(operation_id)
    except (BufferReadError, BufferConfigurationError):
        _persist_receipt_phase(
            db,
            receipt_attempt_id,
            receipt_sequence,
            phase="PROVIDER_READ_FAILED",
            provider_read_started=True,
            code="BUFFER_RECONCILIATION_READ_FAILED",
        )
        raise BufferReconciliationError("BUFFER_RECONCILIATION_READ_FAILED") from None

    _persist_receipt_phase(
        db,
        receipt_attempt_id,
        receipt_sequence,
        phase="PROVIDER_READ_COMPLETED",
        provider_read_started=True,
        provider_read_completed=True,
        provider_operation_status=snapshot.status,
    )

    retained_persisted_external_link = False
    try:
        # Re-read after I/O; no stale ORM state may authorize a terminal transition.
        publication, attempt, known = _entry(
            db, publication_id, settings, stage="post_provider_revalidation"
        )
        if operation_id != attempt.provider_operation_id or expected_request != attempt.request_fingerprint:
            raise BufferReconciliationError("BUFFER_OPERATION_CONFLICT")
        if snapshot.buffer_post_id != operation_id:
            raise BufferReconciliationError(
                "BUFFER_POST_SNAPSHOT_MISMATCH",
                stage="provider_snapshot",
                field="provider_operation_id",
            )
        mismatch = snapshot_mismatch_field(publication, snapshot, settings)
        if mismatch is not None:
            raise BufferReconciliationError(
                "BUFFER_POST_SNAPSHOT_MISMATCH",
                stage="provider_snapshot",
                field=mismatch,
            )
        pin = pinterest_pin_id(snapshot.external_link) if snapshot.status == "sent" else None
        if snapshot.status == "sent" and not pin:
            destination_mode = _destination_identity_mode(db, publication, attempt=attempt)
            persisted_pin = pinterest_pin_id(attempt.provider_external_link)
            if (
                destination_mode == HISTORICAL_MIXED_DESTINATION_MODE
                and persisted_pin is not None
                and known == {persisted_pin}
            ):
                pin = persisted_pin
                retained_persisted_external_link = True
                _persist_receipt_phase(
                    db,
                    receipt_attempt_id,
                    receipt_sequence,
                    phase="FALLBACK_SELECTED",
                    provider_read_started=True,
                    provider_read_completed=True,
                    provider_operation_status=snapshot.status,
                    fallback_selected=True,
                )
                publication, attempt, known = _entry(
                    db, publication_id, settings, stage="post_fallback_revalidation"
                )
                if operation_id != attempt.provider_operation_id or expected_request != attempt.request_fingerprint:
                    raise BufferReconciliationError("BUFFER_OPERATION_CONFLICT")
            else:
                raise BufferReconciliationError("BUFFER_SENT_LINK_UNVERIFIED")
        if pin:
            if known and known != {pin}:
                raise BufferReconciliationError("KNOWN_PROVIDER_PIN_MISMATCH")
            if (db.scalar(select(PinPublication.id).where(
                    PinPublication.pinterest_pin_id == pin,
                    PinPublication.id != publication_id,
                )) or db.scalar(select(PublicationAttempt.id).where(
                    PublicationAttempt.provider_pin_id == pin,
                    PublicationAttempt.publication_id != publication_id,
                ))):
                raise BufferReconciliationError("PROVIDER_PIN_ID_ALREADY_ASSIGNED")
        elif known and snapshot.status == "error":
            raise BufferReconciliationError("KNOWN_PROVIDER_PIN_REQUIRES_CONFIRMATION")
    except BufferReconciliationError as exc:
        _persist_rejection_receipt(
            db,
            receipt_attempt_id,
            receipt_sequence,
            "POST_PROVIDER_REJECTED",
            exc,
            provider_status=snapshot.status,
            read_started=True,
            read_completed=True,
            fallback_selected=retained_persisted_external_link,
        )
        raise

    _persist_receipt_phase(
        db,
        receipt_attempt_id,
        receipt_sequence,
        phase="CAS_STARTED",
        provider_read_started=True,
        provider_read_completed=True,
        provider_operation_status=snapshot.status,
        fallback_selected=retained_persisted_external_link,
    )
    # The receipt commit is intentionally outside the business-state transaction.
    # Revalidate local authority once more before terminal CAS.
    try:
        publication, attempt, known = _entry(
            db, publication_id, settings, stage="pre_cas_revalidation"
        )
        if operation_id != attempt.provider_operation_id or expected_request != attempt.request_fingerprint:
            raise BufferReconciliationError("BUFFER_OPERATION_CONFLICT")
    except BufferReconciliationError as exc:
        _persist_rejection_receipt(
            db,
            receipt_attempt_id,
            receipt_sequence,
            "POST_PROVIDER_REJECTED",
            exc,
            provider_status=snapshot.status,
            read_started=True,
            read_completed=True,
            fallback_selected=retained_persisted_external_link,
        )
        raise

    now = datetime.now(timezone.utc)
    terminal = snapshot.status in {"sent", "error"}
    status = PublicationStatus.PUBLISHED if pin else PublicationStatus.PUBLISH_FAILED if terminal else PublicationStatus.PUBLISH_UNKNOWN
    code = None if pin else "BUFFER_PROVIDER_FAILED_CONFIRMED" if terminal else "BUFFER_FINAL_OUTCOME_PENDING"
    try:
        # The publication CAS serializes observers; attempt CAS and audit share its transaction.
        result = db.execute(update(PinPublication).where(
            PinPublication.id == publication_id,
            PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN,
            PinPublication.publication_fingerprint == publication.publication_fingerprint,
        ).values(
            status=status,
            error_code=code,
            pinterest_pin_id=pin if terminal else publication.pinterest_pin_id,
            published_at=now if pin else publication.published_at,
        ))
        if result.rowcount != 1:
            raise BufferReconciliationError("RECONCILIATION_CONFLICT")
        terminal_metadata = _receipt_metadata(
            attempt.safe_response_metadata,
            receipt_sequence,
            phase="CAS_SUCCEEDED",
            provider_read_started=True,
            provider_read_completed=True,
            provider_operation_status=snapshot.status,
            fallback_selected=retained_persisted_external_link,
            code=code,
        )
        values = dict(
            provider_operation_status=snapshot.status,
            provider_external_link=(
                attempt.provider_external_link
                if retained_persisted_external_link
                else snapshot.external_link
            ),
            provider_last_observed_at=now,
            error_code=code,
            safe_response_metadata=terminal_metadata,
        )
        if terminal:
            values.update(status="SUCCEEDED" if pin else "FAILED", completed_at=now, provider_pin_id=pin)
        result = db.execute(update(PublicationAttempt).where(
            PublicationAttempt.id == attempt.id,
            PublicationAttempt.status == "UNKNOWN",
            PublicationAttempt.provider_operation_id == operation_id,
            PublicationAttempt.request_fingerprint == expected_request,
        ).values(**values))
        if result.rowcount != 1:
            raise BufferReconciliationError("RECONCILIATION_CONFLICT")
        if terminal:
            db.add(PublicationReconciliationEvent(
                publication_id=publication_id,
                attempt_id=attempt.id,
                actor=actor,
                action="PROVIDER_PIN_CONFIRMED" if pin else "PROVIDER_FAILURE_CONFIRMED",
                previous_status="PUBLISH_UNKNOWN",
                new_status=status.value,
                provider="buffer",
                provider_operation_id=operation_id,
                provider_operation_status=snapshot.status,
                provider_pin_id=pin,
                reason="Exact Buffer post and immutable snapshot verified",
            ))
        db.commit()
    except BufferReconciliationError as exc:
        db.rollback()
        _persist_rejection_receipt(
            db,
            receipt_attempt_id,
            receipt_sequence,
            "CAS_FAILED",
            exc,
            provider_status=snapshot.status,
            read_started=True,
            read_completed=True,
            fallback_selected=retained_persisted_external_link,
        )
        raise
    except Exception:
        db.rollback()
        try:
            _persist_receipt_phase(
                db,
                receipt_attempt_id,
                receipt_sequence,
                phase="CAS_FAILED",
                provider_read_started=True,
                provider_read_completed=True,
                provider_operation_status=snapshot.status,
                fallback_selected=retained_persisted_external_link,
                code="BUFFER_RECONCILIATION_PERSISTENCE_FAILED",
            )
        except PublicationReconciliationError:
            pass
        raise PublicationReconciliationError("BUFFER_RECONCILIATION_PERSISTENCE_FAILED") from None
    db.refresh(publication)
    return publication

