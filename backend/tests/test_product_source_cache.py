import hashlib
import io

import pytest
from PIL import Image

from app.services.media_storage import StorageCorrupt, StorageMissing
from app.services.product_source_cache import (
    CachedProductSourceDownloader,
    ProductSourceCacheError,
    ProductSourceStorage,
    product_source_key,
    verify_product_source,
)


class MemoryBackend:
    def __init__(self):
        self.rows = {}

    def put(self, key, data):
        self.rows[key] = bytes(data)

    def get(self, key):
        if key not in self.rows:
            raise StorageMissing("missing")
        return self.rows[key]

    def exists(self, key):
        return key in self.rows


def _png_bytes():
    image = Image.new("RGB", (8, 8), "white")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_product_source_storage_round_trip_is_digest_addressed():
    backend = MemoryBackend()
    storage = ProductSourceStorage(backend=backend)
    data = _png_bytes()

    digest = storage.write_verified("image-1", data)

    assert digest == hashlib.sha256(data).hexdigest()
    assert storage.read_verified("image-1", digest) == data
    assert product_source_key("image-1", digest) in backend.rows


def test_product_source_storage_rejects_digest_tampering():
    backend = MemoryBackend()
    storage = ProductSourceStorage(backend=backend)
    data = _png_bytes()
    digest = hashlib.sha256(data).hexdigest()
    key = product_source_key("image-1", digest)
    backend.rows[key] = data + b"tamper"

    with pytest.raises(StorageCorrupt):
        storage.read_verified("image-1", digest)


def test_verify_product_source_rejects_non_image_bytes():
    data = b"not-an-image"
    digest = hashlib.sha256(data).hexdigest()
    with pytest.raises(StorageCorrupt):
        verify_product_source(data, digest)


def test_cached_downloader_is_exact_url_and_has_no_network_fallback():
    data = _png_bytes()
    downloader = CachedProductSourceDownloader(
        expected_url="https://cdn.shopify.com/example.png",
        source_bytes=data,
    )
    assert downloader("https://cdn.shopify.com/example.png") == data
    with pytest.raises(ProductSourceCacheError, match="PRODUCT_SOURCE_URL_DRIFT"):
        downloader("https://cdn.shopify.com/other.png")
