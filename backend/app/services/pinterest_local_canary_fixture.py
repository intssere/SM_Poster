"""Local-only, recoverable Task #61.6A canary fixture preparation."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.models.domain import (
    AuditLog,
    Board,
    ContentAngle,
    CreativeTemplate,
    DraftStatus,
    PinApproval,
    PinConcept,
    PinCreative,
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
    ProductImage,
    ProductIntelligence,
    PublicationStatus,
)
from app.models.routine_publishing import (
    RoutineDispatchPermit,
    RoutinePublishingControl,
)
from app.services.creative_rendering import (
    CANVAS,
    DESIGN_TOKEN_VERSION,
    TEMPLATES,
)
from app.services.fingerprints import creative_fingerprint, text_fingerprint
from app.services.local_canary_admission import publication_has_pending_local_canary_media
from app.services.pinterest_autonomous_execution import (
    EXECUTION_ACTOR,
    EXECUTION_POLICY_VERSION,
    _execution_input_fingerprint,
    _optimizer_metadata,
    deterministic_scheduled_for,
)
from app.services.pinterest_autonomous_generation import (
    CAMPAIGN_KEY,
    CREATIVE_TEMPLATES,
    GENERATION_ACTOR,
    GENERATION_POLICY_VERSION,
    autonomous_generation_readiness,
)
from app.services.pinterest_board_strategy import board_strategy
from app.services.pinterest_seo_intelligence import (
    SEO_POLICY_VERSION,
    seo_brief_preview,
)
from app.services.public_creative_media import (
    public_creative_url,
    public_creative_url_matches,
    public_origin,
)
from app.services.publication_identity import build_publication_candidate
from app.services.publication_scheduler import schedule
from app.services.routine_autonomous_authorization import (
    AUTONOMOUS_ACTOR,
    AUTONOMOUS_NOTE_PREFIX,
    autonomous_content_policy,
)
from app.services.routine_canary_fixture import (
    RoutineCanaryFixtureError,
    _assert_static_safety,
    _count,
)
from app.services.routine_dispatch_authorization import (
    active_permit,
    create_permit,
    validate_permit,
)
from app.services.routine_pinterest_scheduler import scheduler_status
from app.services.pinterest_local_canary_media import (
    DURABLE_CANARY_PROTOCOL,
    LocalCanaryMediaError,
    cleanup_orphan_staging,
    cleanup_committed_staged_creative,
    durable_media_required,
    has_local_canary_media_marker,
    immutable_render_spec_fingerprint,
    promote_staged_creative,
    read_verified_promoted,
    render_verified_local_png,
    stage_creative_png,
    verify_promoted_pending,
)
from app.services.pinterest_autonomous_generation import (
    AutonomousGenerationError,
)
from app.services.pinterest_autonomous_execution import (
    AutonomousExecutionError,
)
from app.services.pinterest_seo_intelligence import PinterestSeoError
from app.services.routine_dispatch_authorization import RoutinePermitError
from app.services.utm import build_pinterest_utm_url
from app.services.publication_identity import PublicationIdentityError


LOCAL_CANARY_PROTOCOL = "TASK61_6A_LOCAL_MEDIA_STAGED_V1"
_CLOSED_GATES = (
    "publishing_enabled",
    "buffer_publishing_enabled",
    "routine_pinterest_worker_enabled",
    "routine_buffer_dispatch_enabled",
    "routine_pinterest_scheduler_enabled",
    "routine_scheduled_live_admission_enabled",
    "routine_scheduled_autonomy_enabled",
    "routine_scheduler_canary_enabled",
    "routine_autonomous_authorization_enabled",
    "pinterest_autonomous_generation_enabled",
    "pinterest_autonomous_execution_enabled",
    "pinterest_portfolio_activation_enabled",
    "pinterest_optimizer_apply_enabled",
    "pinterest_autonomous_board_ensure_enabled",
    "pinterest_write_scope_enabled",
    "pinterest_board_write_scope_enabled",
    "pinterest_board_provisioning_enabled",
    "buffer_single_pin_pilot_enabled",
    "pinterest_single_pin_pilot_enabled",
)


class LocalCanaryFixtureError(RuntimeError):
    """The local fixture could not be prepared or safely reconciled."""


def _utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _locked(db, model, identity):
    return db.scalar(
        select(model).where(model.id == identity).with_for_update()
    )


def _global_preconditions(db, settings: Settings, now: datetime, scheduler: dict) -> None:
    control = _locked(db, RoutinePublishingControl, "default")
    if control is None:
        raise LocalCanaryFixtureError("ROUTINE_CONTROL_NOT_INITIALIZED")
    try:
        _assert_static_safety(db, settings, control, scheduler)
    except RoutineCanaryFixtureError as exc:
        raise LocalCanaryFixtureError(str(exc)) from None
    if any(getattr(settings, name) is not False for name in _CLOSED_GATES):
        raise LocalCanaryFixtureError("UNSAFE_RUNTIME_GATE_STATE")

    counts = {
        "due": _count(
            db,
            PinPublication,
            PinPublication.status == PublicationStatus.SCHEDULED,
            PinPublication.scheduled_for <= now,
        ),
        "future": _count(
            db,
            PinPublication,
            PinPublication.status == PublicationStatus.SCHEDULED,
            PinPublication.scheduled_for > now,
        ),
        "scheduled_without_time": _count(
            db,
            PinPublication,
            PinPublication.status == PublicationStatus.SCHEDULED,
            PinPublication.scheduled_for.is_(None),
        ),
        "active_permits": _count(
            db, RoutineDispatchPermit, RoutineDispatchPermit.status == "ACTIVE"
        ),
        "publish_unknown": _count(
            db,
            PinPublication,
            PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN,
        ),
        "publishing": _count(
            db,
            PinPublication,
            PinPublication.status == PublicationStatus.PUBLISHING,
        ),
    }
    if counts != {
        "due": 0,
        "future": 0,
        "scheduled_without_time": 0,
        "active_permits": 0,
        "publish_unknown": 0,
        "publishing": 0,
    }:
        raise LocalCanaryFixtureError("CANARY_FIXTURE_PRECONDITION_COUNTS_NOT_ZERO")

    if not public_origin(settings.public_media_base_url):
        raise LocalCanaryFixtureError("PUBLIC_MEDIA_BASE_URL_REQUIRED")
    configured = get_settings()
    if (
        any(
            getattr(configured, name) is not getattr(settings, name)
            for name in _CLOSED_GATES
        )
        or configured.routine_pinterest_dry_run
        is not settings.routine_pinterest_dry_run
        or configured.routine_pinterest_batch_size
        != settings.routine_pinterest_batch_size
        or configured.routine_pinterest_daily_write_limit
        != settings.routine_pinterest_daily_write_limit
    ):
        raise LocalCanaryFixtureError("DRY_RUN_GATE_CONFIGURATION_MISMATCH")
    if public_origin(configured.public_media_base_url) != public_origin(
        settings.public_media_base_url
    ):
        raise LocalCanaryFixtureError("PUBLIC_MEDIA_BASE_URL_CONFIGURATION_MISMATCH")


def _plan_identity(db, item: PinterestPortfolioPlanItem, settings: Settings):
    plan = _locked(db, PinterestPortfolioPlan, item.plan_id)
    optimizer = db.scalar(
        select(PinterestOptimizerApplication)
        .where(PinterestOptimizerApplication.plan_id == item.plan_id)
        .with_for_update()
    )
    if plan is None or plan.status != "ACTIVE":
        raise LocalCanaryFixtureError("PORTFOLIO_PLAN_NOT_ACTIVE")
    if (
        optimizer is None
        or optimizer.status != "APPLIED"
        or optimizer.plan_fingerprint_snapshot != plan.plan_fingerprint
    ):
        raise LocalCanaryFixtureError("OPTIMIZER_APPLICATION_DRIFT")
    metadata = _optimizer_metadata(item)
    expected_planned_date = (
        item.planned_date.isoformat() if item.planned_date is not None else None
    )
    if metadata is None or any(
        (
            metadata.get("optimizer_fingerprint") != optimizer.optimizer_fingerprint,
            metadata.get("optimizer_policy_version") != optimizer.optimizer_policy_version,
            metadata.get("input_state_fingerprint") != optimizer.input_state_fingerprint,
            metadata.get("target_slot_index") != item.slot_index,
            metadata.get("target_planned_date") != expected_planned_date,
        )
    ):
        raise LocalCanaryFixtureError("OPTIMIZER_ITEM_BINDING_MISMATCH")

    route = board_strategy(
        db, canonical_key=item.board_key_snapshot, settings=settings
    )
    if route.get("status") != "ROUTE_EXISTING":
        code = (route.get("blockers") or ["ROUTABLE_PINTEREST_BOARD_REQUIRED"])[0]
        raise LocalCanaryFixtureError(str(code))
    board = _locked(db, PinterestBoard, route["selected_board_id"])
    connection = _locked(db, PinterestConnection, board.connection_id) if board else None
    if (
        board is None
        or connection is None
        or connection.status != "CONNECTED"
        or board.connection_id != connection.id
        or not board.is_active
        or not board.is_eligible
        or not connection.boards_last_synced_at
        or board.last_synced_at != connection.boards_last_synced_at
        or board.external_board_id != route.get("selected_external_board_id")
    ):
        raise LocalCanaryFixtureError("PERSISTED_PINTEREST_ROUTING_STALE")
    if item.is_reserve or item.planned_date is None:
        raise LocalCanaryFixtureError("PORTFOLIO_ITEM_NOT_EXECUTABLE")
    try:
        scheduled_for = deterministic_scheduled_for(db, item, settings=settings)
    except AutonomousExecutionError as exc:
        raise LocalCanaryFixtureError(str(exc)) from None
    execution_fingerprint = _execution_input_fingerprint(
        item=item,
        plan=plan,
        optimizer=optimizer,
        optimizer_metadata=metadata,
        board_plan=route,
        scheduled_for=scheduled_for,
    )
    return plan, optimizer, route, board, connection, scheduled_for, execution_fingerprint


def _seo_row(db, item_id: str, preview: dict) -> PinterestSeoBrief:
    existing = db.scalar(
        select(PinterestSeoBrief)
        .where(PinterestSeoBrief.portfolio_item_id == item_id)
        .with_for_update()
    )
    if existing is not None:
        material_fields = (
            "policy_version",
            "input_fingerprint",
            "seo_fingerprint",
            "primary_keyword",
            "secondary_keywords",
            "intent",
            "source_evidence",
            "dimension_scores",
            "coverage_targets",
            "guidance",
            "cannibalization_warnings",
        )
        if existing.status != "CURRENT" or any(
            getattr(existing, field) != preview[field] for field in material_fields
        ):
            raise LocalCanaryFixtureError("PINTEREST_SEO_BRIEF_INPUT_DRIFT")
        return existing
    row = PinterestSeoBrief(
        portfolio_item_id=item_id,
        policy_version=SEO_POLICY_VERSION,
        input_fingerprint=preview["input_fingerprint"],
        seo_fingerprint=preview["seo_fingerprint"],
        primary_keyword=preview["primary_keyword"],
        secondary_keywords=preview["secondary_keywords"],
        intent=preview["intent"],
        source_evidence=preview["source_evidence"],
        dimension_scores=preview["dimension_scores"],
        coverage_targets=preview["coverage_targets"],
        guidance=preview["guidance"],
        cannibalization_warnings=preview["cannibalization_warnings"],
        status="CURRENT",
    )
    db.add(row)
    db.flush()
    return row


def _source_for_fixture(db, item, source_image_id, source_bytes, settings):
    readiness = autonomous_generation_readiness(db, item.id, settings=settings)
    if readiness.get("ready") is not True:
        raise LocalCanaryFixtureError(
            (readiness.get("blockers") or ["AUTONOMOUS_GENERATION_BLOCKED"])[0]
        )
    if readiness.get("source_image_id") != source_image_id:
        raise LocalCanaryFixtureError("CANARY_SOURCE_IMAGE_ID_MISMATCH")
    image = _locked(db, ProductImage, source_image_id)
    if (
        image is None
        or image.product_id != item.product_id
        or image.editorial_eligible is not True
        or not image.source_sha256
    ):
        raise LocalCanaryFixtureError("VERIFIED_LOCAL_SOURCE_REQUIRED")
    actual_source_sha = hashlib.sha256(source_bytes).hexdigest()
    if actual_source_sha != image.source_sha256:
        raise LocalCanaryFixtureError("LOCAL_SOURCE_SHA256_MISMATCH")
    return readiness, image


def _render_spec(db, item, ready, image, *, concept, draft, product, intelligence, angle, board, seo, settings):
    board_route = board_strategy(db, canonical_key=board.slug, settings=settings)
    if board_route.get("status") != "ROUTE_EXISTING":
        raise LocalCanaryFixtureError("PERSISTED_PINTEREST_ROUTING_STALE")
    visual = ready["visual_copy"]
    spec = {
        "version": DESIGN_TOKEN_VERSION,
        "design_token_version": DESIGN_TOKEN_VERSION,
        "draft_id": draft.id,
        "proposal_id": draft.id,
        "concept_id": concept.id,
        "product_id": product.id,
        "brand": intelligence.brand or product.vendor,
        "product_category": (concept.rationale or {}).get("facts_used", {}).get(
            "normalization_category"
        ),
        "image": {
            "id": image.id,
            "shopify_media_id": image.shopify_media_id,
            "provenance_url": image.source_url,
            "checksum_sha256": image.source_sha256,
            "checksum_basis": "persisted_verified_local_input",
            "source_bytes_unchanged": True,
        },
        "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
        "template_key": ready["template_key"],
        "template_version": 1,
        "headline": visual["headline"],
        "supporting_text": visual["supporting_text"],
        "content_angle": angle.name,
        "board": {
            "key": board.slug,
            "name": board.name,
            "pinterest_board_record_id": board_route["selected_board_id"],
            "pinterest_board_id": board_route["selected_external_board_id"],
        },
        "tokens": TEMPLATES.get(ready["template_key"]),
        "text_fingerprint": ready["visual_layout_fingerprint"],
        "seo_brief_id": seo.id,
        "seo_fingerprint": seo.seo_fingerprint,
        "portfolio_item_id": item.id,
        "portfolio_item_fingerprint": item.item_fingerprint,
    }
    return spec


def _pending_receipt(creative: PinCreative) -> dict:
    spec = creative.render_spec if isinstance(creative.render_spec, dict) else {}
    receipt = spec.get("local_canary_stage")
    if (
        spec.get("local_canary_protocol") != LOCAL_CANARY_PROTOCOL
        or not isinstance(receipt, dict)
        or receipt.get("protocol") != LOCAL_CANARY_PROTOCOL
    ):
        raise LocalCanaryFixtureError("LOCAL_CANARY_MEDIA_RECEIPT_INVALID")
    return receipt


def _validate_publication_snapshot(db, publication, approval, creative, settings):
    creative.render_status = "RENDERED"
    try:
        expected_url = public_creative_url(creative, settings=settings)
        if not expected_url:
            raise LocalCanaryFixtureError("PUBLIC_MEDIA_BASE_URL_REQUIRED")
        try:
            expected = build_publication_candidate(
                db,
                approval_id=approval.id,
                board_id=None,
                pinterest_connection_id=publication.pinterest_connection_id,
                pinterest_board_record_id=publication.pinterest_board_record_id,
                scheduled_for=None,
            )
        except PublicationIdentityError as exc:
            raise LocalCanaryFixtureError(str(exc)) from None
        fields = (
            "draft_id",
            "revision_id",
            "creative_id",
            "approval_id",
            "source_image_id",
            "template_id",
            "template_key",
            "template_version",
            "text_fingerprint",
            "creative_fingerprint",
            "board_id",
            "pinterest_connection_id",
            "pinterest_board_record_id",
            "pinterest_board_id_snapshot",
            "title_snapshot",
            "description_snapshot",
            "alt_text_snapshot",
            "destination_url",
            "utm_url",
            "publication_fingerprint",
        )
        if (
            expected.media_url_snapshot != expected_url
            or publication.media_url_snapshot != expected.media_url_snapshot
            or any(getattr(publication, field) != getattr(expected, field) for field in fields)
        ):
            raise LocalCanaryFixtureError("LOCAL_CANARY_PUBLICATION_SNAPSHOT_DRIFT")
    finally:
        creative.render_status = "STAGED"


def _result(item, execution, publication, permit, *, status):
    return {
        "status": status,
        "portfolio_item_id": item.id,
        "execution_run_id": execution.id,
        "generation_run_id": execution.generation_run_id,
        "creative_id": execution.safe_metadata.get("creative_id"),
        "publication_id": publication.id,
        "routine_permit_id": permit.id if permit else None,
        "scheduled_for": publication.scheduled_for,
        "provider_called": False,
        "ai_called": False,
        "external_requests": 0,
    }


def _pending_reconciliation_error(db) -> LocalCanaryFixtureError:
    error = LocalCanaryFixtureError(
        "LOCAL_CANARY_MEDIA_PENDING_RECONCILIATION_REQUIRED"
    )
    try:
        db.rollback()
    except Exception as rollback_error:
        error.rollback_error = rollback_error
    return error


def _validate_succeeded(
    db, item, execution, generation, publication, creative, approval, *,
    storage=None, settings=None,
):
    if (
        execution.status != "SUCCEEDED"
        or execution.stage != "PERMITTED"
        or generation.status != "SUCCEEDED"
        or publication.status != PublicationStatus.SCHEDULED
        or item.status != "SCHEDULED"
        or publication.scheduled_for is None
        or _utc(publication.scheduled_for) != _utc(execution.scheduled_for)
        or publication.creative_id != creative.id
        or publication.approval_id != approval.id
        or creative.render_status != "RENDERED"
    ):
        raise LocalCanaryFixtureError("LOCAL_CANARY_SUCCEEDED_STATE_DRIFT")
    receipt = _pending_receipt(creative)
    if not public_creative_url_matches(
        creative, publication.media_url_snapshot, settings=settings
    ):
        raise LocalCanaryFixtureError("LOCAL_CANARY_PUBLIC_MEDIA_MISMATCH")
    try:
        read_verified_promoted(
            creative,
            digest=receipt["artifact_sha256"],
            expected_input_fingerprint=generation.input_fingerprint,
            storage=storage,
            settings=settings,
        )
    except LocalCanaryMediaError as exc:
        raise LocalCanaryFixtureError("LOCAL_CANARY_MEDIA_PROMOTED_ARTIFACT_INVALID") from exc
    permit = db.get(RoutineDispatchPermit, execution.routine_permit_id)
    if (
        permit is None
        or permit.status != "ACTIVE"
        or permit.publication_id != publication.id
    ):
        raise LocalCanaryFixtureError("LOCAL_CANARY_ACTIVE_PERMIT_MISSING")
    return permit


def prepare_local_canary_fixture(
    db,
    portfolio_item_id: str,
    *,
    source_image_id: str,
    source_bytes: bytes,
    local_media_root: str | Path,
    actor: str,
    media_storage=None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Render and persist exactly one offline fixture, then promote/reconcile it."""
    if db.new or db.dirty or db.deleted:
        raise LocalCanaryFixtureError("UNRELATED_DATABASE_CHANGES_PRESENT")
    existing = db.scalar(
        select(PinterestAutonomousExecutionRun)
        .where(PinterestAutonomousExecutionRun.portfolio_item_id == portfolio_item_id)
        .limit(1)
    )
    if existing is not None:
        return reconcile_local_canary_fixture(
            db,
            portfolio_item_id,
            local_media_root=local_media_root,
            media_storage=media_storage,
            settings=settings,
            now=now,
        )
    if not actor:
        raise LocalCanaryFixtureError("FIXTURE_ACTOR_REQUIRED")
    settings = settings or get_settings()
    now = _utc(now)
    scheduler = scheduler_status(settings)
    if not isinstance(source_bytes, bytes) or not source_bytes:
        raise LocalCanaryFixtureError("VERIFIED_LOCAL_SOURCE_BYTES_REQUIRED")

    stage_receipt = None
    committed = False
    commit_attempted = False
    try:
        _global_preconditions(db, settings, now, scheduler)
        item = _locked(db, PinterestPortfolioPlanItem, portfolio_item_id)
        if item is None or item.status != "PLANNED" or item.publication_id is not None:
            raise LocalCanaryFixtureError("PORTFOLIO_ITEM_NOT_PLANNED")
        if item.is_reserve or not item.planned_date:
            raise LocalCanaryFixtureError("PORTFOLIO_ITEM_NOT_EXECUTABLE")
        for model, label in (
            (PinterestAutonomousGenerationRun, "GENERATION"),
            (PinterestAutonomousExecutionRun, "EXECUTION"),
        ):
            if db.scalar(
                select(model.id).where(model.portfolio_item_id == item.id).limit(1)
            ):
                raise LocalCanaryFixtureError(f"{label}_HISTORY_ALREADY_PRESENT")

        plan, optimizer, route, route_board, connection, scheduled_for, execution_fp = (
            _plan_identity(db, item, settings)
        )
        if scheduled_for > now:
            raise LocalCanaryFixtureError("DETERMINISTIC_DUE_SLOT_REQUIRED")

        seo_preview = seo_brief_preview(db, item.id, settings=settings)
        if seo_preview.get("ready") is not True:
            raise LocalCanaryFixtureError(
                (seo_preview.get("blockers") or ["PINTEREST_SEO_BRIEF_BLOCKED"])[0]
            )
        seo = _seo_row(db, item.id, seo_preview)
        generation_ready, image = _source_for_fixture(
            db, item, source_image_id, source_bytes, settings
        )
        if generation_ready["seo_fingerprint"] != seo.seo_fingerprint:
            raise LocalCanaryFixtureError("PINTEREST_SEO_BRIEF_INPUT_DRIFT")
        if (
            generation_ready.get("selected_board_record_id") != route_board.id
            or generation_ready.get("selected_external_board_id")
            != route_board.external_board_id
        ):
            raise LocalCanaryFixtureError("PERSISTED_PINTEREST_ROUTING_STALE")

        product = _locked(db, Product, item.product_id)
        intelligence = db.scalar(
            select(ProductIntelligence)
            .where(ProductIntelligence.product_id == item.product_id)
            .with_for_update()
        )
        board = _locked(db, Board, item.local_board_id)
        angle = _locked(db, ContentAngle, item.content_angle_id)
        if not all((product, intelligence, board, angle)):
            raise LocalCanaryFixtureError("GENERATION_SOURCE_IDENTITY_INCOMPLETE")

        rationale = {
            "generation_policy_version": GENERATION_POLICY_VERSION,
            "portfolio_item_id": item.id,
            "portfolio_item_fingerprint": item.item_fingerprint,
            "seo_brief_id": seo.id,
            "seo_fingerprint": seo.seo_fingerprint,
            "headline": generation_ready["visual_copy"]["headline"],
            "visual_copy": generation_ready["visual_copy"],
            "visual_layout_fingerprint": generation_ready["visual_layout_fingerprint"],
            "content_angle": angle.name,
            "content_angle_key": angle.key,
            "creative_template_key": generation_ready["template_key"],
            "creative_template": CREATIVE_TEMPLATES[generation_ready["template_key"]],
            "board_mapping": {
                "key": board.slug,
                "name": board.name,
                "pinterest_board_record_id": route["selected_board_id"],
                "pinterest_board_id": route["selected_external_board_id"],
            },
            "keywords": [seo.primary_keyword, *(seo.secondary_keywords or [])],
            "canonical_url": product.product_url,
            "authentic_image": {
                "id": image.id,
                "url": image.source_url,
                "source_sha256": image.source_sha256,
            },
            "facts_used": {
                "title": product.title,
                "brand": intelligence.brand or product.vendor,
                "audience": intelligence.audience,
                "fragrance_family": intelligence.fragrance_family,
                "normalization_status": intelligence.normalization_status,
            },
            "unsupported_claims": [],
            "warnings": list(seo.cannibalization_warnings or []),
            "missing_facts": [],
            "template_version": 1,
            "generated_status": "GENERATED",
        }
        concept = PinConcept(
            store_id=product.store_id,
            product_id=product.id,
            content_angle_id=angle.id,
            keyword_cluster_id=None,
            board_id=board.id,
            campaign_id=None,
            fingerprint=generation_ready["concept_fingerprint"],
            rationale=rationale,
        )
        db.add(concept)
        db.flush()
        rationale["concept_id"] = concept.id
        concept.rationale = rationale

        copy = generation_ready["copy"]
        draft = PinDraft(
            concept_id=concept.id,
            version=1,
            title=copy["title"],
            description=copy["description"],
            alt_text=copy["alt_text"],
            destination_url=product.product_url,
            utm_url=build_pinterest_utm_url(
                product.product_url,
                campaign=CAMPAIGN_KEY,
                content=f"{product.handle}-{angle.key}",
            ),
            text_fingerprint=text_fingerprint(
                title=copy["title"],
                description=copy["description"],
                alt_text=copy["alt_text"],
            ),
            status=DraftStatus.READY_FOR_REVIEW,
        )
        db.add(draft)
        db.flush()

        template_key = generation_ready["template_key"]
        template = db.scalar(
            select(CreativeTemplate).where(
                CreativeTemplate.key == template_key,
                CreativeTemplate.version == 1,
            )
        )
        if template is None:
            template = CreativeTemplate(
                key=template_key,
                version=1,
                name=template_key.replace("_", " ").title(),
                renderer="pillow",
                definition={
                    "renderer_active": True,
                    "authentic_product_image_required": True,
                },
                active=True,
            )
            db.add(template)
            db.flush()
        elif not template.active:
            raise LocalCanaryFixtureError("ACTIVE_CREATIVE_TEMPLATE_REQUIRED")

        spec = _render_spec(
            db,
            item,
            generation_ready,
            image,
            concept=concept,
            draft=draft,
            product=product,
            intelligence=intelligence,
            angle=angle,
            board=board,
            seo=seo,
            settings=settings,
        )
        png, render_provenance = render_verified_local_png(
            spec,
            source_bytes,
            source_image_id=image.id,
            expected_source_sha256=image.source_sha256,
        )
        creative_fp = creative_fingerprint(
            source_image_sha256=image.source_sha256,
            template_key=template.key,
            template_version=template.version,
            text_hash=generation_ready["visual_layout_fingerprint"],
            layout_parameters=spec,
        )
        creative = PinCreative(
            draft_id=draft.id,
            template_id=template.id,
            source_image_id=image.id,
            rendered_url=None,
            sha256=hashlib.sha256(png).hexdigest(),
            creative_fingerprint=creative_fp,
            width=CANVAS[0],
            height=CANVAS[1],
            render_status="PENDING",
            render_spec=spec,
            size_bytes=len(png),
        )
        db.add(creative)
        db.flush()
        artifact = stage_creative_png(
            local_media_root,
            creative_id=creative.id,
            png=png,
            source_image_id=image.id,
            source_sha256=image.source_sha256,
            input_fingerprint=generation_ready["input_fingerprint"],
            provenance={
                **render_provenance,
                "creative_fingerprint": creative_fp,
                "render_spec_fingerprint": immutable_render_spec_fingerprint({
                    **spec,
                    "local_canary_protocol": LOCAL_CANARY_PROTOCOL,
                }),
                "portfolio_item_id": item.id,
                "portfolio_item_fingerprint": item.item_fingerprint,
                "seo_brief_id": seo.id,
                "seo_fingerprint": seo.seo_fingerprint,
                "generation_input_fingerprint": generation_ready["input_fingerprint"],
                "execution_input_fingerprint": execution_fp,
                "plan_fingerprint": plan.plan_fingerprint,
                "optimizer_fingerprint": optimizer.optimizer_fingerprint,
                "route_board_id": route_board.id,
                "connection_id": connection.id,
                "scheduled_for": scheduled_for.isoformat(),
            },
        )
        stage_receipt = artifact.as_dict()
        creative.sha256 = artifact.artifact_sha256
        creative.render_status = "STAGED"
        creative.rendered_url = f"/api/pins/creatives/{creative.id}/image"
        creative.render_spec = {
            **spec,
            "local_canary_protocol": LOCAL_CANARY_PROTOCOL,
            "local_canary_stage": stage_receipt,
        }

        gen_run = PinterestAutonomousGenerationRun(
            portfolio_item_id=item.id,
            seo_brief_id=seo.id,
            input_fingerprint=generation_ready["input_fingerprint"],
            attempt_number=1,
            status="STARTED",
            concept_id=concept.id,
            draft_id=draft.id,
            creative_id=creative.id,
            safe_metadata={
                "policy_version": GENERATION_POLICY_VERSION,
                "portfolio_item_fingerprint": item.item_fingerprint,
                "seo_fingerprint": seo.seo_fingerprint,
                "concept_fingerprint": concept.fingerprint,
                "template_key": template_key,
                "visual_layout_fingerprint": generation_ready["visual_layout_fingerprint"],
                "reconciliation_id": None,
                "supersedes_run_id": None,
                "attempt_number": 1,
                "render_status": "STAGED",
                "media_protocol": LOCAL_CANARY_PROTOCOL,
                "artifact_sha256": artifact.artifact_sha256,
                "provider_called": False,
                "ai_called": False,
            },
            started_at=now,
        )
        db.add(gen_run)
        db.flush()

        # Evaluate the established approval policy against the exact immutable
        # creative that will be published, then restore the non-admissible stage.
        creative.render_status = "RENDERED"
        policy = autonomous_content_policy(db, draft.id)
        if policy.get("ready") is not True or policy.get("provider_called"):
            raise LocalCanaryFixtureError(
                (policy.get("blockers") or ["AUTONOMOUS_POLICY_BLOCKED"])[0]
            )
        creative.render_status = "STAGED"
        draft.status = DraftStatus.APPROVED
        approval = PinApproval(
            draft_id=draft.id,
            revision_id=None,
            creative_id=creative.id,
            approved_version_id="original",
            decision="APPROVED",
            decided_by=AUTONOMOUS_ACTOR,
            note=f"{AUTONOMOUS_NOTE_PREFIX}{policy['policy_fingerprint']}",
        )
        db.add(approval)
        db.flush()

        # The pure constructor validates and binds the identities.  Temporarily
        # use RENDERED only while deriving its public URL; the committed state is
        # always STAGED until the filesystem promotion succeeds.
        creative.render_status = "RENDERED"
        public_url = public_creative_url(creative, settings=settings)
        if not public_url:
            raise LocalCanaryFixtureError("PUBLIC_MEDIA_BASE_URL_REQUIRED")
        try:
            publication = build_publication_candidate(
                db,
                approval_id=approval.id,
                board_id=None,
                pinterest_connection_id=connection.id,
                pinterest_board_record_id=route_board.id,
                scheduled_for=None,
            )
        except PublicationIdentityError as exc:
            raise LocalCanaryFixtureError(str(exc)) from None
        if publication.media_url_snapshot != public_url:
            raise LocalCanaryFixtureError("PUBLIC_MEDIA_URL_SNAPSHOT_MISMATCH")
        publication.status = PublicationStatus.APPROVED
        publication.scheduled_for = None
        creative.render_status = "STAGED"
        db.add(publication)
        db.flush()

        execution = PinterestAutonomousExecutionRun(
            portfolio_item_id=item.id,
            plan_id=plan.id,
            optimizer_application_id=optimizer.id,
            input_fingerprint=execution_fp,
            attempt_number=1,
            status="STARTED",
            stage="PUBLICATION_CREATED",
            seo_brief_id=seo.id,
            generation_run_id=gen_run.id,
            approval_id=approval.id,
            publication_id=publication.id,
            scheduled_for=scheduled_for,
            safe_metadata={
                "policy_version": EXECUTION_POLICY_VERSION,
                "portfolio_item_fingerprint": item.item_fingerprint,
                "plan_fingerprint": plan.plan_fingerprint,
                "optimizer_fingerprint": optimizer.optimizer_fingerprint,
                "board_record_id": route_board.id,
                "reconciliation_id": None,
                "supersedes_run_id": None,
                "attempt_number": 1,
                "media_protocol": LOCAL_CANARY_PROTOCOL,
                "media_state": "PENDING_PROMOTION",
                "artifact_sha256": artifact.artifact_sha256,
                "creative_id": creative.id,
                "provider_called": False,
                "ai_called": False,
            },
            started_at=now,
        )
        db.add(execution)
        db.flush()
        item.publication_id = publication.id
        db.add(
            AuditLog(
                actor=actor[:255],
                action="LOCAL_CANARY_MEDIA_STAGED",
                entity_type="PinterestAutonomousExecutionRun",
                entity_id=execution.id,
                metadata_json={
                    "portfolio_item_id": item.id,
                    "publication_id": publication.id,
                    "creative_id": creative.id,
                    "artifact_sha256": artifact.artifact_sha256,
                    "media_protocol": LOCAL_CANARY_PROTOCOL,
                    "provider_called": False,
                    "external_requests": 0,
                },
            )
        )
        db.add(
            AuditLog(
                actor=GENERATION_ACTOR,
                action="AUTONOMOUS_GENERATION_STARTED",
                entity_type="PinterestAutonomousGenerationRun",
                entity_id=gen_run.id,
                metadata_json={
                    "portfolio_item_id": item.id,
                    "input_fingerprint": gen_run.input_fingerprint,
                    "media_protocol": LOCAL_CANARY_PROTOCOL,
                },
            )
        )
        db.add(
            AuditLog(
                actor=EXECUTION_ACTOR,
                action="AUTONOMOUS_EXECUTION_STARTED",
                entity_type="PinterestAutonomousExecutionRun",
                entity_id=execution.id,
                metadata_json={
                    "portfolio_item_id": item.id,
                    "plan_id": plan.id,
                    "optimizer_application_id": optimizer.id,
                    "input_fingerprint": execution.input_fingerprint,
                    "scheduled_for": scheduled_for.isoformat(),
                    "media_protocol": LOCAL_CANARY_PROTOCOL,
                },
            )
        )

        # Last pre-commit checks bind the staged receipt to the exact current
        # plan item, route, due slot, and immutable publication identities.
        if (
            item.status != "PLANNED"
            or publication.status != PublicationStatus.APPROVED
            or publication.scheduled_for is not None
            or creative.render_status != "STAGED"
            or has_local_canary_media_marker(creative) is not True
        ):
            raise LocalCanaryFixtureError("LOCAL_CANARY_PENDING_STATE_INVALID")
        commit_attempted = True
        db.commit()
        committed = True
    except Exception as exc:
        if not committed:
            db.rollback()
            if commit_attempted:
                try:
                    durable_run = db.scalar(
                        select(PinterestAutonomousExecutionRun)
                        .where(
                            PinterestAutonomousExecutionRun.portfolio_item_id
                            == portfolio_item_id
                        )
                        .limit(1)
                    )
                except Exception:
                    raise LocalCanaryFixtureError(
                        "LOCAL_CANARY_MEDIA_PENDING_RECONCILIATION_REQUIRED"
                    ) from exc
                committed = durable_run is not None
        if committed:
            raise LocalCanaryFixtureError(
                "LOCAL_CANARY_MEDIA_PENDING_RECONCILIATION_REQUIRED"
            ) from exc
        if stage_receipt is not None:
            try:
                cleanup_local_canary_orphans(db, local_media_root)
            except Exception as cleanup_error:
                error = LocalCanaryFixtureError(
                    "LOCAL_CANARY_ORPHAN_CLEANUP_FAILED"
                )
                error.cleanup_error = cleanup_error
                raise error from exc
        if isinstance(exc, LocalCanaryFixtureError):
            raise
        if isinstance(exc, (AutonomousGenerationError, AutonomousExecutionError, PinterestSeoError, RoutinePermitError, LocalCanaryMediaError)):
            raise LocalCanaryFixtureError(str(exc)) from None
        raise

    return reconcile_local_canary_fixture(
        db,
        portfolio_item_id,
        local_media_root=local_media_root,
        media_storage=media_storage,
        settings=settings,
        now=now,
    )


