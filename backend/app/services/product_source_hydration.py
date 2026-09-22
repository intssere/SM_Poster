from __future__ import annotations

from typing import Callable
from urllib.parse import urlparse

from sqlalchemy import update

from app.core.config import Settings, get_settings
from app.models.domain import ProductImage
from app.services.creative_rendering import CreativeRenderError, SecureImageDownloader
from app.services.media_storage import StorageCorrupt, StorageMissing, StorageUnavailable
from app.services.product_source_cache import (
    ProductSourceStorage,
    verify_product_source,
)


class ProductSourceHydrationError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _validate_identity(image: ProductImage) -> list[str]:
    blockers: list[str] = []
    parsed = urlparse(image.source_url or "")
    if not image.editorial_eligible:
        blockers.append("PRODUCT_IMAGE_NOT_EDITORIAL_ELIGIBLE")
    if not image.shopify_media_id:
        blockers.append("SHOPIFY_MEDIA_ID_REQUIRED")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.lower() != "cdn.shopify.com"
    ):
        blockers.append("SHOPIFY_CDN_SOURCE_REQUIRED")
    return blockers


def product_source_readiness(
    db,
    *,
    product_image_id: str,
    settings: Settings | None = None,
    storage: ProductSourceStorage | None = None,
) -> dict:
    settings = settings or get_settings()
    image = db.get(ProductImage, product_image_id)
    if image is None:
        raise ProductSourceHydrationError("PRODUCT_IMAGE_NOT_FOUND")

    blockers = _validate_identity(image)
    cache_status = "ABSENT"
    cache_verified = False
    if image.source_sha256:
        source_storage = storage or ProductSourceStorage(settings=settings)
        try:
            source_storage.read_verified(image.id, image.source_sha256)
        except StorageMissing:
            blockers.append("PRODUCT_SOURCE_CACHE_MISSING")
            cache_status = "MISSING"
        except StorageCorrupt:
            blockers.append("PRODUCT_SOURCE_CACHE_CORRUPT")
            cache_status = "CORRUPT"
        except StorageUnavailable:
            blockers.append("PRODUCT_SOURCE_CACHE_UNAVAILABLE")
            cache_status = "UNAVAILABLE"
        except ValueError:
            blockers.append("PRODUCT_SOURCE_DIGEST_INVALID")
            cache_status = "INVALID"
        else:
            cache_status = "VERIFIED"
            cache_verified = True

    return {
        "product_image_id": image.id,
        "product_id": image.product_id,
        "shopify_media_id": image.shopify_media_id,
        "source_url": image.source_url,
        "source_sha256": image.source_sha256,
        "width": image.width,
        "height": image.height,
        "editorial_eligible": bool(image.editorial_eligible),
        "cache_status": cache_status,
        "cache_verified": cache_verified,
        "ready_to_hydrate": not blockers and image.source_sha256 is None,
        "already_hydrated": not blockers and bool(image.source_sha256) and cache_verified,
        "blockers": blockers,
        "state_mutated": False,
        "provider_called": False,
        "ai_called": False,
    }


def _require_exact(expected, actual, code: str) -> None:
    if expected != actual:
        raise ProductSourceHydrationError(code)


