"""Read-only readiness certificate for one already-scheduled autonomous pin."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.models.domain import (
    Board,
    ContentRevision,
    CreativeTemplate,
    PinApproval,
    PinCreative,
    PinConcept,
    PinDraft,
    PinPublication,
    PinterestAutonomousExecutionRun,
    PinterestAutonomousGenerationRun,
    PinterestBoard,
    PinterestConnection,
    PinterestOptimizerApplication,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
    PinterestSeoBrief,
    Product,
    PublicationStatus,
)
from app.models.routine_publishing import (
    RoutineDispatchPermit,
    RoutinePublishingControl,
)
from app.services.pinterest_board_strategy import normalize_board_text
from app.services.pinterest_publisher import normalize_persisted_utc
from app.services.public_creative_media import public_creative_url_matches
from app.services.publication_scheduler import request_fingerprint_for
from app.services.routine_offline_preflight import (
    build_routine_offline_evidence,
)
from app.services.routine_autonomous_authorization import AUTONOMOUS_ACTOR
from app.services.routine_scheduled_quotas import (
    ScheduledQuotaError,
    scheduled_quota_limits,
)
from app.services.routine_scheduled_commitments import assess_scheduled_quota_with_commitments
from app.services.routine_scheduler_canary_context import (
    RoutineSchedulerCanaryContext,
    validate_canary_context,
)

MAX_ROUTE_ROWS = 200
MAX_SAME_DAY_ITEMS = 200
OPTIMIZER_METADATA_KEY = "adaptive_optimizer_v1"
EXECUTION_POLICY_VERSION = "PINTEREST_AUTONOMOUS_EXECUTION_V1"


def _hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _optimizer_metadata(item) -> dict | None:
    selection = item.selection_metadata or {}
    value = selection.get(OPTIMIZER_METADATA_KEY) if isinstance(selection, dict) else None
    return value if isinstance(value, dict) else None


def _execution_fingerprint(*, item, plan, optimizer, optimizer_metadata, board, scheduled_for):
    return _hash({
        "policy_version": EXECUTION_POLICY_VERSION,
        "portfolio_item_id": item.id,
        "portfolio_item_fingerprint": item.item_fingerprint,
        "plan_id": plan.id,
        "plan_fingerprint": plan.plan_fingerprint,
        "optimizer_application_id": optimizer.id,
        "optimizer_fingerprint": optimizer.optimizer_fingerprint,
        "optimizer_input_state_fingerprint": optimizer.input_state_fingerprint,
        "item_optimizer_metadata": {
            "optimizer_policy_version": optimizer_metadata.get("optimizer_policy_version"),
            "optimizer_fingerprint": optimizer_metadata.get("optimizer_fingerprint"),
            "input_state_fingerprint": optimizer_metadata.get("input_state_fingerprint"),
            "recommended_position": optimizer_metadata.get("recommended_position"),
            "target_slot_index": optimizer_metadata.get("target_slot_index"),
            "target_planned_date": optimizer_metadata.get("target_planned_date"),
            "selection_reason": optimizer_metadata.get("selection_reason"),
        },
        "product_id": item.product_id,
        "local_board_id": item.local_board_id,
        "board_key_snapshot": item.board_key_snapshot,
        "content_angle_id": item.content_angle_id,
        "angle_key_snapshot": item.angle_key_snapshot,
        "scheduled_for": scheduled_for.isoformat(),
        "pinterest_board_record_id": board.id,
        "pinterest_external_board_id": board.external_board_id,
    })


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return normalize_persisted_utc(value)


def _check(checks: list[dict], code: str, passed: bool, message: str, **context) -> None:
    checks.append({
        "code": code,
        "passed": bool(passed),
        "blocking": not bool(passed),
        "message": message,
        "context": context,
    })


def _current_persisted_route(db, item, publication):
    """Resolve only persisted rows; ambiguity and overlarge route sets fail closed."""
    local_board = db.get(Board, item.local_board_id)
    if not (
        local_board
        and local_board.active
        and local_board.slug
        and local_board.slug == item.board_key_snapshot
    ):
        return None, "LOCAL_BOARD_SNAPSHOT_STALE"

    local_matches = list(db.scalars(
        select(Board)
        .where(Board.slug == item.board_key_snapshot, Board.active.is_(True))
        .order_by(Board.id)
        .limit(2)
    ).all())
    if len(local_matches) != 1 or local_matches[0].id != local_board.id:
        return None, "LOCAL_BOARD_ROUTING_AMBIGUOUS"

    connections = list(db.scalars(
        select(PinterestConnection)
        .where(PinterestConnection.status == "CONNECTED")
        .order_by(PinterestConnection.id)
        .limit(2)
    ).all())
    if len(connections) != 1:
        return None, "PERSISTED_PINTEREST_CONNECTION_MISSING_OR_AMBIGUOUS"
    connection = connections[0]
    if publication.pinterest_connection_id != connection.id:
        return None, "PERSISTED_PINTEREST_CONNECTION_STALE"

    boards = list(db.scalars(
        select(PinterestBoard)
        .where(
            PinterestBoard.connection_id == connection.id,
            PinterestBoard.is_active.is_(True),
        )
        .order_by(PinterestBoard.id)
        .limit(MAX_ROUTE_ROWS + 1)
    ).all())
    if len(boards) > MAX_ROUTE_ROWS:
        return None, "PERSISTED_PINTEREST_ROUTE_SET_TOO_LARGE"

    key = str(item.board_key_snapshot or "").strip().casefold()
    if not key:
        return None, "LOCAL_BOARD_SNAPSHOT_STALE"
    explicit = [
        row for row in boards
        if str(row.routing_label or "").strip().casefold() == key
    ]
    normalized_name = normalize_board_text(local_board.name)
    matched = explicit or [
        row for row in boards
        if normalize_board_text(row.name) == normalized_name
    ]
    if not matched:
        return None, "PERSISTED_PINTEREST_ROUTING_REQUIRED"
    if len(matched) != 1:
        return None, "PERSISTED_PINTEREST_ROUTING_AMBIGUOUS"

    board = matched[0]
    synced = _utc(connection.boards_last_synced_at)
    if not (
        board.is_eligible
        and connection.provider == "pinterest"
        and board.external_board_id
        and board.last_synced_at is not None
        and synced is not None
        and _utc(board.last_synced_at) == synced
        and board.id == publication.pinterest_board_record_id
        and board.external_board_id == publication.pinterest_board_id_snapshot
    ):
        return None, "PERSISTED_PINTEREST_ROUTING_STALE"
    return (connection, board, local_board), None


def _expected_item_fingerprint(item, plan) -> str | None:
    metadata = item.selection_metadata or {}
    candidate_fingerprint = (
        metadata.get("candidate_fingerprint") if isinstance(metadata, dict) else None
    )
    if not candidate_fingerprint or item.planned_date is None:
        return None
    return _hash({
        "plan_fingerprint": plan.plan_fingerprint,
        "slot_index": item.slot_index,
        "is_reserve": item.is_reserve,
        "planned_date": item.planned_date.isoformat(),
        "candidate_fingerprint": candidate_fingerprint,
    })


def _scheduled_for(db, item, settings) -> datetime | None:
    """Recompute the deterministic slot with a strict bound on same-day rows."""
    if item.planned_date is None or item.is_reserve:
        return None
    day_items = list(db.scalars(
        select(PinterestPortfolioPlanItem)
        .where(
            PinterestPortfolioPlanItem.plan_id == item.plan_id,
            PinterestPortfolioPlanItem.is_reserve.is_(False),
            PinterestPortfolioPlanItem.planned_date == item.planned_date,
        )
        .order_by(PinterestPortfolioPlanItem.slot_index, PinterestPortfolioPlanItem.id)
        .limit(MAX_SAME_DAY_ITEMS + 1)
    ).all())
    if len(day_items) > MAX_SAME_DAY_ITEMS or not day_items:
        return None

    def recommended(row):
        metadata = _optimizer_metadata(row)
        position = metadata.get("recommended_position") if metadata else None
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            return None
        return position

    if any(recommended(row) is None for row in day_items):
        return None
    ordered = sorted(day_items, key=lambda row: (recommended(row), int(row.slot_index), row.id))
    try:
        index = next(i for i, row in enumerate(ordered) if row.id == item.id)
    except StopIteration:
        return None
    start = int(settings.pinterest_autonomous_schedule_start_minute_utc)
    end = int(settings.pinterest_autonomous_schedule_end_minute_utc)
    if not 0 <= start < end <= 1439:
        return None
    span_us = (end - start) * 60 * 1_000_000
    offset_us = (span_us * (2 * index + 1)) // (2 * len(ordered))
    midnight = datetime(
        item.planned_date.year, item.planned_date.month, item.planned_date.day,
        tzinfo=timezone.utc,
    )
    return midnight + timedelta(minutes=start, microseconds=offset_us)


def scheduled_autonomous_readiness(
    db,
    portfolio_item_id: str,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    canary_context: RoutineSchedulerCanaryContext | None = None,
) -> dict:
    """Return a bounded, provider-free certificate for an existing due execution.

    This function never creates permits, claims, publications, or runs. Provider
    credentials and gateways are intentionally not read or constructed.
    """
    settings = settings or get_settings()
    if canary_context is not None:
        validate_canary_context(canary_context, settings)
    now = _utc(now or datetime.now(timezone.utc))
    checks: list[dict] = []
    item = db.get(PinterestPortfolioPlanItem, portfolio_item_id)
    if item is None:
        _check(checks, "PORTFOLIO_ITEM_EXISTS", False, "The requested portfolio item does not exist.")
        return _result(checks, now, portfolio_item_id, None, None, None)

    plan = db.get(PinterestPortfolioPlan, item.plan_id)
    _check(checks, "ACTIVE_PLAN", bool(plan and plan.status == "ACTIVE"),
           "The portfolio item must belong to an active plan.")
    _check(checks, "EXECUTABLE_ITEM", bool(
        not item.is_reserve and item.status == "SCHEDULED" and item.planned_date is not None
    ), "Only a scheduled, non-reserve plan item can be certified.")
    expected_item_fp = _expected_item_fingerprint(item, plan) if plan else None
    _check(checks, "PLAN_ITEM_FINGERPRINT_CURRENT", bool(
        expected_item_fp and expected_item_fp == item.item_fingerprint
    ), "The persisted item fingerprint must bind its current plan/date/candidate snapshot.")

    executions = list(db.scalars(
        select(PinterestAutonomousExecutionRun)
        .where(PinterestAutonomousExecutionRun.portfolio_item_id == item.id)
        .order_by(
            PinterestAutonomousExecutionRun.attempt_number.desc(),
            PinterestAutonomousExecutionRun.created_at.desc(),
            PinterestAutonomousExecutionRun.id.desc(),
        )
        .limit(1)
    ).all())
    execution = executions[0] if executions else None
    _check(checks, "LATEST_EXECUTION_PRESENT", execution is not None,
           "An existing execution run must be present for the active portfolio item.")
    optimizer_rows = list(db.scalars(
        select(PinterestOptimizerApplication)
        .where(PinterestOptimizerApplication.plan_id == item.plan_id)
        .limit(2)
    ).all())
    optimizer = optimizer_rows[0] if len(optimizer_rows) == 1 else None
    _check(checks, "EXECUTION_PERMITTED", bool(
        execution
        and execution.status == "SUCCEEDED"
        and execution.stage == "PERMITTED"
        and plan
        and execution.plan_id == plan.id
        and execution.portfolio_item_id == item.id
        and optimizer
        and execution.optimizer_application_id == optimizer.id
    ), "The existing autonomous execution must have completed through PERMITTED.")

    metadata = _optimizer_metadata(item)
    optimizer_current = bool(
        optimizer
        and plan
        and optimizer.status == "APPLIED"
        and optimizer.plan_fingerprint_snapshot == plan.plan_fingerprint
        and metadata
        and metadata.get("optimizer_fingerprint") == optimizer.optimizer_fingerprint
        and metadata.get("optimizer_policy_version") == optimizer.optimizer_policy_version
        and metadata.get("input_state_fingerprint") == optimizer.input_state_fingerprint
    )
    _check(checks, "OPTIMIZER_BINDING_CURRENT", optimizer_current,
           "The applied optimizer and item metadata must bind the current plan fingerprint.")

    publication = db.get(PinPublication, execution.publication_id) if execution and execution.publication_id else None
    permit_rows = list(db.scalars(
        select(RoutineDispatchPermit)
        .where(
            RoutineDispatchPermit.publication_id == (publication.id if publication else ""),
            RoutineDispatchPermit.status == "ACTIVE",
        )
        .order_by(RoutineDispatchPermit.authorized_at.desc(), RoutineDispatchPermit.id)
        .limit(2)
    ).all())
    permit = permit_rows[0] if len(permit_rows) == 1 else None
    _check(checks, "ACTIVE_PUBLICATION_AND_PERMIT", bool(
        publication and len(permit_rows) == 1 and item.publication_id == publication.id
        and execution and execution.publication_id == publication.id
        and execution.routine_permit_id == permit.id
    ), "A single existing active permit must bind the execution and scheduled publication.")

    scheduled = _scheduled_for(db, item, settings)
    schedule_matches = bool(
        scheduled and execution and publication and permit
        and _utc(execution.scheduled_for) == _utc(scheduled)
        and _utc(publication.scheduled_for) == _utc(scheduled)
        and _utc(permit.scheduled_for_snapshot) == _utc(scheduled)
        and publication.status == PublicationStatus.SCHEDULED
        and _utc(publication.scheduled_for) <= now
    )
    _check(checks, "SCHEDULE_SNAPSHOT_CURRENT_AND_DUE", schedule_matches,
           "Execution, publication, permit, and deterministic plan slot must agree and be due.")

    expected_execution_fp = None
    route_result = None
    if publication and item and plan and optimizer and metadata and scheduled:
        route_result, _route_error = _current_persisted_route(db, item, publication)
        if route_result:
            connection, board, _local_board = route_result
            try:
                expected_execution_fp = _execution_fingerprint(
                    item=item,
                    plan=plan,
                    optimizer=optimizer,
                    optimizer_metadata=metadata,
                    board=board,
                    scheduled_for=scheduled,
                )
            except (TypeError, ValueError):
                expected_execution_fp = None
            _check(checks, "PERSISTED_BOARD_ROUTE_UNAMBIGUOUS", True,
                   "One current persisted board route uniquely matches the plan and publication.",
                   pinterest_board_record_id=board.id)
        else:
            _check(checks, "PERSISTED_BOARD_ROUTE_UNAMBIGUOUS", False,
                   "Persisted board routing is absent, stale, or ambiguous.",
                   reason=_route_error)
    else:
        _check(checks, "PERSISTED_BOARD_ROUTE_UNAMBIGUOUS", False,
               "A unique persisted board route could not be evaluated.")
    _check(checks, "EXECUTION_FINGERPRINT_CURRENT", bool(
        expected_execution_fp and execution and execution.input_fingerprint == expected_execution_fp
    ), "The execution input fingerprint must match the current plan, optimizer, route, and schedule.")

    seo = db.get(PinterestSeoBrief, execution.seo_brief_id) if execution and execution.seo_brief_id else None
    generation = (
        db.get(PinterestAutonomousGenerationRun, execution.generation_run_id)
        if execution and execution.generation_run_id else None
    )
    seo_current = bool(
        seo and execution and generation and seo.status == "CURRENT"
        and seo.portfolio_item_id == item.id
        and generation.portfolio_item_id == item.id
        and generation.status == "SUCCEEDED"
        and generation.seo_brief_id == seo.id
        and (execution.safe_metadata or {}).get("seo_brief_id") == seo.id
        and (generation.safe_metadata or {}).get("portfolio_item_fingerprint") == item.item_fingerprint
        and (generation.safe_metadata or {}).get("seo_fingerprint") == seo.seo_fingerprint
    )
    _check(checks, "SEO_SNAPSHOT_CURRENT", seo_current,
           "Current persisted SEO and generation snapshots must bind this plan item.")

    approval = db.get(PinApproval, publication.approval_id) if publication and publication.approval_id else None
    draft = db.get(PinDraft, publication.draft_id) if publication else None
    revision = db.get(ContentRevision, publication.revision_id) if publication and publication.revision_id else None
    creative = db.get(PinCreative, publication.creative_id) if publication else None
    template = db.get(CreativeTemplate, creative.template_id) if creative else None
    concept = db.get(PinConcept, draft.concept_id) if draft else None
    product = db.get(Product, item.product_id)

    content = revision or draft
    creative_current = bool(
        publication and draft and content and creative and generation and approval
        and publication.draft_id == draft.id
        and publication.creative_id == creative.id
        and creative.draft_id == draft.id
        and generation.draft_id == draft.id
        and generation.creative_id == creative.id
        and approval.decision == "APPROVED"
        and approval.decided_by == AUTONOMOUS_ACTOR
        and approval.draft_id == draft.id
        and approval.creative_id == creative.id
        and approval.revision_id == publication.revision_id
        and publication.creative_fingerprint == creative.creative_fingerprint
        and publication.source_image_id == creative.source_image_id
        and publication.template_id == creative.template_id
        and template is not None
        and publication.template_key == template.key
        and publication.template_version == template.version
        and publication.text_fingerprint == content.text_fingerprint
        and publication.title_snapshot == content.title
        and publication.description_snapshot == content.description
        and publication.alt_text_snapshot == content.alt_text
    )
    _check(checks, "CREATIVE_AND_COPY_SNAPSHOTS_CURRENT", creative_current,
           "Approved creative and immutable publication copy must still match persisted provenance.")
    url_current = bool(
        publication and content and product and concept
        and concept.product_id == product.id == item.product_id
        and publication.destination_url == content.destination_url == product.product_url
        and publication.utm_url == content.utm_url
        and publication.utm_url
    )
    _check(checks, "SEO_URL_SNAPSHOTS_CURRENT", url_current,
           "Publication destination and UTM snapshots must match the current product and approved copy.")
    quota_ready = False
    quota_reason = None
    quota_headroom = None
    if plan and product and publication and scheduled and item.local_board_id:
        try:
            limits = scheduled_quota_limits(plan, settings, scheduled.date())
            if not product.vendor or not product.vendor.strip():
                raise ScheduledQuotaError("SCHEDULED_QUOTA_IDENTITY_REQUIRED")
            quota = assess_scheduled_quota_with_commitments(
                db, publication_id=publication.id, plan_id=plan.id,
                plan_item_id=item.id, product_id=item.product_id,
                vendor_key=product.vendor, board_id=item.local_board_id,
                scheduled_for=scheduled.date(), limits=limits,
            )
            quota_ready = quota.can_reserve or quota.already_committed
            quota_headroom = {
                "daily": quota.daily_remaining, "monthly": quota.monthly_remaining,
                "product": quota.product_remaining, "vendor": quota.vendor_remaining,
                "board": quota.board_remaining,
                "already_reserved": quota.already_reserved,
                "already_committed": quota.already_committed,
            }
            if not quota_ready:
                quota_reason = "SCHEDULED_QUOTA_LIMIT_REACHED"
        except (ScheduledQuotaError, ValueError) as exc:
            quota_reason = getattr(exc, "code", str(exc))
    _check(checks, "SCHEDULED_QUOTA_HEADROOM", quota_ready,
           "All five quota dimensions must admit this bound publication.",
           reason=quota_reason, headroom=quota_headroom)
    creative_media_current = bool(
        publication and creative
        and publication.media_url_snapshot
        and (
            creative.rendered_url == publication.media_url_snapshot
            or public_creative_url_matches(
                creative,
                publication.media_url_snapshot,
                settings=settings,
            )
        )
    )
    _check(checks, "CREATIVE_MEDIA_SNAPSHOT_CURRENT", creative_media_current,
           "Publication media URL and creative render snapshot must match.")

    permit_check = False
    permit_error = None
    if publication and permit:
        try:
            evidence = build_routine_offline_evidence(db, publication, permit=permit, now=now)
            permit_check = bool(
                permit.authorized_by == AUTONOMOUS_ACTOR
                and evidence.permit_validated
                and evidence.quality_passed
                and evidence.duplicate_safe
                and evidence.persisted_route_validated
                and evidence.external_requests == 0
            )
        except Exception as exc:
            permit_check = False
            permit_error = exc.__class__.__name__
    _check(checks, "PERMIT_OFFLINE_VALIDATION", permit_check,
           "The existing autonomous permit, quality snapshot, duplicate result, and route must validate offline.",
           validation_error=permit_error)

    control = db.get(RoutinePublishingControl, "default")
    _check(checks, "DRY_RUN_CONTROL", bool(
        control and (
            control.state == ("PAUSED" if canary_context is not None else "DRY_RUN")
        )
    ),
           "Routine control must already be in DRY_RUN; this certificate does not change it.")
    if canary_context is None:
        _check(checks, "SCHEDULED_AUTONOMY_AUTHORIZED",
               getattr(settings, "routine_scheduled_autonomy_enabled", False) is True,
               "The explicit scheduled-autonomy authorization gate must be enabled.")
        _check(checks, "SCHEDULER_AND_WORKER_ENABLED",
               settings.routine_pinterest_scheduler_enabled is True
               and settings.routine_pinterest_worker_enabled is True,
               "Scheduler and worker must be enabled for a scheduled DRY_RUN certification.")
    else:
        fingerprints_match = bool(
            publication
            and permit
            and publication.id == canary_context.target_publication_id
            and permit.id == canary_context.target_permit_id
            and publication.publication_fingerprint == canary_context.expected_publication_fingerprint
            and request_fingerprint_for(publication) == canary_context.expected_request_fingerprint
            and publication.pinterest_board_record_id == canary_context.expected_route_id
        )
        _check(checks, "CANARY_TARGET_BINDING_CURRENT", fingerprints_match,
               "The canary context must bind this publication, permit, request, and route.")
        _check(checks, "CANARY_GATES_CLOSED",
               all(getattr(settings, key, None) is False for key in (
                   "routine_pinterest_scheduler_enabled",
                   "routine_pinterest_worker_enabled",
                   "routine_scheduled_autonomy_enabled",
               )),
               "The canary may certify only while ordinary scheduler gates remain closed.")
    _check(checks, "DRY_RUN_MODE", settings.routine_pinterest_dry_run is True,
           "The worker must remain in DRY_RUN mode.")

    provider_gates = {
        "publishing_enabled": getattr(settings, "publishing_enabled", False),
        "buffer_publishing_enabled": getattr(settings, "buffer_publishing_enabled", False),
        "routine_buffer_dispatch_enabled": getattr(settings, "routine_buffer_dispatch_enabled", False),
        "routine_scheduled_live_admission_enabled": getattr(settings, "routine_scheduled_live_admission_enabled", False),
        "routine_autonomous_authorization_enabled": getattr(settings, "routine_autonomous_authorization_enabled", False),
        "pinterest_seo_brief_persistence_enabled": getattr(settings, "pinterest_seo_brief_persistence_enabled", False),
        "pinterest_autonomous_generation_enabled": getattr(settings, "pinterest_autonomous_generation_enabled", False),
        "pinterest_autonomous_execution_enabled": getattr(settings, "pinterest_autonomous_execution_enabled", False),
        "pinterest_write_scope_enabled": getattr(settings, "pinterest_write_scope_enabled", False),
        "pinterest_board_write_scope_enabled": getattr(settings, "pinterest_board_write_scope_enabled", False),
        "pinterest_board_provisioning_enabled": getattr(settings, "pinterest_board_provisioning_enabled", False),
        "pinterest_autonomous_board_ensure_enabled": getattr(settings, "pinterest_autonomous_board_ensure_enabled", False),
        "pinterest_optimizer_apply_enabled": getattr(settings, "pinterest_optimizer_apply_enabled", False),
        "pinterest_portfolio_activation_enabled": getattr(settings, "pinterest_portfolio_activation_enabled", False),
        "pinterest_analytics_ingestion_enabled": getattr(settings, "pinterest_analytics_ingestion_enabled", False),
        "pinterest_learning_snapshot_persistence_enabled": getattr(settings, "pinterest_learning_snapshot_persistence_enabled", False),
        "buffer_single_pin_pilot_enabled": getattr(settings, "buffer_single_pin_pilot_enabled", False),
        "pinterest_single_pin_pilot_enabled": getattr(settings, "pinterest_single_pin_pilot_enabled", False),
    }
    active_gates = sorted(key for key, enabled in provider_gates.items() if enabled is True)
    _check(checks, "PROVIDER_GATES_CLOSED", not active_gates,
           "Every live publishing, write-scope, provisioning, and pilot gate must remain closed.",
           active_gates=active_gates)
    _check(checks, "BUFFER_DISPATCH_DISABLED",
           settings.routine_buffer_dispatch_enabled is False,
           "DRY_RUN certification requires routine Buffer dispatch to remain disabled.")

    return _result(
        checks, now, portfolio_item_id, execution, publication, permit,
        publication_fingerprint=publication.publication_fingerprint if publication else None,
        request_fingerprint=request_fingerprint_for(publication) if publication else None,
        plan_fingerprint=plan.plan_fingerprint if plan else None,
        item_fingerprint=item.item_fingerprint,
        execution_fingerprint=execution.input_fingerprint if execution else None,
    )


def _result(
    checks: list[dict],
    now: datetime,
    item_id: str,
    execution,
    publication,
    permit,
    **fingerprints,
) -> dict:
    blockers = [check["code"] for check in checks if check["blocking"]]
    payload = {
        "contract": "SCHEDULED_AUTONOMOUS_READINESS_V1",
        "portfolio_item_id": item_id,
        "execution_run_id": execution.id if execution else None,
        "publication_id": publication.id if publication else None,
        "permit_id": permit.id if permit else None,
        "checked_at": now,
        "ready": not blockers,
        "blockers": blockers,
        "checks": checks,
        "fingerprints": fingerprints,
        "state_mutated": False,
        "provider_called": False,
        "provider_calls": 0,
        "permit_created": False,
        "live_ready": False,
        "quota_reservation_committed": False,
    }
    payload["certificate_fingerprint"] = _hash({
        "contract": payload["contract"],
        "portfolio_item_id": item_id,
        "execution_run_id": payload["execution_run_id"],
        "publication_id": payload["publication_id"],
        "permit_id": payload["permit_id"],
        "fingerprints": fingerprints,
        "blockers": blockers,
    })
    return payload