def _pending_rows(db, portfolio_item_id: str):
    item = _locked(db, PinterestPortfolioPlanItem, portfolio_item_id)
    execution = db.scalar(
        select(PinterestAutonomousExecutionRun)
        .where(PinterestAutonomousExecutionRun.portfolio_item_id == portfolio_item_id)
        .with_for_update()
    )
    generation = db.scalar(
        select(PinterestAutonomousGenerationRun)
        .where(PinterestAutonomousGenerationRun.portfolio_item_id == portfolio_item_id)
        .with_for_update()
    )
    if item is None or execution is None or generation is None:
        raise LocalCanaryFixtureError("LOCAL_CANARY_FIXTURE_NOT_FOUND")
    publication = _locked(db, PinPublication, execution.publication_id)
    creative = _locked(db, PinCreative, generation.creative_id)
    approval = _locked(db, PinApproval, execution.approval_id)
    if not all((publication, creative, approval)):
        raise LocalCanaryFixtureError("LOCAL_CANARY_BINDING_INCOMPLETE")
    return item, execution, generation, publication, creative, approval


def _validate_pending(
    db,
    *,
    item,
    execution,
    generation,
    publication,
    creative,
    approval,
    settings,
    now,
):
    draft = db.get(PinDraft, creative.draft_id)
    if execution.status == "SUCCEEDED":
        if (
            execution.stage != "PERMITTED"
            or generation.status != "SUCCEEDED"
            or publication.status != PublicationStatus.SCHEDULED
            or item.status != "SCHEDULED"
        ):
            raise LocalCanaryFixtureError("LOCAL_CANARY_SUCCEEDED_STATE_DRIFT")
        return None
    if (
        item.status != "PLANNED"
        or item.publication_id != publication.id
        or item.plan_id != execution.plan_id
        or execution.status != "STARTED"
        or execution.stage != "PUBLICATION_CREATED"
        or generation.status != "STARTED"
        or generation.portfolio_item_id != item.id
        or generation.creative_id != creative.id
        or creative.render_status != "STAGED"
        or publication.status != PublicationStatus.APPROVED
        or publication.scheduled_for is not None
        or approval.decision != "APPROVED"
        or approval.decided_by != AUTONOMOUS_ACTOR
        or approval.draft_id != publication.draft_id
        or approval.creative_id != creative.id
        or creative.draft_id != publication.draft_id
        or draft is None
        or draft.status != DraftStatus.APPROVED
        or publication.draft_id != draft.id
        or publication.creative_id != creative.id
        or publication.approval_id != approval.id
        or execution.publication_id != publication.id
        or execution.generation_run_id != generation.id
        or execution.approval_id != approval.id
        or execution.seo_brief_id != generation.seo_brief_id
        or active_permit(db, publication.id) is not None
    ):
        raise LocalCanaryFixtureError("LOCAL_CANARY_PENDING_STATE_INVALID")
    if publication_has_pending_local_canary_media(db, publication) is not True:
        raise LocalCanaryFixtureError("LOCAL_CANARY_PENDING_ADMISSION_MARKER_MISSING")

    receipt = _pending_receipt(creative)
    if (
        receipt.get("input_fingerprint") != generation.input_fingerprint
        or receipt.get("source_image_id") != creative.source_image_id
        or receipt.get("artifact_sha256") != creative.sha256
        or receipt.get("artifact_size") != creative.size_bytes
        or receipt.get("provenance", {}).get("execution_input_fingerprint")
        != execution.input_fingerprint
    ):
        raise LocalCanaryFixtureError("LOCAL_CANARY_MEDIA_PROVENANCE_DRIFT")
    item_current = db.get(PinterestPortfolioPlanItem, item.id)
    plan, optimizer, route, board, connection, scheduled_for, execution_fp = (
        _plan_identity(db, item_current, settings)
    )
    if (
        execution.optimizer_application_id != optimizer.id
        or publication.board_id is not None
        or publication.pinterest_connection_id != connection.id
        or publication.pinterest_board_record_id != board.id
        or publication.pinterest_board_id_snapshot != board.external_board_id
    ):
        raise LocalCanaryFixtureError("LOCAL_CANARY_ROUTE_BINDING_DRIFT")
    if scheduled_for > now or _utc(execution.scheduled_for) != _utc(scheduled_for):
        raise LocalCanaryFixtureError("DETERMINISTIC_DUE_SLOT_DRIFT")
    if execution_fp != execution.input_fingerprint:
        raise LocalCanaryFixtureError("AUTONOMOUS_EXECUTION_INPUT_DRIFT")
    _validate_publication_snapshot(db, publication, approval, creative, settings)
    gen_readiness = autonomous_generation_readiness(db, item.id, settings=settings)
    if (
        not gen_readiness.get("input_fingerprint")
        or gen_readiness["input_fingerprint"] != generation.input_fingerprint
        or gen_readiness.get("source_image_id") != creative.source_image_id
        or gen_readiness.get("seo_fingerprint") != receipt.get("provenance", {}).get("seo_fingerprint")
    ):
        raise LocalCanaryFixtureError("AUTONOMOUS_GENERATION_INPUT_DRIFT")
    image = db.get(ProductImage, creative.source_image_id)
    if (
        image is None
        or image.source_sha256 != receipt.get("source_sha256")
        or image.editorial_eligible is not True
    ):
        raise LocalCanaryFixtureError("LOCAL_CANARY_SOURCE_PROVENANCE_DRIFT")
    creative.render_status = "RENDERED"
    policy = autonomous_content_policy(db, draft.id)
    creative.render_status = "STAGED"
    if (
        policy.get("ready") is not True
        or approval.note
        != f"{AUTONOMOUS_NOTE_PREFIX}{policy.get('policy_fingerprint')}"
    ):
        raise LocalCanaryFixtureError("LOCAL_CANARY_APPROVAL_PROVENANCE_DRIFT")
    return receipt


