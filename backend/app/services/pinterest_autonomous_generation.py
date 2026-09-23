from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.models.domain import (
    AuditLog,
    Board,
    ContentAngle,
    DraftStatus,
    PinConcept,
    PinDraft,
    PinterestAutonomousGenerationRun,
    PinterestPortfolioPlanItem,
    PinterestSeoBrief,
    Product,
    ProductImage,
    ProductIntelligence,
)
from app.services.creative_rendering import (
    CreativeRenderError,
    CreativeRenderService,
    creative_text_layout_preflight,
)
from app.services.fingerprints import concept_fingerprint, text_fingerprint
from app.services.pin_proposals import CREATIVE_TEMPLATES, UNSUPPORTED_CLAIM_PATTERNS
from app.services.pinterest_board_strategy import board_strategy
from app.services.pinterest_autonomous_run_lineage import (
    latest_run,
    next_attempt_context,
    retry_reconciliation,
)
from app.services.pinterest_seo_intelligence import normalize_keyword
from app.services.utm import build_pinterest_utm_url

GENERATION_POLICY_VERSION = "PINTEREST_AUTONOMOUS_GENERATION_V1"
GENERATION_ACTOR = "autonomous-generation-v1"
CAMPAIGN_KEY = "pinterest-autonomous-v1"


class AutonomousGenerationError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _template_for_angle(angle: ContentAngle) -> str:
    key = (angle.key or "").casefold()
    if "luxury" in key:
        return "luxury_product_spotlight"
    if "gift" in key:
        return "gift_guide_gift_set"
    if any(token in key for token in ("arabian", "designer", "niche", "men-", "women-", "unisex")):
        return "product_classification"
    return "editorial_product_pick"


def _select_authentic_image(db, product_id: str) -> tuple[ProductImage | None, list[str]]:
    rows = list(db.scalars(
        select(ProductImage)
        .where(
            ProductImage.product_id == product_id,
            ProductImage.editorial_eligible.is_(True),
        )
        .order_by(ProductImage.is_primary.desc(), ProductImage.id)
    ).all())
    valid = []
    for image in rows:
        parsed = urlparse(image.source_url or "")
        if (
            image.shopify_media_id
            and parsed.scheme == "https"
            and parsed.hostname == "cdn.shopify.com"
        ):
            valid.append(image)

    primaries = [image for image in valid if image.is_primary]
    if len(primaries) == 1:
        return primaries[0], []
    if len(primaries) > 1:
        return None, ["AUTHENTIC_IMAGE_AMBIGUOUS"]
    if len(valid) == 1:
        return valid[0], []
    if not valid:
        return None, ["AUTHENTIC_IMAGE_REQUIRED"]
    return None, ["AUTHENTIC_IMAGE_AMBIGUOUS"]


def _copy(
    *,
    product: Product,
    intelligence: ProductIntelligence,
    seo: PinterestSeoBrief,
) -> dict[str, str]:
    primary = normalize_keyword(seo.primary_keyword)
    if not primary:
        raise AutonomousGenerationError("PRIMARY_SEO_KEYWORD_REQUIRED")

    brand = (intelligence.brand or product.vendor or "").strip()
    primary_display = " ".join(word.capitalize() for word in primary.split())

    raw_title = f"{product.title} | {primary_display}"
    if len(raw_title) <= 100:
        title = raw_title
    else:
        prefix = f"{primary_display} | "
        available = max(1, 100 - len(prefix))
        title = prefix + product.title[:available].rstrip()

    by_brand = ""
    if brand and brand.casefold() not in (product.title or "").casefold():
        by_brand = f" by {brand}"

    clauses = [
        f"{primary_display}: explore {product.title}{by_brand} at Diamond Shelf."
    ]
    if intelligence.fragrance_family:
        clauses.append(f"Catalog fragrance family: {intelligence.fragrance_family}.")
    if intelligence.audience:
        clauses.append(f"Catalog audience: {intelligence.audience}.")
    description = " ".join(clauses)[:500].rstrip()

    alt_text = f"{product.title}{by_brand} product image for Diamond Shelf"[:500].rstrip()

    combined = " ".join((title, description, alt_text))
    for pattern in UNSUPPORTED_CLAIM_PATTERNS:
        if pattern.search(combined):
            raise AutonomousGenerationError("UNSUPPORTED_CLAIM_DETECTED")

    if primary not in normalize_keyword(f"{title} {description}"):
        raise AutonomousGenerationError("PRIMARY_SEO_KEYWORD_MISSING")
    if normalize_keyword(title).count(primary) > 1 or normalize_keyword(description).count(primary) > 1:
        raise AutonomousGenerationError("SEO_KEYWORD_REPETITION")

    return {
        "title": title,
        "description": description,
        "alt_text": alt_text,
    }


