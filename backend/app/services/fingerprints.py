import hashlib
import json
from collections.abc import Iterable


def _sha256(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def concept_fingerprint(
    *, product_ids: Iterable[str], content_angle: str, keyword_cluster: str | None,
    board_id: str | None, destination_type: str = "product"
) -> str:
    return _sha256({
        "product_ids": sorted(set(product_ids)),
        "content_angle": content_angle.strip().lower(),
        "keyword_cluster": (keyword_cluster or "").strip().lower(),
        "board_id": board_id or "",
        "destination_type": destination_type.strip().lower(),
    })


def text_fingerprint(*, title: str, description: str, alt_text: str) -> str:
    normalize = lambda s: " ".join(s.lower().split())
    return _sha256({
        "title": normalize(title),
        "description": normalize(description),
        "alt_text": normalize(alt_text),
    })


def creative_fingerprint(
    *, source_image_sha256: str, template_key: str, template_version: int,
    text_hash: str, layout_parameters: dict | None = None
) -> str:
    return _sha256({
        "source_image_sha256": source_image_sha256,
        "template": template_key,
        "template_version": template_version,
        "text_hash": text_hash,
        "layout": layout_parameters or {},
    })


def publication_fingerprint(*, concept_id: str, creative_id: str, board_id: str, canonical_destination: str) -> str:
    return _sha256({
        "concept_id": concept_id,
        "creative_id": creative_id,
        "board_id": board_id,
        "destination": canonical_destination.rstrip("/"),
    })
