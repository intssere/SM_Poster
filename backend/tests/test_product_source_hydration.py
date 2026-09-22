import hashlib
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from app.core.config import Settings
from app.services import product_source_hydration as svc
from app.services.media_storage import StorageCorrupt, StorageMissing, StorageUnavailable


IMAGE_ID = "5536ed41-22f8-441c-9efd-a2e73972649e"
PRODUCT_ID = "01abbd7d-fd2e-4428-99ad-608a76cb7098"
MEDIA_ID = "gid-image-1"
SOURCE_URL = "https://cdn.shopify.com/s/files/example.png"


def _png_bytes():
    image = Image.new("RGB", (10, 10), "white")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class MemoryStorage:
    def __init__(self):
        self.rows = {}
        self.fail_write = None

    def write_verified(self, image_id, data):
        if self.fail_write:
            raise self.fail_write
        digest = hashlib.sha256(data).hexdigest()
        self.rows[(image_id, digest)] = bytes(data)
        return digest

    def read_verified(self, image_id, digest):
        key = (image_id, digest)
        if key not in self.rows:
            raise StorageMissing("missing")
        data = self.rows[key]
        if hashlib.sha256(data).hexdigest() != digest:
            raise StorageCorrupt("corrupt")
        return data


class Result:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class FakeDB:
    def __init__(self, image, *, rowcount=1, commit_digest=None):
        self.image = image
        self.rowcount = rowcount
        self.commit_digest = commit_digest
        self.execute_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def get(self, model, row_id):
        return self.image if row_id == self.image.id else None

    def execute(self, statement):
        self.execute_count += 1
        return Result(self.rowcount)

    def commit(self):
        self.commit_count += 1
        if self.commit_digest is not None:
            self.image.source_sha256 = self.commit_digest

    def rollback(self):
        self.rollback_count += 1


def _settings():
    return Settings(database_url="sqlite://")


def _image(**updates):
    data = {
        "id": IMAGE_ID,
        "product_id": PRODUCT_ID,
        "shopify_media_id": MEDIA_ID,
        "source_url": SOURCE_URL,
        "source_sha256": None,
        "width": 1000,
        "height": 1000,
        "editorial_eligible": True,
    }
    data.update(updates)
    return SimpleNamespace(**data)


def test_readiness_allows_exact_null_digest_hydration():
    image = _image()
    result = svc.product_source_readiness(
        FakeDB(image),
        product_image_id=IMAGE_ID,
        settings=_settings(),
        storage=MemoryStorage(),
    )
    assert result["ready_to_hydrate"] is True
    assert result["already_hydrated"] is False
    assert result["blockers"] == []
    assert result["state_mutated"] is False
    assert result["provider_called"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.shopify.com/image.png",
        "https://example.com/image.png",
        "https://localhost/image.png",
    ],
)
def test_readiness_rejects_non_exact_shopify_cdn_source(url):
    result = svc.product_source_readiness(
        FakeDB(_image(source_url=url)),
        product_image_id=IMAGE_ID,
        settings=_settings(),
        storage=MemoryStorage(),
    )
    assert result["ready_to_hydrate"] is False
    assert "SHOPIFY_CDN_SOURCE_REQUIRED" in result["blockers"]


def test_readiness_verifies_existing_digest_cache():
    data = _png_bytes()
    digest = hashlib.sha256(data).hexdigest()
    storage = MemoryStorage()
    storage.rows[(IMAGE_ID, digest)] = data
    result = svc.product_source_readiness(
        FakeDB(_image(source_sha256=digest)),
        product_image_id=IMAGE_ID,
        settings=_settings(),
        storage=storage,
    )
    assert result["already_hydrated"] is True
    assert result["cache_status"] == "VERIFIED"
    assert result["cache_verified"] is True


def test_exact_identity_drift_blocks_before_network():
    image = _image()
    db = FakeDB(image)
    calls = []

    def downloader(url):
        calls.append(url)
        return _png_bytes()

    with pytest.raises(svc.ProductSourceHydrationError, match="PRODUCT_ID_MISMATCH"):
        svc.hydrate_product_source(
            db,
            product_image_id=IMAGE_ID,
            expected_product_id="wrong-product",
            expected_source_url=SOURCE_URL,
            expected_shopify_media_id=MEDIA_ID,
            expected_current_source_sha256=None,
            settings=_settings(),
            storage=MemoryStorage(),
            downloader=downloader,
        )
    assert calls == []
    assert db.execute_count == 0