def _compact_product_identity(title: str) -> str:
    value = " ".join((title or "").split()).strip()
    value = re.sub(
        r"\s*[\u2013\u2014-]\s*\d+(?:\.\d+)?\s*(?:fl\.?\s*oz|oz|ml|g)\.?\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()
    return value


def _controlled_render_failure_code(value: Exception | str) -> str:
    message = str(value).strip()
    code = re.sub(r"[^A-Z0-9]+", "_", message.upper()).strip("_")
    return (code or "CREATIVE_RENDER_ERROR")[:120]


def _visual_copy(
    *,
    product: Product,
    intelligence: ProductIntelligence,
    seo: PinterestSeoBrief,
    template_key: str,
) -> dict[str, object]:
    primary = normalize_keyword(seo.primary_keyword)
    if not primary:
        raise AutonomousGenerationError("PRIMARY_SEO_KEYWORD_REQUIRED")
    primary_display = " ".join(word.capitalize() for word in primary.split())
    compact_title = _compact_product_identity(product.title) or product.title.strip()
    brand = (intelligence.brand or product.vendor or "").strip()

    candidates = [
        {"headline": compact_title, "supporting_text": primary_display},
        {"headline": primary_display, "supporting_text": compact_title},
    ]
    if brand and brand.casefold() not in primary_display.casefold():
        candidates.append(
            {"headline": brand, "supporting_text": primary_display}
        )

    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate["headline"], candidate["supporting_text"])
        if not all(key) or key in seen:
            continue
        seen.add(key)
        try:
            layout = creative_text_layout_preflight(
                template_key=template_key,
                headline=candidate["headline"],
                supporting_text=candidate["supporting_text"],
                product_category=None,
            )
        except CreativeRenderError:
            continue
        layout_fingerprint = _hash({
            "template_key": template_key,
            "headline": candidate["headline"],
            "supporting_text": candidate["supporting_text"],
            "layout": layout,
        })
        return {
            **candidate,
            "layout": layout,
            "layout_fingerprint": layout_fingerprint,
        }

    raise AutonomousGenerationError("CREATIVE_TEXT_LAYOUT_UNFIT")


def _existing_run(db, portfolio_item_id: str):
    return latest_run(db, PinterestAutonomousGenerationRun, portfolio_item_id)