def hydrate_product_source(
    db,
    *,
    product_image_id: str,
    expected_product_id: str,
    expected_source_url: str,
    expected_shopify_media_id: str,
    expected_current_source_sha256: str | None,
    settings: Settings | None = None,
    storage: ProductSourceStorage | None = None,
    downloader: Callable[[str], bytes] | None = None,
) -> dict:
    settings = settings or get_settings()
    readiness = product_source_readiness(
        db,
        product_image_id=product_image_id,
        settings=settings,
        storage=storage,
    )

    _require_exact(
        expected_product_id,
        readiness["product_id"],
        "PRODUCT_ID_MISMATCH",
    )
    _require_exact(
        expected_source_url,
        readiness["source_url"],
        "SOURCE_URL_MISMATCH",
    )
    _require_exact(
        expected_shopify_media_id,
        readiness["shopify_media_id"],
        "SHOPIFY_MEDIA_ID_MISMATCH",
    )

    if readiness["blockers"]:
        raise ProductSourceHydrationError(readiness["blockers"][0])

    current_digest = readiness["source_sha256"]
    if current_digest is not None:
        if expected_current_source_sha256 not in (None, current_digest):
            raise ProductSourceHydrationError("SOURCE_SHA256_MISMATCH")
        if readiness["already_hydrated"] is not True:
            raise ProductSourceHydrationError("PRODUCT_SOURCE_CACHE_NOT_VERIFIED")
        return {
            "status": "SUCCEEDED",
            "idempotent": True,
            "product_image_id": product_image_id,
            "product_id": readiness["product_id"],
            "shopify_media_id": readiness["shopify_media_id"],
            "source_url": readiness["source_url"],
            "source_sha256": current_digest,
            "cache_status": "VERIFIED",
            "network_get_count": 0,
            "state_mutated": False,
            "provider_called": False,
            "ai_called": False,
        }

    if expected_current_source_sha256 is not None:
        raise ProductSourceHydrationError("SOURCE_SHA256_MISMATCH")
    if readiness["ready_to_hydrate"] is not True:
        raise ProductSourceHydrationError(
            (readiness["blockers"] or ["PRODUCT_SOURCE_NOT_READY"])[0]
        )

    image = db.get(ProductImage, product_image_id)
    if image is None:
        raise ProductSourceHydrationError("PRODUCT_IMAGE_NOT_FOUND")
    _require_exact(expected_product_id, image.product_id, "PRODUCT_ID_DRIFT")
    _require_exact(expected_source_url, image.source_url, "SOURCE_URL_DRIFT")
    _require_exact(
        expected_shopify_media_id,
        image.shopify_media_id,
        "SHOPIFY_MEDIA_ID_DRIFT",
    )
    if image.source_sha256 is not None:
        raise ProductSourceHydrationError("SOURCE_SHA256_CONCURRENT_UPDATE")

    source_storage = storage or ProductSourceStorage(settings=settings)
    fetch = downloader or SecureImageDownloader()

    try:
        raw = fetch(image.source_url)
    except CreativeRenderError as exc:
        raise ProductSourceHydrationError("PRODUCT_SOURCE_DOWNLOAD_FAILED") from exc
    except Exception as exc:
        raise ProductSourceHydrationError("PRODUCT_SOURCE_DOWNLOAD_FAILED") from exc

    try:
        digest = source_storage.write_verified(image.id, raw)
        verify_product_source(
            source_storage.read_verified(image.id, digest),
            digest,
        )
    except StorageMissing as exc:
        raise ProductSourceHydrationError("PRODUCT_SOURCE_CACHE_WRITE_VERIFY_FAILED") from exc
    except StorageCorrupt as exc:
        raise ProductSourceHydrationError("PRODUCT_SOURCE_CACHE_CORRUPT") from exc
    except StorageUnavailable as exc:
        raise ProductSourceHydrationError("PRODUCT_SOURCE_CACHE_UNAVAILABLE") from exc
    except ValueError as exc:
        raise ProductSourceHydrationError("PRODUCT_SOURCE_CACHE_IDENTITY_INVALID") from exc

    result = db.execute(
        update(ProductImage)
        .where(
            ProductImage.id == product_image_id,
            ProductImage.product_id == expected_product_id,
            ProductImage.source_url == expected_source_url,
            ProductImage.shopify_media_id == expected_shopify_media_id,
            ProductImage.source_sha256.is_(None),
        )
        .values(source_sha256=digest)
    )
    if int(result.rowcount or 0) != 1:
        db.rollback()
        raise ProductSourceHydrationError("SOURCE_SHA256_CONCURRENT_UPDATE")

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise ProductSourceHydrationError("PRODUCT_SOURCE_DB_COMMIT_FAILED") from exc

    image = db.get(ProductImage, product_image_id)
    if image is None or image.source_sha256 != digest:
        raise ProductSourceHydrationError("PRODUCT_SOURCE_POSTCONDITION_FAILED")
    try:
        source_storage.read_verified(image.id, digest)
    except (StorageMissing, StorageCorrupt, StorageUnavailable, ValueError) as exc:
        raise ProductSourceHydrationError("PRODUCT_SOURCE_POSTCONDITION_FAILED") from exc

    return {
        "status": "SUCCEEDED",
        "idempotent": False,
        "product_image_id": image.id,
        "product_id": image.product_id,
        "shopify_media_id": image.shopify_media_id,
        "source_url": image.source_url,
        "source_sha256": digest,
        "cache_status": "VERIFIED",
        "network_get_count": 1,
        "state_mutated": True,
        "provider_called": True,
        "provider_operation": "SHOPIFY_CDN_IMAGE_GET",
        "ai_called": False,
    }
