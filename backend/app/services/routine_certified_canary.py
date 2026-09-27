"""Read-only, single-publication offline certification. Never invokes the routine worker."""

from __future__ import annotations

import socket
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from threading import Lock

from sqlalchemy import func, select, text

from app.core.config import Settings
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
from app.models.routine_publishing import (
    RoutineAttemptBoundary,
    RoutineDispatchPermit,
    RoutinePublishingControl,
)
from app.services.routine_canary_fixture import _assert_static_safety, RoutineCanaryFixtureError
from app.services.routine_dispatch_authorization import validate_permit
from app.services.routine_offline_preflight import build_routine_offline_evidence, RoutineOfflinePreflightError
from app.services.routine_pinterest_scheduler import scheduler_status


CONFIRMATION_TEXT_VERSION = "ROUTINE_CERTIFIED_OFFLINE_CANARY_V1"


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


def certify_one_shot_offline_canary(
    db, *, publication_id: str, settings: Settings, now: datetime | None = None,
    scheduler_snapshot: dict | None = None,
) -> dict:
    """Evaluate once without transitioning control or persisting any audit/attempt state.

    The control row is locked first (the same order as fixture preparation).
    PostgreSQL SHARE locks then hold the global publication/permit set through
    evaluation. READ COMMITTED ensures scans after waiting for a lock see the
    latest committed state. PAUSED is never changed; rollback discards any
    unexpected transaction writes and releases all locks on every path.
    """
    now = now or datetime.now(timezone.utc)
    try:
        control = db.scalar(
            select(RoutinePublishingControl).where(RoutinePublishingControl.id == "default").with_for_update()
        )
        if db.get_bind().dialect.name == "postgresql":
            # Other schedule/permit writers do not coordinate on control. A
            # transaction-scoped SHARE lock blocks their ROW EXCLUSIVE writes
            # until the certification has reached its linearization point.
            # Lock every table read by offline permit/route/duplicate checks.
            db.execute(text(
                "LOCK TABLE pin_publications, routine_dispatch_permits, routine_publishing_runs, "
                "pin_drafts, pin_creatives, pin_approvals, pinterest_connections, "
                "pinterest_boards, product_images, creative_templates, "
                "publication_attempts, routine_attempt_boundaries IN SHARE MODE"
            ))
        scheduler = scheduler_snapshot if scheduler_snapshot is not None else scheduler_status(settings)
        try:
            _assert_static_safety(db, settings, control, scheduler)
        except RoutineCanaryFixtureError as exc:
            raise CertifiedCanaryError(str(exc)) from None

        if _due_ids(db, now) != [publication_id]:
            raise CertifiedCanaryError("CANARY_DUE_IDENTITY_OR_CARDINALITY")
        permits = _active_permits(db)
        if len(permits) != 1 or permits[0].publication_id != publication_id:
            raise CertifiedCanaryError("CANARY_ACTIVE_PERMIT_CARDINALITY")
        permit = permits[0]
        publication = db.get(PinPublication, publication_id)
        before = _boundary_counts(db)

        with _offline_network_guard() as external:
            valid = validate_permit(db, publication, permit, now=now, require_due=True)
            if valid.get("valid") is not True:
                raise CertifiedCanaryError("CANARY_PERMIT_INVALID")
            try:
                evidence = build_routine_offline_evidence(db, publication, permit=permit, now=now)
            except RoutineOfflinePreflightError as exc:
                raise CertifiedCanaryError(str(exc)) from None
            if (evidence.publication_id != publication_id or evidence.permit_validated is not True
                    or evidence.persisted_route_validated is not True
                    or evidence.quality_passed is not True or evidence.duplicate_safe is not True
                    or type(evidence.external_requests) is not int or evidence.external_requests != 0
                    or external[0] != 0):
                raise CertifiedCanaryError("CANARY_OFFLINE_EVIDENCE_INVALID")
            if db.new or db.dirty or db.deleted:
                raise CertifiedCanaryError("CANARY_UNEXPECTED_DATABASE_WRITE")

        db.refresh(control)
        db.refresh(publication)
        db.refresh(permit)
        if (control.state != "PAUSED" or publication.status != PublicationStatus.SCHEDULED
                or permit.status != "ACTIVE" or permit.consumed_at is not None
                or _due_ids(db, now) != [publication_id]
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

        receipt = {
            "status": "SUCCEEDED", "mode": "DRY_RUN",
            "publication_id": publication_id, "control_state": "PAUSED",
            "scanned": 1, "eligible": 1, "skipped": 0,
            "claimed": 0, "dispatched": 0, "published": 0,
            "failed": 0, "unknown": 0, "external_requests": external[0],
        }
        return receipt
    finally:
        # Even a successful read-only evaluation must close its snapshot and lock.
        db.rollback()