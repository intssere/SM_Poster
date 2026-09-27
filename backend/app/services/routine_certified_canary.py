"""Read-only, single-publication offline certification. Never invokes the routine worker."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import socket
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from threading import Lock

from sqlalchemy import event, func, select, text

from app.core.config import Settings
from app.models.domain import (
    CreativeTemplate, PinApproval, PinCreative, PinDraft, PinPublication,
    PinterestBoard, PinterestConnection, Product, ProductImage,
    PublicationAttempt, PublicationStatus,
)
from app.models.routine_publishing import (
    RoutineAttemptBoundary,
    RoutineDispatchPermit,
    RoutinePublishingControl,
)
from app.services.deployment_attestation import read_build_provenance
from app.services.routine_canary_fixture import _assert_static_safety, RoutineCanaryFixtureError
from app.services.routine_dispatch_authorization import validate_permit
from app.services.routine_offline_preflight import (
    PERSISTED_ROUTE_MAX_AGE, build_routine_offline_evidence, RoutineOfflinePreflightError,
)
from app.services.routine_pinterest_scheduler import scheduler_status


CONFIRMATION_TEXT_VERSION = "ROUTINE_CERTIFIED_OFFLINE_CANARY_V1"
PREFLIGHT_CONTRACT_VERSION = "ROUTINE_CERTIFIED_OFFLINE_CANARY_PREFLIGHT_V1"
PREFLIGHT_TTL = timedelta(minutes=5)
_SHARE_LOCK_SQL = (
    "LOCK TABLE pin_publications, routine_dispatch_permits, routine_publishing_runs, "
    "pin_drafts, pin_creatives, pin_approvals, pinterest_connections, "
    "pinterest_boards, product_images, products, creative_templates, "
    "publication_attempts, routine_attempt_boundaries IN SHARE MODE"
)
_READ_ONLY_SQL = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"


class CertifiedCanaryError(RuntimeError):
    pass


class OfflineNetworkAttempt(CertifiedCanaryError):
    pass


_network_counter: ContextVar[list[int] | None] = ContextVar("certified_canary_network_counter", default=None)
_hook_lock = Lock()
_hook_installed = False
_BLOCKED_EVENTS = {
    "socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
    "socket.gethostbyaddr", "socket.getnameinfo", "socket.sendto", "subprocess.Popen", "os.system",
}


def _network_audit(event, args):
    counter = _network_counter.get()
    if counter is not None and event in _BLOCKED_EVENTS:
        counter[0] += 1
        raise OfflineNetworkAttempt("CANARY_NETWORK_OPERATION_ATTEMPTED")


def _guard_socket_method(method):
    def checked(sock, *args, **kwargs):
        counter = _network_counter.get()
        if counter is not None:
            counter[0] += 1
            raise OfflineNetworkAttempt("CANARY_NETWORK_OPERATION_ATTEMPTED")
        return method(sock, *args, **kwargs)
    return checked


@contextmanager
def _offline_network_guard():
    global _hook_installed
    with _hook_lock:
        if not _hook_installed:
            # A Python audit hook cannot be removed; its context variable scopes
            # the prohibition to this request, not to other application requests.
            sys.addaudithook(_network_audit)
            # Audit events cover connections but not send on an existing socket.
            # Include that path so pooled client connections cannot evade the
            # tripwire during the offline evaluation.
            for name in ("connect", "connect_ex", "send", "sendall", "sendto", "sendmsg", "sendfile"):
                method = getattr(socket.socket, name, None)
                if method is not None:
                    setattr(socket.socket, name, _guard_socket_method(method))
            _hook_installed = True
    counter = [0]
    token = _network_counter.set(counter)
    try:
        yield counter
    finally:
        _network_counter.reset(token)


def _due_ids(db, now):
    # Two rows suffice to reject a non-unique global due set.
    return list(db.scalars(
        select(PinPublication.id).where(
            PinPublication.status == PublicationStatus.SCHEDULED,
            PinPublication.scheduled_for <= now,
        ).order_by(PinPublication.id).limit(2)
    ))


def _active_permits(db):
    return list(db.scalars(
        select(RoutineDispatchPermit).where(RoutineDispatchPermit.status == "ACTIVE").limit(2)
    ))


def _boundary_counts(db):
    return (
        db.scalar(select(func.count()).select_from(PublicationAttempt)),
        db.scalar(select(func.count()).select_from(RoutineAttemptBoundary)),
    )


def _reject_database_write(_conn, _cursor, statement, _params, _context, _many):
    # No helper used by either path may emit DML, DDL, or a write-capable CTE.
    # PostgreSQL preflight additionally runs in a server-enforced READ ONLY
    # transaction. Execution needs row/table locks, which PostgreSQL forbids in
    # READ ONLY mode, so this connection-scoped guard covers that transaction.
    sql = statement.strip()
    if ";" in sql:
        raise CertifiedCanaryError("CANARY_DATABASE_WRITE_ATTEMPTED")
    if re.match(r"^SELECT\b", sql, re.I):
        # PostgreSQL SELECT ... INTO creates a table without a write keyword.
        if re.search(r"\bINTO\b", sql, re.I):
            raise CertifiedCanaryError("CANARY_DATABASE_WRITE_ATTEMPTED")
        # In the locked execution transaction READ ONLY is not available.
        # Reject *all* SQL functions except this closed list of pure functions
        # used by the prerequisite SELECTs. In particular nextval/setval,
        # pg_notify and application-defined write functions cannot pass.
        pure = {
            "COUNT", "EXISTS", "IN", "CAST", "COALESCE", "LOWER", "UPPER",
            "LENGTH", "SUBSTR", "DATE", "EXTRACT", "MAX", "MIN", "SUM",
            "ABS", "ROUND", "NULLIF", "TRIM", "ANY", "ALL", "JSON_EXTRACT",
        }
        if any(name.upper() not in pure for name in re.findall(r"\b([a-z_]\w*)\s*\(", sql, re.I)):
            raise CertifiedCanaryError("CANARY_DATABASE_WRITE_ATTEMPTED")
    elif sql != _SHARE_LOCK_SQL and sql != _READ_ONLY_SQL:
        raise CertifiedCanaryError("CANARY_DATABASE_WRITE_ATTEMPTED")


def _canonical(value):
    if isinstance(value, datetime):
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None
                else value.astimezone(timezone.utc)).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise CertifiedCanaryError("CANARY_PREFLIGHT_STATE_UNSERIALIZABLE")


def _bytes(value):
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _row(db, model, ident):
    row = db.get(model, ident) if ident else None
    return ({column.key: getattr(row, column.key) for column in model.__table__.columns}
            if row is not None else None)


def _release_identity():
    provenance = read_build_provenance()
    if not provenance.present:
        return None
    if not provenance.valid:
        raise CertifiedCanaryError("CANARY_RELEASE_IDENTITY_INVALID")
    return {
        "canonical_commit_sha": provenance.commit_sha,
        "canonical_tree_sha": provenance.tree_sha,
        "release_commit_sha": provenance.release_commit_sha,
        "release_tree_sha": provenance.release_tree_sha,
        "topology": provenance.topology,
        "release_overlay": {"path": provenance.overlay_path, "sha256": provenance.overlay_sha256},
    }


def _signing_key(settings):
    # No ephemeral or default key: a missing signing secret must block both paths.
    if not settings.app_secret_key or len(settings.app_secret_key) < 32:
        raise CertifiedCanaryError("CANARY_PREFLIGHT_SIGNING_UNAVAILABLE")
    return settings.app_secret_key.encode()


def _evaluated_state(db, publication, permit, valid, evidence, settings, scheduler):
    image = db.get(ProductImage, publication.source_image_id) if publication.source_image_id else None
    return {
        "publication": _row(db, PinPublication, publication.id),
        "permit": _row(db, RoutineDispatchPermit, permit.id),
        "control": _row(db, RoutinePublishingControl, "default"),
        "draft": _row(db, PinDraft, publication.draft_id),
        "creative": _row(db, PinCreative, publication.creative_id),
        "approval": _row(db, PinApproval, publication.approval_id),
        "image": _row(db, ProductImage, publication.source_image_id),
        "product": _row(db, Product, image.product_id) if image else None,
        "template": _row(db, CreativeTemplate, publication.template_id),
        "connection": _row(db, PinterestConnection, publication.pinterest_connection_id),
        "board": _row(db, PinterestBoard, publication.pinterest_board_record_id),
        "validated": {key: valid.get(key) for key in ("valid", "status", "quality", "duplicate", "readiness")},
        "offline_evidence": {
            key: getattr(evidence, key, None) for key in (
                "publication_id", "publication_fingerprint", "request_fingerprint",
                "pinterest_connection_id", "pinterest_board_record_id", "board_service_id",
                "permit_validated", "quality_passed", "duplicate_safe",
                "persisted_route_validated", "external_requests",
            )
        },
        "safety_gates": {
            key: getattr(settings, key) for key in (
                "publishing_enabled", "buffer_publishing_enabled", "buffer_single_pin_pilot_enabled",
                "pinterest_single_pin_pilot_enabled", "routine_pinterest_worker_enabled",
                "routine_buffer_dispatch_enabled", "routine_pinterest_scheduler_enabled",
                "routine_autonomous_authorization_enabled", "pinterest_autonomous_generation_enabled",
                "pinterest_autonomous_execution_enabled", "pinterest_autonomous_board_ensure_enabled",
                "pinterest_write_scope_enabled", "pinterest_board_write_scope_enabled",
                "pinterest_board_provisioning_enabled", "routine_pinterest_dry_run",
                "routine_pinterest_batch_size", "routine_pinterest_daily_write_limit",
            )
        },
        "scheduler": scheduler,
    }


def _check_receipt(receipt, contract_version, publication_id, settings, now):
    fields = {
        "contract_version", "publication_id", "permit_id", "request_fingerprint",
        "prerequisite_fingerprint", "release_identity", "issued_at", "expires_at", "signature",
    }
    if not isinstance(receipt, dict) or set(receipt) != fields or contract_version != PREFLIGHT_CONTRACT_VERSION:
        raise CertifiedCanaryError("CANARY_PREFLIGHT_RECEIPT_REQUIRED")
    if receipt["contract_version"] != PREFLIGHT_CONTRACT_VERSION or receipt["publication_id"] != publication_id:
        raise CertifiedCanaryError("CANARY_PREFLIGHT_IDENTITY_MISMATCH")
    if not all(isinstance(receipt[k], str) and receipt[k] for k in (
        "permit_id", "request_fingerprint", "prerequisite_fingerprint", "issued_at", "expires_at", "signature"
    )):
        raise CertifiedCanaryError("CANARY_PREFLIGHT_RECEIPT_MALFORMED")
    if not all(len(receipt[k]) == 64 and all(c in "0123456789abcdef" for c in receipt[k])
               for k in ("request_fingerprint", "prerequisite_fingerprint", "signature")):
        raise CertifiedCanaryError("CANARY_PREFLIGHT_RECEIPT_MALFORMED")
    if receipt["release_identity"] is not None and not isinstance(receipt["release_identity"], dict):
        raise CertifiedCanaryError("CANARY_PREFLIGHT_RECEIPT_MALFORMED")
    try:
        issued = datetime.fromisoformat(receipt["issued_at"])
        expires = datetime.fromisoformat(receipt["expires_at"])
        if issued.tzinfo is None or expires.tzinfo is None:
            raise ValueError("timezone required")
        payload = {key: value for key, value in receipt.items() if key != "signature"}
        signature = hmac.new(_signing_key(settings), _bytes(payload), hashlib.sha256).hexdigest()
    except (ValueError, TypeError, OverflowError, CertifiedCanaryError) as exc:
        if isinstance(exc, CertifiedCanaryError) and str(exc) == "CANARY_PREFLIGHT_SIGNING_UNAVAILABLE":
            raise
        raise CertifiedCanaryError("CANARY_PREFLIGHT_RECEIPT_MALFORMED") from None
    if not hmac.compare_digest(signature, receipt["signature"]):
        raise CertifiedCanaryError("CANARY_PREFLIGHT_SIGNATURE_INVALID")
    if expires - issued != PREFLIGHT_TTL or issued > now or now >= expires:
        raise CertifiedCanaryError("CANARY_PREFLIGHT_EXPIRED")
    if receipt["release_identity"] != _release_identity():
        raise CertifiedCanaryError("CANARY_PREFLIGHT_RELEASE_DRIFT")


def _evaluate_prerequisites(db, *, publication_id, settings, now, scheduler_snapshot, locked):
    connection = db.connection()
    event.listen(connection, "before_cursor_execute", _reject_database_write)
    try:
        if not locked and db.get_bind().dialect.name == "postgresql":
            # A single consistent snapshot and an actual DB-enforced no-write transaction.
            db.execute(text(_READ_ONLY_SQL))
        control = db.scalar(
            select(RoutinePublishingControl).where(RoutinePublishingControl.id == "default").with_for_update()
            if locked else
            select(RoutinePublishingControl).where(RoutinePublishingControl.id == "default")
        )
        if locked and db.get_bind().dialect.name == "postgresql":
            # Other schedule/permit writers do not coordinate on control. A
            # transaction-scoped SHARE lock blocks their ROW EXCLUSIVE writes
            # until the certification has reached its linearization point.
            # Lock every table read by offline permit/route/duplicate checks.
            db.execute(text(_SHARE_LOCK_SQL))
        evaluated_at = now() if callable(now) else now
        with _offline_network_guard() as external:
            scheduler = scheduler_snapshot if scheduler_snapshot is not None else scheduler_status(settings)
            try:
                _assert_static_safety(db, settings, control, scheduler)
            except RoutineCanaryFixtureError as exc:
                raise CertifiedCanaryError(str(exc)) from None
            if _due_ids(db, evaluated_at) != [publication_id]:
                raise CertifiedCanaryError("CANARY_DUE_IDENTITY_OR_CARDINALITY")
            permits = _active_permits(db)
            if len(permits) != 1 or permits[0].publication_id != publication_id:
                raise CertifiedCanaryError("CANARY_ACTIVE_PERMIT_CARDINALITY")
            permit = permits[0]
            expires = permit.expires_at
            expires = expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires.astimezone(timezone.utc)
            if permit.consumed_at is not None or expires <= evaluated_at:
                raise CertifiedCanaryError("CANARY_PERMIT_INVALID")
            publication = db.get(PinPublication, publication_id)
            before = _boundary_counts(db)
            valid = validate_permit(db, publication, permit, now=evaluated_at, require_due=True)
            if valid.get("valid") is not True:
                raise CertifiedCanaryError("CANARY_PERMIT_INVALID")
            try:
                evidence = build_routine_offline_evidence(db, publication, permit=permit, now=evaluated_at)
            except RoutineOfflinePreflightError as exc:
                raise CertifiedCanaryError(str(exc)) from None
            if (evidence.publication_id != publication_id or evidence.permit_validated is not True
                    or evidence.persisted_route_validated is not True
                    or evidence.quality_passed is not True or evidence.duplicate_safe is not True
                    or type(evidence.external_requests) is not int or evidence.external_requests != 0
                    or external[0] != 0):
                raise CertifiedCanaryError("CANARY_OFFLINE_EVIDENCE_INVALID")
            if (getattr(evidence, "request_fingerprint", None) is not None
                    and evidence.request_fingerprint != permit.request_fingerprint):
                raise CertifiedCanaryError("CANARY_OFFLINE_EVIDENCE_INVALID")
            if db.new or db.dirty or db.deleted:
                raise CertifiedCanaryError("CANARY_UNEXPECTED_DATABASE_WRITE")
            state = _evaluated_state(db, publication, permit, valid, evidence, settings, scheduler)
            if db.new or db.dirty or db.deleted:
                raise CertifiedCanaryError("CANARY_UNEXPECTED_DATABASE_WRITE")

            db.refresh(control)
            db.refresh(publication)
            db.refresh(permit)
            if (control.state != "PAUSED" or publication.status != PublicationStatus.SCHEDULED
                    or permit.status != "ACTIVE" or permit.consumed_at is not None
                    or _due_ids(db, evaluated_at) != [publication_id]
                    or len(_active_permits(db)) != 1
                    or _boundary_counts(db) != before
                    or db.new or db.dirty or db.deleted):
                raise CertifiedCanaryError("CANARY_POSTCONDITION_DRIFT")
            try:
                _assert_static_safety(
                    db, settings, control,
                    scheduler_snapshot if scheduler_snapshot is not None else scheduler_status(settings),
                )
            except RoutineCanaryFixtureError as exc:
                raise CertifiedCanaryError(str(exc)) from None
            if external[0] != 0:
                raise CertifiedCanaryError("CANARY_NETWORK_OPERATION_ATTEMPTED")
            completed_at = now() if callable(now) else now
            if completed_at < evaluated_at:
                raise CertifiedCanaryError("CANARY_PREFLIGHT_CLOCK_DRIFT")
            if expires <= completed_at:
                raise CertifiedCanaryError("CANARY_PERMIT_INVALID")
            board = state["board"]
            if board and board["last_synced_at"]:
                synced = board["last_synced_at"]
                synced = synced.replace(tzinfo=timezone.utc) if synced.tzinfo is None else synced.astimezone(timezone.utc)
                if completed_at - synced > PERSISTED_ROUTE_MAX_AGE:
                    raise CertifiedCanaryError("PERSISTED_PINTEREST_ROUTING_TOO_OLD")
        return permit.id, permit.request_fingerprint, state, completed_at
    finally:
        event.remove(connection, "before_cursor_execute", _reject_database_write)
        # Preflight closes its read-only snapshot here. Execution's caller keeps
        # the historical locks until the receipt/state comparison is complete.
        if not locked:
            db.rollback()


def preflight_certified_offline_canary(
    db, *, publication_id: str, settings: Settings, now: datetime | None = None,
    scheduler_snapshot: dict | None = None,
) -> dict:
    clock = (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))
    key = _signing_key(settings)
    release = _release_identity()
    permit_id, request_fingerprint, state, issued_at = _evaluate_prerequisites(
        db, publication_id=publication_id, settings=settings, now=clock,
        scheduler_snapshot=scheduler_snapshot, locked=False,
    )
    payload = {
        "contract_version": PREFLIGHT_CONTRACT_VERSION,
        "publication_id": publication_id,
        "permit_id": permit_id,
        "request_fingerprint": request_fingerprint,
        "prerequisite_fingerprint": hmac.new(key, _bytes(state), hashlib.sha256).hexdigest(),
        "release_identity": release,
        "issued_at": issued_at.isoformat(),
        "expires_at": (issued_at + PREFLIGHT_TTL).isoformat(),
    }
    return {**payload, "signature": hmac.new(key, _bytes(payload), hashlib.sha256).hexdigest()}


def certify_one_shot_offline_canary(
    db, *, publication_id: str, settings: Settings, preflight_contract_version: str,
    preflight_receipt: dict, now: datetime | None = None, scheduler_snapshot: dict | None = None,
) -> dict:
    """Verify a short-lived preflight and re-evaluate under the historical locks.

    Receipts are not consumed in storage: explicit production authorization
    remains the external one-shot authority. Reuse within the TTL is possible.
    """
    clock = (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))
    _check_receipt(preflight_receipt, preflight_contract_version, publication_id, settings, clock())
    try:
        permit_id, request_fingerprint, state, completed_at = _evaluate_prerequisites(
            db, publication_id=publication_id, settings=settings, now=clock,
            scheduler_snapshot=scheduler_snapshot, locked=True,
        )
        # Lock waits and slow prerequisite reads consume receipt TTL. Never
        # certify against the time captured before acquiring the locks.
        _check_receipt(preflight_receipt, preflight_contract_version, publication_id, settings, completed_at)
        key = _signing_key(settings)
        fingerprint = hmac.new(key, _bytes(state), hashlib.sha256).hexdigest()
        if (preflight_receipt["permit_id"] != permit_id
                or preflight_receipt["request_fingerprint"] != request_fingerprint
                or not hmac.compare_digest(preflight_receipt["prerequisite_fingerprint"], fingerprint)):
            raise CertifiedCanaryError("CANARY_PREFLIGHT_STATE_DRIFT")
        # All state checks have passed; this is an offline, read-only evaluation only.
        return {
            "status": "SUCCEEDED", "mode": "DRY_RUN",
            "publication_id": publication_id, "control_state": "PAUSED",
            "scanned": 1, "eligible": 1, "skipped": 0,
            "claimed": 0, "dispatched": 0, "published": 0,
            "failed": 0, "unknown": 0, "external_requests": 0,
        }
    finally:
        db.rollback()