def test_hydration_downloads_once_writes_cache_and_compare_sets_digest():
    data = _png_bytes()
    digest = hashlib.sha256(data).hexdigest()
    image = _image()
    db = FakeDB(image, rowcount=1, commit_digest=digest)
    storage = MemoryStorage()
    calls = []

    def downloader(url):
        calls.append(url)
        return data

    result = svc.hydrate_product_source(
        db,
        product_image_id=IMAGE_ID,
        expected_product_id=PRODUCT_ID,
        expected_source_url=SOURCE_URL,
        expected_shopify_media_id=MEDIA_ID,
        expected_current_source_sha256=None,
        settings=_settings(),
        storage=storage,
        downloader=downloader,
    )

    assert calls == [SOURCE_URL]
    assert db.execute_count == 1
    assert db.commit_count == 1
    assert db.rollback_count == 0
    assert image.source_sha256 == digest
    assert storage.read_verified(IMAGE_ID, digest) == data
    assert result["network_get_count"] == 1
    assert result["provider_called"] is True
    assert result["provider_operation"] == "SHOPIFY_CDN_IMAGE_GET"


def test_concurrent_compare_and_set_failure_fails_closed():
    data = _png_bytes()
    digest = hashlib.sha256(data).hexdigest()
    image = _image()
    db = FakeDB(image, rowcount=0, commit_digest=digest)
    storage = MemoryStorage()

    with pytest.raises(
        svc.ProductSourceHydrationError,
        match="SOURCE_SHA256_CONCURRENT_UPDATE",
    ):
        svc.hydrate_product_source(
            db,
            product_image_id=IMAGE_ID,
            expected_product_id=PRODUCT_ID,
            expected_source_url=SOURCE_URL,
            expected_shopify_media_id=MEDIA_ID,
            expected_current_source_sha256=None,
            settings=_settings(),
            storage=storage,
            downloader=lambda url: data,
        )
    assert db.commit_count == 0
    assert db.rollback_count == 1
    assert image.source_sha256 is None


def test_network_failure_leaves_database_unchanged():
    image = _image()
    db = FakeDB(image)
    with pytest.raises(
        svc.ProductSourceHydrationError,
        match="PRODUCT_SOURCE_DOWNLOAD_FAILED",
    ):
        svc.hydrate_product_source(
            db,
            product_image_id=IMAGE_ID,
            expected_product_id=PRODUCT_ID,
            expected_source_url=SOURCE_URL,
            expected_shopify_media_id=MEDIA_ID,
            expected_current_source_sha256=None,
            settings=_settings(),
            storage=MemoryStorage(),
            downloader=lambda url: (_ for _ in ()).throw(RuntimeError("network")),
        )
    assert db.execute_count == 0
    assert db.commit_count == 0
    assert image.source_sha256 is None


def test_cache_write_failure_leaves_database_unchanged():
    image = _image()
    db = FakeDB(image)
    storage = MemoryStorage()
    storage.fail_write = StorageUnavailable("unavailable")
    with pytest.raises(
        svc.ProductSourceHydrationError,
        match="PRODUCT_SOURCE_CACHE_UNAVAILABLE",
    ):
        svc.hydrate_product_source(
            db,
            product_image_id=IMAGE_ID,
            expected_product_id=PRODUCT_ID,
            expected_source_url=SOURCE_URL,
            expected_shopify_media_id=MEDIA_ID,
            expected_current_source_sha256=None,
            settings=_settings(),
            storage=storage,
            downloader=lambda url: _png_bytes(),
        )
    assert db.execute_count == 0
    assert db.commit_count == 0
    assert image.source_sha256 is None


def test_exact_retry_after_success_uses_zero_network_calls():
    data = _png_bytes()
    digest = hashlib.sha256(data).hexdigest()
    image = _image(source_sha256=digest)
    storage = MemoryStorage()
    storage.rows[(IMAGE_ID, digest)] = data
    db = FakeDB(image)
    calls = []

    result = svc.hydrate_product_source(
        db,
        product_image_id=IMAGE_ID,
        expected_product_id=PRODUCT_ID,
        expected_source_url=SOURCE_URL,
        expected_shopify_media_id=MEDIA_ID,
        expected_current_source_sha256=None,
        settings=_settings(),
        storage=storage,
        downloader=lambda url: calls.append(url),
    )

    assert calls == []
    assert db.execute_count == 0
    assert db.commit_count == 0
    assert result["idempotent"] is True
    assert result["network_get_count"] == 0
    assert result["state_mutated"] is False
    assert result["provider_called"] is False


def test_existing_digest_with_missing_cache_fails_closed_without_network():
    data = _png_bytes()
    digest = hashlib.sha256(data).hexdigest()
    image = _image(source_sha256=digest)
    calls = []
    with pytest.raises(
        svc.ProductSourceHydrationError,
        match="PRODUCT_SOURCE_CACHE_MISSING",
    ):
        svc.hydrate_product_source(
            FakeDB(image),
            product_image_id=IMAGE_ID,
            expected_product_id=PRODUCT_ID,
            expected_source_url=SOURCE_URL,
            expected_shopify_media_id=MEDIA_ID,
            expected_current_source_sha256=digest,
            settings=_settings(),
            storage=MemoryStorage(),
            downloader=lambda url: calls.append(url),
        )
    assert calls == []