def _pending_receipts(db) -> list[tuple[str, dict]]:
    rows = list(db.scalars(
        select(PinCreative).where(PinCreative.render_status == "STAGED")
    ).all())
    receipts = []
    for creative in rows:
        spec = creative.render_spec if isinstance(creative.render_spec, dict) else {}
        receipt = spec.get("local_canary_stage")
        if (
            spec.get("local_canary_protocol") != LOCAL_CANARY_PROTOCOL
            or not isinstance(receipt, dict)
            or receipt.get("protocol") != LOCAL_CANARY_PROTOCOL
        ):
            raise LocalCanaryFixtureError("STAGED_CREATIVE_RECEIPT_UNRECOGNIZED")
        receipts.append((creative.id, receipt))
    return receipts


def cleanup_local_canary_orphans(db, local_media_root: str | Path) -> list[str]:
    """Delete only unreferenced staging files under the shared control-row lock."""
    if db.new or db.dirty or db.deleted:
        raise LocalCanaryFixtureError("UNRELATED_DATABASE_CHANGES_PRESENT")
    try:
        control = _locked(db, RoutinePublishingControl, "default")
        if control is None:
            raise LocalCanaryFixtureError("ROUTINE_CONTROL_NOT_INITIALIZED")
        receipts = _pending_receipts(db)
        removed = cleanup_orphan_staging(
            local_media_root,
            committed_receipts=receipts,
        )
        # Cleanup is a filesystem-only operation. Roll back the read/lock
        # transaction so it can never commit fixture rows from the caller.
        db.rollback()
        return removed
    except Exception as exc:
        try:
            db.rollback()
        except Exception as rollback_error:
            error = LocalCanaryFixtureError("LOCAL_CANARY_ORPHAN_CLEANUP_FAILED")
            error.rollback_error = rollback_error
            raise error from exc
        if isinstance(exc, LocalCanaryFixtureError):
            raise
        raise LocalCanaryFixtureError("LOCAL_CANARY_ORPHAN_CLEANUP_FAILED") from exc


