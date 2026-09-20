from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.models.domain import (
    Board,
    ContentAngle,
    KeywordCluster,
    PinConcept,
    PinDraft,
    PinterestPortfolioPlanItem,
    PinterestSeoBrief,
    Product,
    ProductIntelligence,
)

SEO_POLICY_VERSION = "PINTEREST_SEO_BRIEF_V1"
MAX_KEYWORD_CHARS = 100
MAX_KEYWORD_TERMS = 8
TITLE_MAX_CHARS = 100
DESCRIPTION_MAX_CHARS = 500


class PinterestSeoError(RuntimeError):
    pass


@dataclass(frozen=True)
class KeywordCandidate:
    phrase: str
    normalized: str
    sources: tuple[str, ...]
    dimensions: dict[str, int]
    total_score: int
    intent: str | None


def _hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_keyword(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in text)
    text = re.sub(r"[^\w$]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _valid_phrase(value: str | None) -> str | None:
    normalized = normalize_keyword(value)
    if not normalized or len(normalized) > MAX_KEYWORD_CHARS:
        return None
    terms = normalized.split()
    if not terms or len(terms) > MAX_KEYWORD_TERMS:
        return None
    return normalized


def _tokens(value: str | None) -> set[str]:
    return {part for part in normalize_keyword(value).split() if len(part) > 1}


def _product_context(product: Product, intelligence: ProductIntelligence) -> dict:
    notes = [str(item) for item in (intelligence.fragrance_notes or []) if str(item).strip()]
    brand = (intelligence.brand or product.vendor or "").strip()
    return {
        "title": product.title or "",
        "brand": brand,
        "audience": intelligence.audience or "",
        "family": intelligence.fragrance_family or "",
        "notes": notes,
        "season": intelligence.season or "",
        "occasion": intelligence.occasion or "",
        "is_arabian": bool(intelligence.arabian_classification),
        "is_designer": bool(intelligence.designer),
        "is_niche": bool(intelligence.niche),
    }


def _source_candidates(
    *,
    item: PinterestPortfolioPlanItem,
    product: Product,
    intelligence: ProductIntelligence,
    board: Board,
    angle: ContentAngle,
    clusters: list[KeywordCluster],
) -> tuple[dict[str, set[str]], dict[str, str]]:
    evidence: dict[str, set[str]] = defaultdict(set)
    intents: dict[str, str] = {}
    context = _product_context(product, intelligence)

    def add(value: str | None, source: str, intent: str | None = None):
        phrase = _valid_phrase(value)
        if phrase is None:
            return
        evidence[phrase].add(source)
        if intent and phrase not in intents:
            intents[phrase] = intent

    for keyword in item.seed_keywords or []:
        add(str(keyword), "portfolio_seed")

    title = context["title"]
    brand = context["brand"]
    family = context["family"]
    audience = context["audience"]

    add(f"{title} perfume", "product_title")
    if brand:
        add(f"{brand} perfume", "brand")
    if family:
        add(f"{family} perfume", "fragrance_family")
    if audience:
        add(f"{audience} fragrance", "audience")
    for note in context["notes"][:8]:
        add(f"{note} perfume", "fragrance_note")
    if context["is_arabian"]:
        add("arabian perfume", "product_taxonomy")
        add("arabian fragrance", "product_taxonomy")
    if context["is_niche"]:
        add("niche fragrance", "product_taxonomy")
    if context["is_designer"]:
        add("designer fragrance", "product_taxonomy")

    add(board.name, "board")
    add(board.slug, "board")
    add(angle.name, "content_angle")
    add(angle.key, "content_angle")

    context_tokens = set()
    for value in (
        title,
        brand,
        family,
        audience,
        board.name,
        board.slug,
        angle.name,
        angle.key,
        context["season"],
        context["occasion"],
        *context["notes"],
    ):
        context_tokens |= _tokens(value)

    seed_normalized = {normalize_keyword(str(k)) for k in (item.seed_keywords or [])}
    for cluster in clusters:
        cluster_terms = [
            phrase
            for phrase in (_valid_phrase(str(value)) for value in (cluster.keywords or []))
            if phrase
        ]
        cluster_relevant = False
        for phrase in cluster_terms:
            phrase_tokens = _tokens(phrase)
            if phrase in seed_normalized or bool(phrase_tokens & context_tokens):
                cluster_relevant = True
                break
        if not cluster_relevant:
            continue
        for phrase in cluster_terms:
            add(phrase, f"keyword_cluster:{cluster.key}", cluster.intent)

    return evidence, intents


def _dimensions(
    phrase: str,
    *,
    product: Product,
    intelligence: ProductIntelligence,
    board: Board,
    angle: ContentAngle,
) -> dict[str, int]:
    phrase_tokens = _tokens(phrase)
    context = _product_context(product, intelligence)

    product_tokens = set()
    for value in (
        context["title"],
        context["brand"],
        context["family"],
        context["audience"],
        *context["notes"],
    ):
        product_tokens |= _tokens(value)
    if context["is_arabian"]:
        product_tokens |= {"arabian", "perfume", "fragrance"}
    if context["is_niche"]:
        product_tokens |= {"niche", "fragrance"}
    if context["is_designer"]:
        product_tokens |= {"designer", "fragrance"}

    product_overlap = len(phrase_tokens & product_tokens)
    product_relevance = min(40, 10 * product_overlap)
    if context["brand"] and _tokens(context["brand"]) <= phrase_tokens:
        product_relevance = min(40, product_relevance + 10)

    board_overlap = len(phrase_tokens & (_tokens(board.name) | _tokens(board.slug)))
    board_fit = min(20, board_overlap * 10)

    angle_overlap = len(phrase_tokens & (_tokens(angle.name) | _tokens(angle.key)))
    angle_fit = min(20, angle_overlap * 10)

    term_count = len(phrase.split())
    specificity = 10 if 2 <= term_count <= 5 else 6 if term_count in {1, 6, 7} else 2

    repeated = term_count != len(set(phrase.split()))
    phrase_quality = 10
    if repeated:
        phrase_quality -= 4
    if len(phrase) > 80:
        phrase_quality -= 2
    if term_count == 1:
        phrase_quality -= 2
    phrase_quality = max(0, phrase_quality)

    return {
        "product_relevance": product_relevance,
        "board_fit": board_fit,
        "angle_fit": angle_fit,
        "specificity": specificity,
        "phrase_quality": phrase_quality,
    }


def _history_warnings(
    db,
    *,
    product_id: str,
    board_id: str,
    angle_id: str,
    primary_keyword: str,
) -> list[dict]:
    phrase = normalize_keyword(primary_keyword)
    rows = list(db.execute(
        select(PinDraft, PinConcept)
        .join(PinConcept, PinConcept.id == PinDraft.concept_id)
        .where(PinConcept.product_id == product_id)
        .order_by(PinDraft.created_at, PinDraft.id)
    ).all())
    warnings = []
    for draft, concept in rows:
        haystack = normalize_keyword(f"{draft.title} {draft.description}")
        if phrase and phrase in haystack:
            warnings.append({
                "code": "PRIMARY_KEYWORD_PREVIOUSLY_USED",
                "draft_id": draft.id,
                "same_board": concept.board_id == board_id,
                "same_angle": concept.content_angle_id == angle_id,
            })
    return warnings[:20]


def seo_brief_preview(
    db,
    portfolio_item_id: str,
    *,
    settings: Settings | None = None,
) -> dict:
    settings = settings or get_settings()
    item = db.get(PinterestPortfolioPlanItem, portfolio_item_id)
    if item is None:
        raise PinterestSeoError("PORTFOLIO_ITEM_NOT_FOUND")

    product = db.get(Product, item.product_id)
    intelligence = db.scalar(
        select(ProductIntelligence)
        .where(ProductIntelligence.product_id == item.product_id)
        .limit(1)
    )
    board = db.get(Board, item.local_board_id)
    angle = db.get(ContentAngle, item.content_angle_id)
    blockers = []
    if product is None:
        blockers.append("PRODUCT_NOT_FOUND")
    if intelligence is None:
        blockers.append("PRODUCT_INTELLIGENCE_REQUIRED")
    if board is None or not board.active:
        blockers.append("BOARD_INTENT_REQUIRED")
    if angle is None or not angle.active:
        blockers.append("CONTENT_ANGLE_REQUIRED")

    if blockers:
        return {
            "policy_version": SEO_POLICY_VERSION,
            "portfolio_item_id": portfolio_item_id,
            "ready": False,
            "blockers": blockers,
            "state_mutated": False,
            "provider_called": False,
            "ai_called": False,
        }

    clusters = list(db.scalars(
        select(KeywordCluster).order_by(KeywordCluster.key, KeywordCluster.id)
    ).all())
    evidence, intents = _source_candidates(
        item=item,
        product=product,
        intelligence=intelligence,
        board=board,
        angle=angle,
        clusters=clusters,
    )

    candidates: list[KeywordCandidate] = []
    for phrase in sorted(evidence):
        dimensions = _dimensions(
            phrase,
            product=product,
            intelligence=intelligence,
            board=board,
            angle=angle,
        )
        total = sum(dimensions.values())
        candidates.append(KeywordCandidate(
            phrase=phrase,
            normalized=phrase,
            sources=tuple(sorted(evidence[phrase])),
            dimensions=dimensions,
            total_score=total,
            intent=intents.get(phrase),
        ))

    candidates.sort(key=lambda row: (
        -row.total_score,
        -row.dimensions["product_relevance"],
        -row.dimensions["board_fit"],
        -row.dimensions["angle_fit"],
        row.normalized,
    ))

    relevant = [
        row for row in candidates
        if row.dimensions["product_relevance"] > 0
        and (row.dimensions["board_fit"] > 0 or row.dimensions["angle_fit"] > 0 or "portfolio_seed" in row.sources)
    ]
    if not relevant:
        return {
            "policy_version": SEO_POLICY_VERSION,
            "portfolio_item_id": portfolio_item_id,
            "ready": False,
            "blockers": ["NO_EVIDENCE_BOUND_PRIMARY_KEYWORD"],
            "candidate_count": len(candidates),
            "state_mutated": False,
            "provider_called": False,
            "ai_called": False,
        }

    primary = relevant[0]
    secondaries = []
    primary_tokens = _tokens(primary.phrase)
    for row in relevant[1:]:
        if row.normalized == primary.normalized:
            continue
        row_tokens = _tokens(row.phrase)
        if row_tokens == primary_tokens:
            continue
        if row_tokens and primary_tokens and row_tokens <= primary_tokens:
            continue
        secondaries.append(row)
        if len(secondaries) >= settings.pinterest_seo_max_secondary_keywords:
            break

    selected = [primary, *secondaries]
    intent = primary.intent or next((row.intent for row in selected if row.intent), None) or "product_discovery"
    warnings = _history_warnings(
        db,
        product_id=item.product_id,
        board_id=item.local_board_id,
        angle_id=item.content_angle_id,
        primary_keyword=primary.phrase,
    )

    evidence_payload = {
        row.phrase: {
            "sources": list(row.sources),
            "intent": row.intent,
            "score": row.total_score,
            "dimensions": row.dimensions,
        }
        for row in selected
    }
    input_payload = {
        "policy_version": SEO_POLICY_VERSION,
        "portfolio_item_id": item.id,
        "portfolio_item_fingerprint": item.item_fingerprint,
        "product_id": item.product_id,
        "product_title": product.title,
        "product_vendor": product.vendor,
        "product_intelligence": {
            "brand": intelligence.brand,
            "audience": intelligence.audience,
            "family": intelligence.fragrance_family,
            "notes": intelligence.fragrance_notes or [],
            "arabian": bool(intelligence.arabian_classification),
            "designer": bool(intelligence.designer),
            "niche": bool(intelligence.niche),
        },
        "board": {
            "id": board.id,
            "slug": board.slug,
            "name": board.name,
            "rules": board.rules or {},
        },
        "angle": {
            "id": angle.id,
            "key": angle.key,
            "name": angle.name,
            "rules": angle.rules or {},
        },
        "seed_keywords": [normalize_keyword(str(v)) for v in (item.seed_keywords or [])],
        "selected_keywords": [row.phrase for row in selected],
        "evidence": evidence_payload,
    }
    input_fingerprint = _hash(input_payload)

    coverage_targets = {
        "title": {
            "must_include": [primary.phrase],
            "may_include": [row.phrase for row in secondaries[:2]],
            "max_characters": TITLE_MAX_CHARS,
            "avoid_exact_keyword_repetition": True,
        },
        "description": {
            "must_include": [primary.phrase],
            "may_include": [row.phrase for row in secondaries],
            "max_characters": DESCRIPTION_MAX_CHARS,
            "natural_language_required": True,
        },
        "alt_text": {
            "must_describe_visible_product": True,
            "may_include": [primary.phrase],
            "keyword_stuffing_prohibited": True,
        },
    }
    guidance = {
        "primary_keyword": primary.phrase,
        "board_context": {"key": board.slug, "name": board.name},
        "angle_context": {"key": angle.key, "name": angle.name},
        "title": "Use the primary keyword once where natural; preserve product identity and search intent.",
        "description": "Lead with product relevance, cover secondary terms naturally, and avoid repetitive keyword lists.",
        "alt_text": "Describe the visible product/image first; include the primary term only when it remains accurate.",
    }
    seo_payload = {
        "input_fingerprint": input_fingerprint,
        "primary_keyword": primary.phrase,
        "secondary_keywords": [row.phrase for row in secondaries],
        "intent": intent,
        "coverage_targets": coverage_targets,
        "warnings": warnings,
    }
    seo_fingerprint = _hash(seo_payload)

    return {
        "policy_version": SEO_POLICY_VERSION,
        "portfolio_item_id": item.id,
        "ready": True,
        "blockers": [],
        "primary_keyword": primary.phrase,
        "secondary_keywords": [row.phrase for row in secondaries],
        "intent": intent,
        "source_evidence": evidence_payload,
        "dimension_scores": primary.dimensions,
        "primary_score": primary.total_score,
        "score_semantics": "deterministic_evidence_components_not_search_volume_or_rank_probability",
        "coverage_targets": coverage_targets,
        "guidance": guidance,
        "cannibalization_warnings": warnings,
        "input_fingerprint": input_fingerprint,
        "seo_fingerprint": seo_fingerprint,
        "candidate_count": len(candidates),
        "state_mutated": False,
        "provider_called": False,
        "ai_called": False,
    }


def persist_seo_brief(
    db,
    portfolio_item_id: str,
    *,
    settings: Settings | None = None,
) -> PinterestSeoBrief:
    settings = settings or get_settings()
    if settings.pinterest_seo_brief_persistence_enabled is not True:
        raise PinterestSeoError("PINTEREST_SEO_BRIEF_PERSISTENCE_DISABLED")

    preview = seo_brief_preview(db, portfolio_item_id, settings=settings)
    if preview.get("ready") is not True:
        raise PinterestSeoError((preview.get("blockers") or ["PINTEREST_SEO_BRIEF_BLOCKED"])[0])

    existing = db.scalar(
        select(PinterestSeoBrief)
        .where(PinterestSeoBrief.portfolio_item_id == portfolio_item_id)
        .limit(1)
    )
    if existing is not None:
        if (
            existing.input_fingerprint == preview["input_fingerprint"]
            and existing.seo_fingerprint == preview["seo_fingerprint"]
            and existing.status == "CURRENT"
        ):
            return existing
        raise PinterestSeoError("PINTEREST_SEO_BRIEF_INPUT_DRIFT")

    row = PinterestSeoBrief(
        portfolio_item_id=portfolio_item_id,
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
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(PinterestSeoBrief)
            .where(PinterestSeoBrief.portfolio_item_id == portfolio_item_id)
            .limit(1)
        )
        if (
            existing
            and existing.input_fingerprint == preview["input_fingerprint"]
            and existing.seo_fingerprint == preview["seo_fingerprint"]
        ):
            return existing
        raise PinterestSeoError("PINTEREST_SEO_BRIEF_INPUT_DRIFT") from None
    db.refresh(row)
    return row
