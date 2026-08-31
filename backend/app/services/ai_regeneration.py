"""Provider-neutral, review-only regeneration with immutable revision lineage."""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlparse

from sqlalchemy import delete, func, select, text as sql_text

from app.db.session import SessionLocal
from app.models.domain import (
    AISettings,
    AIRequestTelemetry,
    ContentRevision,
    ContentVersionSelection,
    CreativeTemplate,
    DraftStatus,
    PinConcept,
    PinCreative,
    PinDraft,
    Product,
    ProductImage,
    ProductIntelligence,
)
from app.services.creative_rendering import CreativeRenderError, CreativeRenderService, TEMPLATES
from app.services.pin_proposals import CREATIVE_TEMPLATES
from app.services.fingerprints import text_fingerprint
from app.services.ai_providers import (
    ProviderUnavailable,
    TextGenerationResult,
    provider_for_settings,
    provider_status,
    normalize_local_base_url,
)


PROVIDER_MODES = ("disabled", "local_free", "hosted_paid")
DEFAULT_PRICING = {
    "gpt-4o-mini": {"input_per_1m": 0.15, "output_per_1m": 0.60},
}
UNSUPPORTED_CLAIM_PATTERNS = (
    re.compile(r"\b(?:best|#1|number one|popular|trending|viral|bestseller)\b", re.I),
    re.compile(r"\b(?:sale|discount|deal|save|limited time|exclusive)\b", re.I),
    re.compile(r"\b(?:long[- ]lasting|long lasting|all[- ]day|projection|sillage|compliment)\b", re.I),
    re.compile(r"\b(?:mood|seasonal|for summer|for winter|date night)\b", re.I),
)


class AIRegenerationError(ValueError):
    """A safe, user-facing regeneration error."""


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _claims(text: str, product: Product) -> list[str]:
    scrubbed = text
    for source in (product.title, product.vendor or ""):
        if source:
            scrubbed = re.sub(re.escape(source), "", scrubbed, flags=re.I)
    return sorted({pattern.pattern for pattern in UNSUPPORTED_CLAIM_PATTERNS if pattern.search(scrubbed)})


def _verified_image(image: ProductImage, product_id: str, rationale: dict[str, Any]) -> dict[str, Any]:
    persisted = rationale.get("authentic_image") or {}
    parsed = urlparse(image.source_url)
    if (
        image.product_id != product_id
        or not image.shopify_media_id
        or not image.editorial_eligible
        or image.source_url != persisted.get("url")
        or parsed.scheme != "https"
        or parsed.hostname != "cdn.shopify.com"
    ):
        raise AIRegenerationError("The proposal does not have a verified authentic Shopify image.")
    return {
        "id": image.id,
        "shopify_media_id": image.shopify_media_id,
        "provenance_url": image.source_url,
        "checksum_sha256": image.source_sha256,
        "verification": "content_checksum_verified" if image.source_sha256 else "persisted_shopify_metadata",
        "checksum_basis": "persisted" if image.source_sha256 else "not_yet_rendered",
        "generated_background": False,
    }


def _facts(product: Product, intelligence: ProductIntelligence, image: ProductImage) -> dict[str, Any]:
    normalized = intelligence.normalized_data or {}
    facts: dict[str, Any] = {
        "title": product.title,
        "product_url": product.product_url,
        "normalization_category": normalized.get("normalization_category", "other"),
        "normalization_status": intelligence.normalization_status or "UNKNOWN",
        "image": image.source_url,
    }
    for field in (
        "brand", "audience", "designer", "niche", "arabian_classification",
        "fragrance_family", "concentration", "size", "price_band", "gift_suitability",
    ):
        value = getattr(intelligence, field, None)
        if value:
            facts[field] = value
    return facts


def _snapshot(
    *,
    draft: PinDraft,
    rationale: dict[str, Any],
    facts: dict[str, Any],
    image: ProductImage,
    creative: PinCreative | None,
    template_key: str | None = None,
) -> dict[str, Any]:
    creative_spec = (creative.render_spec or {}) if creative else {}
    return {
        "headline": rationale.get("headline", ""),
        "title": draft.title,
        "description": draft.description,
        "alt_text": draft.alt_text,
        "cta": rationale.get("cta", "Shop the authentic product"),
        "content_angle": rationale.get("content_angle", ""),
        "content_angle_key": rationale.get("content_angle_key", ""),
        "creative_template": rationale.get("creative_template", ""),
        "creative_template_key": template_key or rationale.get("creative_template_key", ""),
        "destination_url": draft.destination_url,
        "utm_url": draft.utm_url,
        "keywords": list(rationale.get("keywords") or []),
        "facts_used": facts,
        "warnings": list(rationale.get("warnings") or []),
        "missing_facts": list(rationale.get("missing_facts") or []),
        "unsupported_claims": list(rationale.get("unsupported_claims") or []),
        "text_fingerprint": draft.text_fingerprint,
        "creative_fingerprint": creative.creative_fingerprint if creative else None,
        "creative_id": creative.id if creative else None,
        "source_image_id": image.id,
        "provenance": creative_spec.get("image") or _verified_image(image, image.product_id, rationale),
    }