def reconcile_local_canary_fixture(
    db,
    portfolio_item_id: str,
    *,
    local_media_root: str | Path,
    media_storage=None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Promote and finalize one committed fixture; safe after any crash boundary."""
    try:
        settings = settings or get_settings()
        now = _utc(now)
        scheduler = scheduler_status(settings)
        item, execution, generation, publication, creative, approval = _pending_rows(
            db, portfolio_item_id
        )
        if execution.status == "SUCCEEDED":
            permit = _validate_succeeded(
                db, item, execution, generation, publication, creative, approval,
                storage=media_storage, settings=settings,
            )
            db.commit()
            return _result(
                item, execution, publication, permit, status="SUCCEEDED"
            )
        _global_preconditions(db, settings, now, scheduler)
        receipt = _validate_pending(
            db,
            item=item,
            execution=execution,
            generation=generation,
            publication=publication,
            creative=creative,
            approval=approval,
            settings=settings,
            now=now,
        )
        db.commit()
    except Exception as exc:
        raise _pending_reconciliation_error(db) from exc

    try:
        promoted_receipt = promote_staged_creative(
            local_media_root,
            creative_id=creative.id,
            receipt=receipt,
            creative=creative,
            storage=media_storage,
            settings=settings,
        )
        if durable_media_required(settings):
            if not isinstance(promoted_receipt, dict) or (
                promoted_receipt.get("durable_protocol") != DURABLE_CANARY_PROTOCOL
            ):
                raise LocalCanaryFixtureError("DURABLE_MEDIA_RECEIPT_REQUIRED")
            # Persist this recoverable receipt before any final RENDERED or
            # permit-admission transition. If this commit is interrupted, the
            # next reconciliation deterministically discovers the same object.
            creative.render_spec = {
                **(creative.render_spec if isinstance(creative.render_spec, dict) else {}),
                "local_canary_stage": promoted_receipt,
                "durable_media_protocol": DURABLE_CANARY_PROTOCOL,
            }
            db.commit()
            cleanup_committed_staged_creative(
                local_media_root,
                creative_id=creative.id,
                receipt=promoted_receipt,
            )
    except Exception as exc:
        raise _pending_reconciliation_error(db) from exc

    try:
        item, execution, generation, publication, creative, approval = _pending_rows(
            db, portfolio_item_id
        )
        if execution.status == "SUCCEEDED":
            permit = _validate_succeeded(
                db, item, execution, generation, publication, creative, approval,
                storage=media_storage, settings=settings,
            )
            db.commit()
            return _result(
                item, execution, publication, permit, status="SUCCEEDED"
            )
        _global_preconditions(db, settings, now, scheduler)
        receipt = _validate_pending(
            db,
            item=item,
            execution=execution,
            generation=generation,
            publication=publication,
            creative=creative,
            approval=approval,
            settings=settings,
            now=now,
        )
        artifact = verify_promoted_pending(
            creative,
            receipt,
            storage=media_storage,
            settings=settings,
            expected_input_fingerprint=generation.input_fingerprint,
        )
        if hashlib.sha256(artifact).hexdigest() != creative.sha256:
            raise LocalCanaryFixtureError("LOCAL_CANARY_MEDIA_DIGEST_MISMATCH")
        creative.render_status = "RENDERED"
        creative.rendered_at = now
        policy = autonomous_content_policy(db, creative.draft_id)
        if policy.get("ready") is not True or policy.get("provider_called"):
            raise LocalCanaryFixtureError(
                (policy.get("blockers") or ["AUTONOMOUS_APPROVAL_DRIFT"])[0]
            )
        if not public_creative_url(creative, settings=settings):
            raise LocalCanaryFixtureError("PUBLIC_MEDIA_BASE_URL_REQUIRED")
        db.flush()
        schedule(db, publication, execution.scheduled_for, commit=False)
        permit = create_permit(
            db,
            publication,
            actor=AUTONOMOUS_ACTOR,
            now=now,
            commit=False,
        )
        validation = validate_permit(
            db,
            publication,
            permit,
            now=now,
            expected_status=PublicationStatus.SCHEDULED,
            require_due=True,
        )
        if validation.get("valid") is not True:
            raise LocalCanaryFixtureError(
                f"ROUTINE_PERMIT_VALIDATION_FAILED:{validation.get('status')}"
            )
        generation.status = "SUCCEEDED"
        generation.completed_at = now
        generation.safe_metadata = {
            **(generation.safe_metadata or {}),
            "render_status": "RENDERED",
            "artifact_sha256": creative.sha256,
            "media_state": "PROMOTED",
            "provider_called": False,
            "ai_called": False,
        }
        db.add(
            AuditLog(
                actor=GENERATION_ACTOR,
                action="AUTONOMOUS_GENERATION_SUCCEEDED",
                entity_type="PinterestAutonomousGenerationRun",
                entity_id=generation.id,
                metadata_json={
                    "concept_id": generation.concept_id,
                    "draft_id": generation.draft_id,
                    "creative_id": creative.id,
                    "artifact_sha256": creative.sha256,
                },
            )
        )
        execution.stage = "PERMITTED"
        execution.status = "SUCCEEDED"
        execution.routine_permit_id = permit.id
        execution.completed_at = now
        execution.safe_metadata = {
            **(execution.safe_metadata or {}),
            "media_state": "PROMOTED",
            "routine_permit_id": permit.id,
            "provider_called": False,
            "ai_called": False,
        }
        item.status = "SCHEDULED"
        db.add(
            AuditLog(
                actor=EXECUTION_ACTOR,
                action="AUTONOMOUS_EXECUTION_SUCCEEDED",
                entity_type="PinterestAutonomousExecutionRun",
                entity_id=execution.id,
                metadata_json={
                    "portfolio_item_id": item.id,
                    "publication_id": publication.id,
                    "routine_permit_id": permit.id,
                    "stage": "PERMITTED",
                    "provider_called": False,
                },
            )
        )
        db.add(
            AuditLog(
                actor=EXECUTION_ACTOR,
                action="LOCAL_CANARY_MEDIA_PROMOTED",
                entity_type="PinterestAutonomousExecutionRun",
                entity_id=execution.id,
                metadata_json={
                    "portfolio_item_id": item.id,
                    "publication_id": publication.id,
                    "artifact_sha256": creative.sha256,
                    "provider_called": False,
                    "external_requests": 0,
                },
            )
        )
        db.commit()
        return _result(item, execution, publication, permit, status="SUCCEEDED")
    except Exception as exc:
        raise _pending_reconciliation_error(db) from exc