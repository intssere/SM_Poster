from __future__ import annotations

import hashlib
import io
from typing import Callable

from PIL import Image

from app.services.media_storage import (
    StorageBackend,
    StorageCorrupt,
    StorageMissing,
    StorageUnavailable,
    default_storage,
)


MAX_PRODUCT_SOURCE_BYTES = 8 * 1024 * 1024
_ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


class ProductSourceCacheError(RuntimeError):
    pass


def product_source_key(image_id: str, digest: str) -> str:
    if not image_id or "/" in image_id or "\\" in image_id:
        raise ValueError("Invalid product image identifier.")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Invalid product source digest.")
    return f"product-source/{image_id}/{digest}.source"


def verify_product_source(data: bytes, digest: str) -> bytes:
    if not data or len(data) > MAX_PRODUCT_SOURCE_BYTES:
        raise StorageCorrupt("Stored product source size is invalid.")
    if hashlib.sha256(data).hexdigest() != digest:
        raise StorageCorrupt("Stored product source failed digest verification.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in _ALLOWED_FORMATS:
                raise StorageCorrupt("Stored product source format is not allowed.")
            width, height = image.size
    except StorageCorrupt:
        raise
    except Exception as exc:
        raise StorageCorrupt("Stored product source is not a valid image.") from exc
    if width < 1 or height < 1 or width * height > 40_000_000:
        raise StorageCorrupt("Stored product source dimensions are invalid.")
    return data


class ProductSourceStorage:
    def __init__(
        self,
        *,
        backend: StorageBackend | None = None,
        settings=None,
    ):
        self.backend = backend or default_storage(
            kind="product-source",
            settings=settings,
        )

    def write_verified(self, image_id: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        verify_product_source(data, digest)
        key = product_source_key(image_id, digest)
        try:
            self.backend.put(key, data)
            verify_product_source(self.backend.get(key), digest)
        except (StorageCorrupt, StorageMissing, StorageUnavailable):
            raise
        except Exception as exc:
            raise StorageUnavailable("Product source storage unavailable.") from exc
        return digest

    def read_verified(self, image_id: str, digest: str) -> bytes:
        key = product_source_key(image_id, digest)
        try:
            data = self.backend.get(key)
            return verify_product_source(data, digest)
        except (StorageCorrupt, StorageMissing, StorageUnavailable):
            raise
        except Exception as exc:
            raise StorageUnavailable("Product source storage unavailable.") from exc

    def has_verified(self, image_id: str, digest: str) -> bool:
        try:
            self.read_verified(image_id, digest)
            return True
        except (StorageCorrupt, StorageMissing, StorageUnavailable, ValueError):
            return False


class CachedProductSourceDownloader:
    """Exact-URL downloader backed only by verified cached bytes.

    This object deliberately has no network client and performs no DNS lookup.
    """

    def __init__(self, *, expected_url: str, source_bytes: bytes):
        self.expected_url = expected_url
        self.source_bytes = bytes(source_bytes)

    def __call__(self, url: str) -> bytes:
        if url != self.expected_url:
            raise ProductSourceCacheError("PRODUCT_SOURCE_URL_DRIFT")
        return self.source_bytes


def cached_downloader(
    *,
    expected_url: str,
    source_bytes: bytes,
) -> Callable[[str], bytes]:
    return CachedProductSourceDownloader(
        expected_url=expected_url,
        source_bytes=source_bytes,
    )