def _copy_variant(
    draft: PinDraft | None,
    rationale: dict[str, Any],
    product: Product,
    intelligence: ProductIntelligence,
    version: int,
) -> dict[str, Any]:
    vendor = _clean(intelligence.brand or product.vendor)
    title = _clean(product.title)
    angle = _clean(rationale.get("content_angle") or "Editorial Product Pick")
    headline = f"{angle}: {vendor or title} — Alternate {version}"
    copy_title = f"{title} | {angle} (Alternate {version})"
    clauses = [f"Explore {title}{f' by {vendor}' if vendor and vendor.lower() not in title.lower() else ''} at Diamond Shelf."]
    if intelligence.audience:
        clauses.append(f"The catalog lists this product for {intelligence.audience}.")
    if intelligence.concentration:
        clauses.append(f"Listed concentration: {intelligence.concentration}.")
    if intelligence.fragrance_family and (intelligence.normalized_data or {}).get("normalization_category") == "fragrance":
        clauses.append(f"Listed fragrance family: {intelligence.fragrance_family}.")
    if intelligence.size:
        clauses.append(f"Catalog size: {intelligence.size}.")
    if intelligence.gift_suitability:
        clauses.append("The catalog identifies this as a gift set.")
    clauses.append("This alternate edit uses only persisted catalog information.")
    description = " ".join(clauses)[:800].rstrip()
    alt_text = f"{title}{f' by {vendor}' if vendor else ''} — authentic Shopify product image for Diamond Shelf; alternate edit {version}"
    output = {
        "headline": headline[:500].rstrip(),
        "title": copy_title[:500].rstrip(),
        "description": description,
        "alt_text": alt_text[:500].rstrip(),
        "cta": _clean(rationale.get("cta") or "Shop the authentic product"),
    }
    if _claims(" ".join(output.values()), product):
        raise AIRegenerationError("Safe regeneration detected an unsupported claim and was not saved.")
    return output


def _provider_prompt(
    product: Product,
    intelligence: ProductIntelligence,
    rationale: dict[str, Any],
) -> str:
    facts = _facts(product, intelligence, type("_Image", (), {"source_url": (rationale.get("authentic_image") or {}).get("url")})())
    return (
        "Create one alternate social copy edit for the catalog product below. "
        "This is TEXT ONLY: do not generate, redraw, describe invented, or alter product imagery. "
        "Use only the supplied facts. Do not add claims about popularity, performance, discounts, seasons, "
        "moods, exclusivity, or rankings. Return JSON with exactly headline, title, description, and alt_text. "
        "Keep headline/title/alt_text <= 500 characters and description <= 800 characters.\n"
        f"Persisted facts: {json.dumps(facts, sort_keys=True)}\n"
        f"Existing content angle: {rationale.get('content_angle', '')}\n"
    )


def _parse_provider_copy(text: str) -> dict[str, str]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.I)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise AIRegenerationError("The AI provider returned invalid structured copy.") from exc
    if not isinstance(parsed, dict):
        raise AIRegenerationError("The AI provider returned an invalid copy object.")
    fields = ("headline", "title", "description", "alt_text")
    output = {field: _clean(parsed.get(field)) for field in fields}
    if any(not output[field] for field in fields):
        raise AIRegenerationError("The AI provider returned incomplete copy.")
    limits = {"headline": 500, "title": 500, "description": 800, "alt_text": 500}
    if any(len(output[field]) > limits[field] for field in fields):
        raise AIRegenerationError("The AI provider returned copy over the safe length limit.")
    return output


def _words(value: str) -> set[str]:
    return set(re.findall(r"[^\W\d_]+", value.lower(), flags=re.UNICODE))