def autonomous_generation_readiness(
    db,
    portfolio_item_id: str,
    *,
    settings: Settings | None = None,
) -> dict:
    settings = settings or get_settings()
    item = db.get(PinterestPortfolioPlanItem, portfolio_item_id)
    if item is None:
        raise AutonomousGenerationError("PORTFOLIO_ITEM_NOT_FOUND")

    blockers: list[str] = []
    if item.is_reserve:
        blockers.append("RESERVE_ITEM_NOT_PROMOTED")
    if item.status not in {"PLANNED", "GENERATED"}:
        blockers.append("PORTFOLIO_ITEM_NOT_PLANNED")

    seo = db.scalar(
        select(PinterestSeoBrief)
        .where(
            PinterestSeoBrief.portfolio_item_id == item.id,
            PinterestSeoBrief.status == "CURRENT",
        )
        .limit(1)
    )
    if seo is None:
        blockers.append("CURRENT_SEO_BRIEF_REQUIRED")

    product = db.get(Product, item.product_id)
    intelligence = db.scalar(
        select(ProductIntelligence)
        .where(ProductIntelligence.product_id == item.product_id)
        .limit(1)
    )
    board = db.get(Board, item.local_board_id)
    angle = db.get(ContentAngle, item.content_angle_id)

    if product is None:
        blockers.append("PRODUCT_NOT_FOUND")
    if (
        intelligence is None
        or intelligence.eligibility_status != "ELIGIBLE"
        or intelligence.inventory_eligible is not True
        or intelligence.image_available is not True
    ):
        blockers.append("PRODUCT_NOT_AUTONOMOUSLY_ELIGIBLE")
    if board is None or not board.active or board.slug != item.board_key_snapshot:
        blockers.append("PORTFOLIO_BOARD_DRIFT")
    if angle is None or not angle.active or angle.key != item.angle_key_snapshot:
        blockers.append("PORTFOLIO_ANGLE_DRIFT")

    image, image_blockers = _select_authentic_image(db, item.product_id)
    blockers.extend(image_blockers)

    board_plan = None
    if board is not None:
        try:
            board_plan = board_strategy(
                db,
                canonical_key=board.slug,
                settings=settings,
            )
        except Exception:
            board_plan = {"status": "BLOCKED", "blockers": ["BOARD_STRATEGY_ERROR"]}
        if board_plan.get("status") != "ROUTE_EXISTING":
            blockers.append("ROUTABLE_PINTEREST_BOARD_REQUIRED")

    template_key = _template_for_angle(angle) if angle is not None else None
    if template_key not in CREATIVE_TEMPLATES:
        blockers.append("CREATIVE_TEMPLATE_UNSUPPORTED")

    copy = None
    visual_copy = None
    if not blockers and product and intelligence and seo:
        try:
            copy = _copy(product=product, intelligence=intelligence, seo=seo)
            visual_copy = _visual_copy(
                product=product,
                intelligence=intelligence,
                seo=seo,
                template_key=template_key,
            )
        except AutonomousGenerationError as exc:
            blockers.append(str(exc))

    concept_fp = None
    input_fingerprint = None
    if product and board and angle and seo and image and copy and visual_copy and board_plan:
        concept_fp = concept_fingerprint(
            product_ids=[product.id],
            content_angle=angle.key,
            keyword_cluster="|".join([seo.primary_keyword, *(seo.secondary_keywords or [])]),
            board_id=board.slug,
        )
        input_fingerprint = _hash({
            "policy_version": GENERATION_POLICY_VERSION,
            "portfolio_item_id": item.id,
            "portfolio_item_fingerprint": item.item_fingerprint,
            "seo_brief_id": seo.id,
            "seo_fingerprint": seo.seo_fingerprint,
            "product_id": product.id,
            "board_id": board.id,
            "board_key": board.slug,
            "pinterest_board_record_id": board_plan.get("selected_board_id"),
            "pinterest_external_board_id": board_plan.get("selected_external_board_id"),
            "content_angle_id": angle.id,
            "angle_key": angle.key,
            "source_image_id": image.id,
            "source_image_sha256": image.source_sha256,
            "template_key": template_key,
            "copy": copy,
            "visual_copy": {
                "headline": visual_copy["headline"],
                "supporting_text": visual_copy["supporting_text"],
                "layout_fingerprint": visual_copy["layout_fingerprint"],
            },
            "concept_fingerprint": concept_fp,
        })

    existing = _existing_run(db, item.id)
    already_generated = False
    retry_reconciliation_record = None
    if existing is not None:
        if input_fingerprint and existing.input_fingerprint == input_fingerprint and existing.status == "SUCCEEDED":
            already_generated = True
            if item.status != "GENERATED":
                blockers.append("GENERATION_RUN_DRIFT")
            if not (existing.concept_id and existing.draft_id and existing.creative_id):
                blockers.append("GENERATION_RUN_DRIFT")
        elif existing.status == "STARTED":
            blockers.append("GENERATION_ALREADY_STARTED")
        elif existing.status == "FAILED":
            retry_reconciliation_record = retry_reconciliation(
                db,
                kind="generation",
                failed_run=existing,
                retry_input_fingerprint=input_fingerprint,
            )
            if retry_reconciliation_record is None:
                blockers.append("GENERATION_FAILED_RECONCILIATION_REQUIRED")
        else:
            blockers.append("GENERATION_INPUT_DRIFT")

    if concept_fp and (
        existing is None
        or (
            existing.status == "FAILED"
            and retry_reconciliation_record is not None
        )
    ):
        conflict = db.scalar(
            select(PinConcept.id)
            .where(PinConcept.fingerprint == concept_fp)
            .limit(1)
        )
        if conflict is not None:
            blockers.append("AUTONOMOUS_CONCEPT_DUPLICATE")

    return {
        "policy_version": GENERATION_POLICY_VERSION,
        "portfolio_item_id": item.id,
        "ready": not blockers,
        "already_generated": already_generated,
        "blockers": blockers,
        "seo_brief_id": seo.id if seo else None,
        "seo_fingerprint": seo.seo_fingerprint if seo else None,
        "primary_keyword": seo.primary_keyword if seo else None,
        "selected_board_record_id": board_plan.get("selected_board_id") if board_plan else None,
        "selected_external_board_id": board_plan.get("selected_external_board_id") if board_plan else None,
        "source_image_id": image.id if image else None,
        "template_key": template_key,
        "copy": copy,
        "visual_copy": (
            {
                "headline": visual_copy["headline"],
                "supporting_text": visual_copy["supporting_text"],
            }
            if visual_copy
            else None
        ),
        "visual_layout_fingerprint": (
            visual_copy["layout_fingerprint"] if visual_copy else None
        ),
        "visual_layout": visual_copy["layout"] if visual_copy else None,
        "concept_fingerprint": concept_fp,
        "input_fingerprint": input_fingerprint,
        "existing_run_id": existing.id if existing else None,
        "existing_run_status": existing.status if existing else None,
        "existing_attempt_number": existing.attempt_number if existing else None,
        "retry_reconciliation_id": (
            retry_reconciliation_record.id if retry_reconciliation_record else None
        ),
        "next_attempt_number": (
            existing.attempt_number + 1
            if existing is not None and retry_reconciliation_record is not None
            else 1 if existing is None else existing.attempt_number
        ),
        "state_mutated": False,
        "provider_called": False,
        "ai_called": False,
    }