def _validate_provider_copy(
    output: dict[str, str],
    product: Product,
    intelligence: ProductIntelligence,
    rationale: dict[str, Any],
) -> None:
    combined = " ".join(output.values())
    if _claims(combined, product):
        raise AIRegenerationError("Safe regeneration detected an unsupported claim and was not saved.")
    known_anchor = _clean(product.title).lower()
    if known_anchor not in combined.lower():
        raise AIRegenerationError("Safe regeneration did not retain the persisted product title.")
    # A provider may use connective language, but numeric and catalog-specific
    # assertions must be traceable to persisted product/intelligence values.
    persisted_text = " ".join(
        _clean(value)
        for value in (
            product.title,
            product.vendor,
            product.product_type,
            intelligence.brand,
            intelligence.audience,
            intelligence.designer,
            intelligence.niche,
            intelligence.fragrance_family,
            intelligence.concentration,
            intelligence.size,
            intelligence.price_band,
        )
        if value
    ).lower()
    for number in re.findall(r"\b\d+(?:[.,]\d+)?\b", combined):
        if number not in persisted_text:
            raise AIRegenerationError("Safe regeneration introduced a numeric claim not present in persisted facts.")
    trusted_text = " ".join(
        _clean(value)
        for value in (
            product.title,
            product.vendor,
            product.product_type,
            intelligence.brand,
            intelligence.audience,
            intelligence.designer,
            intelligence.niche,
            intelligence.fragrance_family,
            intelligence.concentration,
            intelligence.size,
            intelligence.price_band,
            rationale.get("headline"),
            rationale.get("title"),
            rationale.get("description"),
            rationale.get("alt_text"),
            rationale.get("cta"),
            rationale.get("content_angle"),
        )
        if value
    )
    safe_connective = _words(
        "a an and as at authentic browse by catalog details discover edit explore for from "
        "image in is it its of on or our product products see shop shopify shown social the "
        "this to using verified view with"
    )
    unsupported_words = sorted(_words(combined) - _words(trusted_text) - safe_connective)
    if unsupported_words:
        raise AIRegenerationError(
            "Safe regeneration introduced unsupported catalog wording: "
            + ", ".join(unsupported_words[:8])
        )