def execute_autonomous_generation(
    db,
    portfolio_item_id: str,
    *,
    settings: Settings | None = None,
    renderer: CreativeRenderService | None = None,
    now: datetime | None = None,
) -> PinterestAutonomousGenerationRun:
    settings = settings or get_settings()
    if settings.pinterest_autonomous_generation_enabled is not True:
        raise AutonomousGenerationError("AUTONOMOUS_GENERATION_DISABLED")

    ready = autonomous_generation_readiness(db, portfolio_item_id, settings=settings)
    existing = _existing_run(db, portfolio_item_id)
    if ready.get("already_generated") and ready.get("ready") and existing is not None:
        return existing
    if ready.get("ready") is not True:
        raise AutonomousGenerationError(
            (ready.get("blockers") or ["AUTONOMOUS_GENERATION_BLOCKED"])[0]
        )
    attempt_number, supersedes_run_id, reconciliation_id = next_attempt_context(
        db,
        kind="generation",
        latest=existing,
        retry_input_fingerprint=ready["input_fingerprint"],
    )
    if existing is not None and existing.status == "FAILED" and reconciliation_id is None:
        raise AutonomousGenerationError("GENERATION_FAILED_RECONCILIATION_REQUIRED")

    item = db.get(PinterestPortfolioPlanItem, portfolio_item_id)
    seo = db.get(PinterestSeoBrief, ready["seo_brief_id"])
    product = db.get(Product, item.product_id)
    intelligence = db.scalar(
        select(ProductIntelligence)
        .where(ProductIntelligence.product_id == item.product_id)
        .limit(1)
    )
    board = db.get(Board, item.local_board_id)
    angle = db.get(ContentAngle, item.content_angle_id)
    image = db.get(ProductImage, ready["source_image_id"])
    now = now or _now()

    run = PinterestAutonomousGenerationRun(
        portfolio_item_id=item.id,
        seo_brief_id=seo.id,
        input_fingerprint=ready["input_fingerprint"],
        attempt_number=attempt_number,
        supersedes_run_id=supersedes_run_id,
        status="STARTED",
        safe_metadata={
            "policy_version": GENERATION_POLICY_VERSION,
            "portfolio_item_fingerprint": item.item_fingerprint,
            "seo_fingerprint": seo.seo_fingerprint,
            "concept_fingerprint": ready["concept_fingerprint"],
            "template_key": ready["template_key"],
            "visual_layout_fingerprint": ready["visual_layout_fingerprint"],
            "reconciliation_id": reconciliation_id,
            "supersedes_run_id": supersedes_run_id,
            "attempt_number": attempt_number,
            "provider_called": False,
            "ai_called": False,
        },
        started_at=now,
    )
    db.add(run)
    db.flush()
    db.add(AuditLog(
        actor=GENERATION_ACTOR,
        action="AUTONOMOUS_GENERATION_STARTED",
        entity_type="PinterestAutonomousGenerationRun",
        entity_id=run.id,
        metadata_json={
            "portfolio_item_id": item.id,
            "seo_brief_id": seo.id,
            "input_fingerprint": run.input_fingerprint,
            "attempt_number": run.attempt_number,
            "supersedes_run_id": run.supersedes_run_id,
            "reconciliation_id": reconciliation_id,
        },
    ))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        current = _existing_run(db, portfolio_item_id)
        if (
            current
            and current.input_fingerprint == ready["input_fingerprint"]
            and current.attempt_number == attempt_number
            and current.status in {"STARTED", "SUCCEEDED"}
        ):
            if current.status == "SUCCEEDED":
                return current
            run = current
        else:
            raise AutonomousGenerationError("AUTONOMOUS_GENERATION_ALREADY_EXISTS") from None

    try:
        rationale = {
            "generation_policy_version": GENERATION_POLICY_VERSION,
            "portfolio_item_id": item.id,
            "portfolio_item_fingerprint": item.item_fingerprint,
            "seo_brief_id": seo.id,
            "seo_fingerprint": seo.seo_fingerprint,
            "headline": ready["visual_copy"]["headline"],
            "visual_copy": ready["visual_copy"],
            "visual_layout_fingerprint": ready["visual_layout_fingerprint"],
            "content_angle": angle.name,
            "content_angle_key": angle.key,
            "creative_template": CREATIVE_TEMPLATES[ready["template_key"]],
            "creative_template_key": ready["template_key"],
            "board_mapping": {
                "key": board.slug,
                "name": board.name,
                "pinterest_board_record_id": ready["selected_board_record_id"],
                "pinterest_board_id": ready["selected_external_board_id"],
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
            fingerprint=ready["concept_fingerprint"],
            rationale=rationale,
        )
        db.add(concept)
        db.flush()
        rationale["concept_id"] = concept.id
        concept.rationale = rationale

        utm_url = build_pinterest_utm_url(
            product.product_url,
            campaign=CAMPAIGN_KEY,
            content=f"{product.handle}-{angle.key}",
        )
        draft = PinDraft(
            concept_id=concept.id,
            version=1,
            title=ready["copy"]["title"],
            description=ready["copy"]["description"],
            alt_text=ready["copy"]["alt_text"],
            destination_url=product.product_url,
            utm_url=utm_url,
            text_fingerprint=text_fingerprint(
                title=ready["copy"]["title"],
                description=ready["copy"]["description"],
                alt_text=ready["copy"]["alt_text"],
            ),
            status=DraftStatus.READY_FOR_REVIEW,
        )
        db.add(draft)
        db.flush()

        render_service = renderer or CreativeRenderService()
        rendered = render_service.render_variant(
            draft.id,
            ready["template_key"],
            snapshot={
                "headline": ready["visual_copy"]["headline"],
                "title": ready["visual_copy"]["supporting_text"],
                "text_fingerprint": ready["visual_layout_fingerprint"],
            },
            db=db,
        )
        if rendered.get("status") not in {"RENDERED", "EXISTING"} or not rendered.get("creative_id"):
            raise AutonomousGenerationError(
                _controlled_render_failure_code(
                    rendered.get("error") or "Creative variant could not be rendered."
                )
            )

        run = db.get(PinterestAutonomousGenerationRun, run.id)
        run.status = "SUCCEEDED"
        run.concept_id = concept.id
        run.draft_id = draft.id
        run.creative_id = rendered["creative_id"]
        run.completed_at = now
        run.safe_metadata = {
            **(run.safe_metadata or {}),
            "render_status": rendered.get("status"),
        }
        item.status = "GENERATED"
        db.add(AuditLog(
            actor=GENERATION_ACTOR,
            action="AUTONOMOUS_GENERATION_SUCCEEDED",
            entity_type="PinterestAutonomousGenerationRun",
            entity_id=run.id,
            metadata_json={
                "concept_id": concept.id,
                "draft_id": draft.id,
                "creative_id": rendered["creative_id"],
            },
        ))
        db.commit()
        db.refresh(run)
        return run
    except Exception as exc:
        db.rollback()
        failed = db.get(PinterestAutonomousGenerationRun, run.id)
        if failed is not None and failed.status == "STARTED":
            failed.status = "FAILED"
            failed.completed_at = now
            failure_code = (
                str(exc)[:120]
                if isinstance(exc, AutonomousGenerationError)
                else _controlled_render_failure_code(exc)
                if isinstance(exc, CreativeRenderError)
                else exc.__class__.__name__[:120]
            )
            failed.safe_metadata = {
                **(failed.safe_metadata or {}),
                "failure_code": failure_code,
            }
            db.add(AuditLog(
                actor=GENERATION_ACTOR,
                action="AUTONOMOUS_GENERATION_FAILED",
                entity_type="PinterestAutonomousGenerationRun",
                entity_id=failed.id,
                metadata_json={
                    "failure_code": failed.safe_metadata["failure_code"],
                },
            ))
            db.commit()
        if isinstance(exc, AutonomousGenerationError):
            raise
        if isinstance(exc, CreativeRenderError):
            raise AutonomousGenerationError(
                _controlled_render_failure_code(exc)
            ) from None
        raise AutonomousGenerationError("AUTONOMOUS_GENERATION_FAILED") from None