def _pricing(settings: AISettings, model: str) -> dict[str, float] | None:
    configured = (settings.pricing_metadata or {}).get(model)
    if configured is None:
        configured = DEFAULT_PRICING.get(model)
    if not isinstance(configured, dict):
        return None
    try:
        return {
            "input_per_1m": float(configured["input_per_1m"]),
            "output_per_1m": float(configured["output_per_1m"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _cost(prompt_tokens: int | None, completion_tokens: int | None, pricing: dict[str, float] | None) -> float | None:
    if not pricing:
        return None
    prompt = prompt_tokens or 0
    completion = completion_tokens or 0
    return round(
        prompt / 1_000_000 * pricing["input_per_1m"]
        + completion / 1_000_000 * pricing["output_per_1m"],
        8,
    )


def _estimated_tokens(prompt: str) -> tuple[int, int]:
    return max(1, len(prompt) // 4), 500


class AISettingsService:
    def __init__(self, session_factory: Callable = SessionLocal):
        self.session_factory = session_factory

    @staticmethod
    def _row(db: Any) -> AISettings:
        row = db.scalar(select(AISettings).order_by(AISettings.id))
        if not row:
            row = AISettings(enabled=False, provider_mode="disabled", decorative_backgrounds_enabled=False)
            db.add(row)
            db.flush()
        return row

    @staticmethod
    def serialize(row: AISettings) -> dict[str, Any]:
        effective = row.provider_mode if row.enabled else "disabled"
        hosted_configured = bool(os.getenv("OPENAI_API_KEY"))
        return {
            "enabled": row.enabled,
            "provider_mode": row.provider_mode,
            "effective_mode": effective,
            "provider_label": {
                "disabled": "AI disabled",
                "local_free": "Local / free provider",
                "hosted_paid": "Paid hosted provider",
            }[effective],
            "available_provider_modes": [
                {"id": "disabled", "label": "Disabled by default", "available": True},
                {"id": "local_free", "label": "Local / free provider", "available": True},
                {"id": "hosted_paid", "label": "Paid hosted provider", "available": hosted_configured},
            ],
            "capabilities": {
                "copy_regeneration": True,
                "creative_template_variants": True,
                "decorative_backgrounds": False,
                "hosted_provider_configured": hosted_configured,
            },
            "decorative_backgrounds_enabled": False,
            "credentials_configured": hosted_configured,
            "local_base_url": row.local_base_url,
            "local_model": row.local_model,
            "hosted_model": row.hosted_model,
            "request_timeout_seconds": row.request_timeout_seconds,
            "daily_budget_usd": row.daily_budget_usd,
            "monthly_budget_usd": row.monthly_budget_usd,
            "pricing_metadata": row.pricing_metadata or {},
        }

    def get(self) -> dict[str, Any]:
        db = self.session_factory()
        try:
            row = self._row(db)
            db.commit()
            return self.serialize(row)
        finally:
            db.close()

    def update(
        self,
        *,
        enabled: bool | None = None,
        provider_mode: str | None = None,
        decorative_backgrounds_enabled: bool | None = None,
        local_base_url: str | None = None,
        local_model: str | None = None,
        hosted_model: str | None = None,
        request_timeout_seconds: int | None = None,
        daily_budget_usd: float | None = None,
        monthly_budget_usd: float | None = None,
    ) -> dict[str, Any]:
        requested_mode = provider_mode
        if requested_mode is not None and requested_mode not in PROVIDER_MODES:
            raise AIRegenerationError("Unsupported AI provider mode.")
        if decorative_backgrounds_enabled:
            raise AIRegenerationError("Decorative AI backgrounds are reserved for a future safe provider.")
        if request_timeout_seconds is not None and not 1 <= request_timeout_seconds <= 120:
            raise AIRegenerationError("Request timeout must be between 1 and 120 seconds.")
        if daily_budget_usd is not None and daily_budget_usd < 0:
            raise AIRegenerationError("Daily budget cannot be negative.")
        if monthly_budget_usd is not None and monthly_budget_usd < 0:
            raise AIRegenerationError("Monthly budget cannot be negative.")
        db = self.session_factory()
        try:
            row = self._row(db)
            if enabled is not None:
                row.enabled = bool(enabled and (requested_mode or row.provider_mode) != "disabled")
            elif requested_mode is not None:
                row.enabled = requested_mode != "disabled"
            if requested_mode is not None:
                row.provider_mode = requested_mode
            row.decorative_backgrounds_enabled = False
            if local_base_url is not None:
                try:
                    row.local_base_url = normalize_local_base_url(local_base_url)
                except ValueError as exc:
                    raise AIRegenerationError(str(exc)) from exc
            if local_model is not None:
                row.local_model = local_model.strip() or row.local_model
            if hosted_model is not None:
                row.hosted_model = hosted_model.strip() or row.hosted_model
            if request_timeout_seconds is not None:
                row.request_timeout_seconds = request_timeout_seconds
            if daily_budget_usd is not None:
                row.daily_budget_usd = daily_budget_usd
            if monthly_budget_usd is not None:
                row.monthly_budget_usd = monthly_budget_usd
            db.commit()
            return self.serialize(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def status(self) -> dict[str, Any]:
        db = self.session_factory()
        try:
            row = self._row(db)
            status = provider_status(row)
            db.commit()
            return {
                **status,
                "effective_mode": row.provider_mode if row.enabled else "disabled",
                "model": status.get("model"),
                "timeout_seconds": row.request_timeout_seconds,
            }
        finally:
            db.close()

    def usage(self) -> dict[str, Any]:
        db = self.session_factory()
        try:
            row = self._row(db)
            now = datetime.now(timezone.utc)
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

            def spend(since: datetime) -> float:
                value = db.scalar(
                    select(func.coalesce(func.sum(func.coalesce(
                        AIRequestTelemetry.actual_cost_usd,
                        AIRequestTelemetry.estimated_cost_usd,
                        0.0,
                    )), 0.0)).where(
                        AIRequestTelemetry.provider == "openai",
                        AIRequestTelemetry.created_at >= since,
                    )
                )
                return round(float(value or 0.0), 8)

            recent = db.scalars(
                select(AIRequestTelemetry)
                .order_by(AIRequestTelemetry.created_at.desc())
                .limit(20)
            ).all()
            return {
                "daily": {"spent_usd": spend(day_start), "limit_usd": row.daily_budget_usd},
                "monthly": {"spent_usd": spend(month_start), "limit_usd": row.monthly_budget_usd},
                "recent": [
                    {
                        "id": item.id,
                        "provider": item.provider,
                        "model": item.model,
                        "operation": item.operation,
                        "prompt_tokens": item.prompt_tokens,
                        "completion_tokens": item.completion_tokens,
                        "total_tokens": item.total_tokens,
                        "latency_ms": item.latency_ms,
                        "success": item.success,
                        "failure_code": item.failure_code,
                        "estimated_cost_usd": item.estimated_cost_usd,
                        "actual_cost_usd": item.actual_cost_usd,
                        "fallback_used": item.fallback_used,
                        "created_at": item.created_at,
                    }
                    for item in recent
                ],
            }
        finally:
            db.close()


class AIRegenerationService:
    def __init__(
        self,
        session_factory: Callable = SessionLocal,
        creative_renderer: CreativeRenderService | None = None,
        provider_factory: Callable = provider_for_settings,
    ):
        self.session_factory = session_factory
        self.creative_renderer = creative_renderer or CreativeRenderService(session_factory)
        self.provider_factory = provider_factory

    def _settings(self, db: Any) -> AISettings:
        return AISettingsService._row(db)

    @staticmethod
    def _provider_mode(row: AISettings) -> str:
        return row.provider_mode if row.enabled else "disabled"

    @staticmethod
    def _spend(db: Any, since: datetime) -> float:
        value = db.scalar(
            select(func.coalesce(func.sum(func.coalesce(
                AIRequestTelemetry.actual_cost_usd,
                AIRequestTelemetry.estimated_cost_usd,
                0.0,
            )), 0.0)).where(
                AIRequestTelemetry.provider == "openai",
                AIRequestTelemetry.created_at >= since,
            )
        )
        return float(value or 0.0)

    def _telemetry(
        self,
        db: Any,
        *,
        provider: str,
        model: str,
        started: float,
        result: TextGenerationResult | None = None,
        estimated_cost: float | None = None,
        actual_cost: float | None = None,
        failure_code: str | None = None,
        fallback_used: bool = False,
    ) -> AIRequestTelemetry:
        telemetry = AIRequestTelemetry(
            provider=provider,
            model=model,
            operation="copy_regeneration",
            prompt_tokens=result.prompt_tokens if result else None,
            completion_tokens=result.completion_tokens if result else None,
            total_tokens=result.total_tokens if result else None,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            success=failure_code is None,
            failure_code=failure_code,
            estimated_cost_usd=estimated_cost,
            actual_cost_usd=actual_cost,
            fallback_used=fallback_used,
        )
        db.add(telemetry)
        db.flush()
        return telemetry

    def _copy_from_provider(
        self,
        db: Any,
        settings: AISettings,
        product: Product,
        intelligence: ProductIntelligence,
        rationale: dict[str, Any],
        version: int,
    ) -> tuple[dict[str, str], str, str, AIRequestTelemetry | None, float | None, float | None]:
        mode = self._provider_mode(settings)
        if mode == "disabled":
            return _copy_variant(
                None, rationale, product, intelligence, version
            ), "disabled", "deterministic_fallback", None, None, None

        provider = self.provider_factory(settings)
        if provider is None:
            return _copy_variant(None, rationale, product, intelligence, version), mode, "fallback_unavailable", None, None, None
        prompt = _provider_prompt(product, intelligence, rationale)
        estimated_prompt, estimated_completion = _estimated_tokens(prompt)
        pricing = _pricing(settings, provider.model)
        estimated_cost = _cost(estimated_prompt, estimated_completion, pricing)
        if mode == "hosted_paid":
            if db.get_bind().dialect.name == "postgresql":
                db.execute(sql_text("SELECT pg_advisory_xact_lock(:key)"), {"key": 4_714_920_025})
            now = datetime.now(timezone.utc)
            daily = self._spend(db, now.replace(hour=0, minute=0, second=0, microsecond=0))
            monthly = self._spend(db, now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
            failure_code = None
            if pricing is None or estimated_cost is None:
                failure_code = "pricing_unavailable"
            elif settings.daily_budget_usd <= 0 or settings.monthly_budget_usd <= 0:
                failure_code = "budget_exceeded"
            elif daily + estimated_cost > settings.daily_budget_usd or monthly + estimated_cost > settings.monthly_budget_usd:
                failure_code = "budget_exceeded"
            if failure_code:
                telemetry = self._telemetry(
                    db, provider="openai", model=provider.model, started=time.perf_counter(),
                    estimated_cost=estimated_cost, failure_code=failure_code, fallback_used=True,
                )
                return _copy_variant(None, rationale, product, intelligence, version), mode, f"fallback_{failure_code}", telemetry, estimated_cost, None
        started = time.perf_counter()
        try:
            result = provider.generate(prompt)
        except ProviderUnavailable as exc:
            telemetry = self._telemetry(
                db, provider=provider.name, model=provider.model, started=started,
                estimated_cost=estimated_cost, failure_code=exc.code, fallback_used=True,
            )
            return _copy_variant(None, rationale, product, intelligence, version), mode, f"fallback_{exc.code}"[:40], telemetry, estimated_cost, None
        except Exception:
            telemetry = self._telemetry(
                db, provider=provider.name, model=provider.model, started=started,
                estimated_cost=estimated_cost, failure_code="provider_error", fallback_used=True,
            )
            return _copy_variant(None, rationale, product, intelligence, version), mode, "fallback_provider_error", telemetry, estimated_cost, None
        usage_estimated_cost = _cost(result.prompt_tokens, result.completion_tokens, pricing)
        if usage_estimated_cost is not None:
            estimated_cost = usage_estimated_cost
        actual_cost = None  # Text APIs do not return a billed amount in generation responses.
        try:
            output = _parse_provider_copy(result.text)
            _validate_provider_copy(output, product, intelligence, rationale)
            output["cta"] = _clean(rationale.get("cta") or "Shop the authentic product")
        except AIRegenerationError:
            self._telemetry(
                db, provider=provider.name, model=provider.model, started=started,
                result=result, estimated_cost=estimated_cost, actual_cost=actual_cost,
                failure_code="fact_safety_rejected",
            )
            db.commit()
            raise
        telemetry = self._telemetry(
            db, provider=provider.name, model=provider.model, started=started,
            result=result, estimated_cost=estimated_cost, actual_cost=actual_cost,
        )
        return output, mode, "provider_generated", telemetry, estimated_cost, actual_cost

    def _source(self, db: Any, draft_id: str) -> tuple[PinDraft, PinConcept, Product, ProductIntelligence, ProductImage, dict[str, Any], PinCreative | None]:
        row = db.execute(
            select(PinDraft, PinConcept, Product, ProductIntelligence)
            .join(PinConcept, PinConcept.id == PinDraft.concept_id)
            .join(Product, Product.id == PinConcept.product_id)
            .join(ProductIntelligence, ProductIntelligence.product_id == Product.id)
            .where(PinDraft.id == draft_id)
            .with_for_update()
        ).first()
        if not row:
            raise AIRegenerationError("Proposal was not found.")
        draft, concept, product, intelligence = row
        if draft.status != DraftStatus.READY_FOR_REVIEW:
            raise AIRegenerationError("Only proposals in REVIEW can be regenerated.")
        rationale = concept.rationale or {}
        image = db.get(ProductImage, (rationale.get("authentic_image") or {}).get("id"))
        if not image:
            raise AIRegenerationError("The proposal's persisted product image was not found.")
        _verified_image(image, product.id, rationale)
        original = db.scalar(select(PinCreative).where(PinCreative.draft_id == draft.id).order_by(PinCreative.created_at.asc(), PinCreative.id))
        return draft, concept, product, intelligence, image, rationale, original

    @staticmethod
    def _parent(db: Any, draft_id: str) -> ContentRevision | None:
        selection = db.scalar(select(ContentVersionSelection).where(ContentVersionSelection.draft_id == draft_id))
        if selection:
            return db.get(ContentRevision, selection.revision_id)
        return None

    @staticmethod
    def _next_version(db: Any, draft_id: str) -> int:
        current = db.scalar(select(func.max(ContentRevision.version)).where(ContentRevision.draft_id == draft_id)) or 1
        return int(current) + 1

    def regenerate_copy(self, draft_id: str) -> dict[str, Any]:
        db = self.session_factory()
        try:
            draft, concept, product, intelligence, image, rationale, original = self._source(db, draft_id)
            settings = self._settings(db)
            version = self._next_version(db, draft.id)
            copy, provider_mode, generation_mode, telemetry, estimated_cost, actual_cost = self._copy_from_provider(
                db, settings, product, intelligence, rationale, version
            )
            facts = _facts(product, intelligence, image)
            parent = self._parent(db, draft.id)
            revision = ContentRevision(
                draft_id=draft.id,
                parent_revision_id=parent.id if parent else None,
                version=version,
                revision_kind="COPY",
                status="REVIEW",
                **copy,
                content_angle=rationale.get("content_angle", ""),
                content_angle_key=rationale.get("content_angle_key", ""),
                creative_template=rationale.get("creative_template", ""),
                creative_template_key=rationale.get("creative_template_key", ""),
                destination_url=draft.destination_url,
                utm_url=draft.utm_url,
                keywords=list(rationale.get("keywords") or []),
                facts_used=facts,
                warnings=list(rationale.get("warnings") or []),
                missing_facts=list(rationale.get("missing_facts") or []),
                unsupported_claims=[],
                provenance=_verified_image(image, product.id, rationale),
                text_fingerprint=text_fingerprint(
                    title=copy["title"], description=copy["description"], alt_text=copy["alt_text"]
                ),
                creative_fingerprint=None,
                creative_id=None,
                source_image_id=image.id,
                provider_mode=provider_mode,
                generation_mode=generation_mode,
                reason="copy_regeneration",
                ai_telemetry_id=telemetry.id if telemetry else None,
                estimated_cost_usd=estimated_cost,
                actual_cost_usd=actual_cost,
            )
            db.add(revision)
            db.commit()
            return self.revision_payload(revision, None, None)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def regenerate_creative(self, draft_id: str, template_key: str) -> dict[str, Any]:
        if template_key not in TEMPLATES:
            raise AIRegenerationError("Unsupported creative template.")
        db = self.session_factory()
        try:
            draft, concept, product, intelligence, image, rationale, original = self._source(db, draft_id)
            provider_mode = self._provider_mode(self._settings(db))
            generation_mode = "deterministic_template"
            parent = self._parent(db, draft.id)
            if parent:
                base = {
                    "headline": parent.headline,
                    "title": parent.title,
                    "description": parent.description,
                    "alt_text": parent.alt_text,
                    "cta": parent.cta,
                    "content_angle": parent.content_angle,
                    "content_angle_key": parent.content_angle_key,
                    "creative_template": parent.creative_template,
                    "creative_template_key": parent.creative_template_key,
                    "destination_url": parent.destination_url,
                    "utm_url": parent.utm_url,
                    "keywords": parent.keywords,
                    "facts_used": parent.facts_used,
                    "warnings": parent.warnings,
                    "missing_facts": parent.missing_facts,
                    "unsupported_claims": parent.unsupported_claims,
                    "text_fingerprint": parent.text_fingerprint,
                }
            else:
                base = _snapshot(
                    draft=draft, rationale=rationale, facts=_facts(product, intelligence, image),
                    image=image, creative=original,
                )
            if template_key == base["creative_template_key"]:
                raise AIRegenerationError("Choose a different creative template for a new variant.")
            render_result = self.creative_renderer.render_variant(
                draft.id,
                template_key,
                snapshot=base,
                db=db,
            )
            creative = db.get(PinCreative, render_result["creative_id"])
            if not creative:
                raise AIRegenerationError("The rendered creative variant was not found.")
            version = self._next_version(db, draft.id)
            revision = ContentRevision(
                draft_id=draft.id,
                parent_revision_id=parent.id if parent else None,
                version=version,
                revision_kind="CREATIVE",
                status="REVIEW",
                headline=base["headline"],
                title=base["title"],
                description=base["description"],
                alt_text=base["alt_text"],
                cta=base["cta"],
                content_angle=base["content_angle"],
                content_angle_key=base["content_angle_key"],
                creative_template=CREATIVE_TEMPLATES.get(template_key, template_key),
                creative_template_key=template_key,
                destination_url=draft.destination_url,
                utm_url=draft.utm_url,
                keywords=base["keywords"],
                facts_used=base["facts_used"],
                warnings=base["warnings"],
                missing_facts=base["missing_facts"],
                unsupported_claims=[],
                provenance={
                    **((creative.render_spec or {}).get("image") or _verified_image(image, product.id, rationale)),
                    "generated_background": False,
                    "background_source": "deterministic_template_tokens",
                },
                text_fingerprint=base["text_fingerprint"],
                creative_fingerprint=creative.creative_fingerprint,
                creative_id=creative.id,
                source_image_id=image.id,
                provider_mode=provider_mode,
                generation_mode=generation_mode,
                reason="creative_template_variant",
            )
            db.add(revision)
            db.commit()
            return self.revision_payload(revision, creative, None)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def creative_payload(creative: PinCreative | None) -> dict[str, Any] | None:
        if not creative:
            return None
        return {
            "id": creative.id,
            "status": creative.render_status,
            "image_url": creative.rendered_url,
            "error": creative.render_error,
            "width": creative.width,
            "height": creative.height,
            "size_bytes": creative.size_bytes,
            "render_duration_ms": creative.render_duration_ms,
            "duration_ms": creative.render_duration_ms,
            "creative_fingerprint": creative.creative_fingerprint,
            "sha256": creative.sha256,
            "template_version": (creative.render_spec or {}).get("template_version"),
            "specification": creative.render_spec,
        }

    @classmethod
    def revision_payload(cls, revision: ContentRevision, creative: PinCreative | None, active_id: str | None) -> dict[str, Any]:
        return {
            "id": revision.id,
            "version": revision.version,
            "kind": revision.revision_kind,
            "status": revision.status,
            "parent_revision_id": revision.parent_revision_id,
            "active": revision.id == active_id,
            "headline": revision.headline,
            "title": revision.title,
            "description": revision.description,
            "alt_text": revision.alt_text,
            "cta": revision.cta,
            "creative_template": revision.creative_template,
            "creative_template_key": revision.creative_template_key,
            "text_fingerprint": revision.text_fingerprint,
            "creative_fingerprint": revision.creative_fingerprint,
            "facts_used": revision.facts_used,
            "warnings": revision.warnings,
            "missing_facts": revision.missing_facts,
            "unsupported_claims": revision.unsupported_claims,
            "provenance": revision.provenance,
            "provider_mode": revision.provider_mode,
            "generation_mode": revision.generation_mode,
            "reason": revision.reason,
            "ai_telemetry_id": revision.ai_telemetry_id,
            "estimated_cost_usd": revision.estimated_cost_usd,
            "actual_cost_usd": revision.actual_cost_usd,
            "created_at": revision.created_at,
            "creative": cls.creative_payload(creative),
        }

    def versions(self, draft_id: str) -> dict[str, Any]:
        db = self.session_factory()
        try:
            draft, concept, product, intelligence, image, rationale, original = self._source(db, draft_id)
            selection = db.scalar(select(ContentVersionSelection).where(ContentVersionSelection.draft_id == draft.id))
            active_id = selection.revision_id if selection else None
            facts = rationale.get("facts_used") or _facts(product, intelligence, image)
            original_snapshot = _snapshot(
                draft=draft, rationale=rationale, facts=facts, image=image, creative=original
            )
            originals = {
                "id": None, "version": 1, "kind": "ORIGINAL", "status": "REVIEW",
                "parent_revision_id": None, "active": active_id is None,
                **{key: original_snapshot[key] for key in ("headline", "title", "description", "alt_text", "cta", "creative_template", "creative_template_key", "text_fingerprint", "creative_fingerprint", "facts_used", "warnings", "missing_facts", "unsupported_claims", "provenance")},
                "provider_mode": "deterministic_original", "generation_mode": "original_persisted",
                "reason": "original_persisted", "created_at": draft.created_at,
                "creative": self.creative_payload(original),
            }
            revisions = []
            for revision in db.scalars(
                select(ContentRevision).where(ContentRevision.draft_id == draft.id).order_by(ContentRevision.version)
            ):
                revisions.append(self.revision_payload(
                    revision, db.get(PinCreative, revision.creative_id) if revision.creative_id else None, active_id
                ))
            return {"draft_id": draft.id, "active_version": selection.revision_id if selection else None, "active_version_number": selection and db.get(ContentRevision, selection.revision_id).version or 1, "versions": [originals, *revisions], "publishing_enabled": False}
        finally:
            db.close()

    def select_version(self, draft_id: str, version_id: str) -> dict[str, Any]:
        db = self.session_factory()
        try:
            draft = db.get(PinDraft, draft_id)
            if not draft:
                raise AIRegenerationError("Proposal was not found.")
            if draft.status != DraftStatus.READY_FOR_REVIEW:
                raise AIRegenerationError("Only proposals in REVIEW can select a version.")
            existing = db.scalar(select(ContentVersionSelection).where(ContentVersionSelection.draft_id == draft_id))
            if version_id == "original":
                if existing:
                    db.delete(existing)
            else:
                revision = db.get(ContentRevision, version_id)
                if not revision or revision.draft_id != draft_id:
                    raise AIRegenerationError("Revision was not found for this proposal.")
                if revision.status != "REVIEW":
                    raise AIRegenerationError("Only revisions in REVIEW can become active.")
                if existing:
                    existing.revision_id = revision.id
                    existing.selected_by = "manual_dashboard_action"
                else:
                    db.add(ContentVersionSelection(
                        draft_id=draft_id,
                        revision_id=revision.id,
                        selected_by="manual_dashboard_action",
                    ))
            db.commit()
            return self.versions(draft_id)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()