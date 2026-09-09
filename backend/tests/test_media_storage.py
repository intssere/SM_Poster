import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.media_storage import (
    LocalStorage,
    PNGMediaStorage,
    StorageCorrupt,
    StorageMissing,
    StorageUnavailable,
    default_storage,
    media_key,
)
from app.services.media_backfill import backfill
from app.api.routes import proposals as proposal_routes

PNG = b"\x89PNG\r\n\x1a\nnetwork-free-test"


class FakeStorage:
    def __init__(self):
        self.objects = {}
        self.unavailable = False

    def put(self, key, data):
        if self.unavailable:
            raise StorageUnavailable("unavailable")
        self.objects[key] = data

    def get(self, key):
        if self.unavailable:
            raise StorageUnavailable("unavailable")
        if key not in self.objects:
            raise StorageMissing("missing")
        return self.objects[key]

    def exists(self, key):
        if self.unavailable:
            raise StorageUnavailable("unavailable")
        return key in self.objects


def test_exact_keys_and_verified_upload_readback():
    digest = hashlib.sha256(PNG).hexdigest()
    assert media_key("creative", "c-1", digest) == f"creative/c-1/{digest}.png"
    assert media_key("ai-asset", "a-1", digest) == f"ai-asset/a-1/{digest}.png"
    backend = FakeStorage()
    storage = PNGMediaStorage("creative", backend=backend)
    assert storage.write("c-1", PNG) == f"creative/c-1/{digest}.png"
    assert storage.read("c-1", digest) == PNG


def test_corrupt_upload_and_read_are_rejected():
    backend = FakeStorage()
    storage = PNGMediaStorage("ai-asset", backend=backend)
    with pytest.raises(StorageCorrupt):
        storage.write("a-1", b"not-png")
    digest = hashlib.sha256(PNG).hexdigest()
    backend.objects[storage.key("a-1", digest)] = PNG + b"tamper"
    with pytest.raises(StorageCorrupt):
        storage.read("a-1", digest)


def test_missing_and_unavailable_are_not_silently_local_or_false():
    backend = FakeStorage()
    storage = PNGMediaStorage("creative", backend=backend)
    digest = hashlib.sha256(PNG).hexdigest()
    with pytest.raises(StorageMissing):
        storage.read("missing", digest)
    backend.unavailable = True
    with pytest.raises(StorageUnavailable):
        backend.exists(storage.key("c", digest))


def test_development_uses_safe_local_fallback(tmp_path):
    settings = SimpleNamespace(is_exposed=False, object_storage_bucket="ignored")
    storage = default_storage(kind="creative", root=tmp_path, settings=settings)
    assert isinstance(storage, LocalStorage)


def test_exposed_runtime_uses_attached_default_client(monkeypatch):
    import replit.object_storage as sdk
    made = []

    class Client:
        def __init__(self, *args, **kwargs):
            made.append((args, kwargs))

    monkeypatch.setattr(sdk, "Client", Client)
    settings = SimpleNamespace(is_exposed=True, object_storage_bucket="")
    default_storage(kind="creative", settings=settings)
    assert made == [((), {})]


def test_replit_object_not_found_exception_maps_to_missing(monkeypatch):
    from app.services.media_storage import ReplitObjectStorage
    class ObjectNotFoundError(Exception):
        pass
    class Client:
        def download_as_bytes(self, key):
            raise ObjectNotFoundError("object could not be found")
    import replit.object_storage as sdk
    monkeypatch.setattr(sdk, "Client", Client)
    with pytest.raises(StorageMissing):
        ReplitObjectStorage().get("creative/missing/x.png")


def test_backfill_is_idempotent_and_does_not_delete_originals(tmp_path):
    root = tmp_path
    (root / "generated-creatives").mkdir()
    (root / "generated-ai-assets").mkdir()
    digest = hashlib.sha256(PNG).hexdigest()
    (root / "generated-creatives/c-1.png").write_bytes(PNG)
    (root / "generated-ai-assets/a-1.png").write_bytes(PNG)
    rows = [
        SimpleNamespace(id="c-1", sha256=digest, render_status="RENDERED"),
    ]
    assets = [SimpleNamespace(id="a-1", sha256=digest, storage_path=None)]

    class Session:
        def __init__(self):
            self.calls = 0
        def scalars(self, _):
            self.calls += 1
            return SimpleNamespace(all=lambda: rows if self.calls == 1 else assets)
        def commit(self):
            pass

    cs, aas = PNGMediaStorage("creative", backend=FakeStorage()), PNGMediaStorage("ai-asset", backend=FakeStorage())
    session = Session()
    first = backfill(session, creative_storage=cs, asset_storage=aas, root=root)
    original_count = (root / "generated-creatives/c-1.png").stat().st_size
    session.calls = 0
    second = backfill(session, creative_storage=cs, asset_storage=aas, root=root)
    assert first["creative_uploaded"] == first["asset_uploaded"] == 1
    assert second["skipped"] == 2
    assert assets[0].storage_path == f"ai-asset/a-1/{digest}.png"
    assert (root / "generated-creatives/c-1.png").stat().st_size == original_count


def test_public_route_reads_fake_durable_storage_and_safe_failures(monkeypatch):
    from contextlib import nullcontext
    from fastapi import HTTPException
    from starlette.requests import Request

    digest = hashlib.sha256(PNG).hexdigest()
    backend = FakeStorage()
    storage = PNGMediaStorage("creative", backend=backend)
    storage.write("c-1", PNG)
    creative = SimpleNamespace(id="c-1", sha256=digest, render_status="RENDERED")
    db = SimpleNamespace(no_autoflush=nullcontext(), get=lambda *_: creative)
    monkeypatch.setattr(proposal_routes, "CreativeStorage", lambda: SimpleNamespace(media=storage))
    request = Request({"type": "http", "method": "GET", "path": "/"})
    response = proposal_routes.public_creative_image("c-1", digest, request, db)
    assert response.status_code == 200 and response.body == PNG
    head = Request({"type": "http", "method": "HEAD", "path": "/"})
    assert proposal_routes.public_creative_image("c-1", digest, head, db).body == b""
    del backend.objects[storage.key("c-1", digest)]
    with pytest.raises(HTTPException) as missing:
        proposal_routes.public_creative_image("c-1", digest, request, db)
    assert missing.value.status_code == 404
    backend.objects[storage.key("c-1", digest)] = PNG + b"corrupt"
    with pytest.raises(HTTPException) as corrupt:
        proposal_routes.public_creative_image("c-1", digest, request, db)
    assert corrupt.value.status_code == 404
    backend.unavailable = True
    with pytest.raises(HTTPException) as unavailable:
        proposal_routes.public_creative_image("c-1", digest, request, db)
    assert unavailable.value.status_code == 503


def test_authenticated_media_routes_use_durable_readback_and_direct_fallback(monkeypatch):
    digest = hashlib.sha256(PNG).hexdigest()
    creative_backend, asset_backend = FakeStorage(), FakeStorage()
    creative_storage = PNGMediaStorage("creative", backend=creative_backend)
    asset_storage = PNGMediaStorage("ai-asset", backend=asset_backend)
    creative_storage.write("c-2", PNG)
    asset_storage.write("a-2", PNG)
    creative = SimpleNamespace(id="c-2", sha256=digest)
    asset = SimpleNamespace(id="a-2", sha256=digest)
    db = SimpleNamespace(get=lambda model, ident: creative if ident == "c-2" else asset)
    monkeypatch.setattr(proposal_routes, "CreativeStorage", lambda: SimpleNamespace(
        read_png=creative_storage.read, path_for=lambda _: Path("/missing")))
    monkeypatch.setattr(proposal_routes, "AIGeneratedAssetStorage", lambda: SimpleNamespace(
        read_png=asset_storage.read, path_for=lambda _: Path("/missing")))
    assert proposal_routes.creative_image("c-2", db).body == PNG
    assert proposal_routes.generated_asset_image("a-2", db).body == PNG
    # Direct calls have no injected DB; legacy local path behavior remains.
    local = LocalStorage(Path("/tmp/media-route-compat"))
    local.put("c-legacy.png", PNG)
    monkeypatch.setattr(proposal_routes, "CreativeStorage", lambda: SimpleNamespace(
        path_for=lambda _: Path("/tmp/media-route-compat/c-legacy.png")))
    assert proposal_routes.creative_image("c-legacy").path == Path("/tmp/media-route-compat/c-legacy.png")


def test_route_constructor_storage_failures_are_safe_503(monkeypatch):
    from contextlib import nullcontext
    from fastapi import HTTPException
    from starlette.requests import Request

    def unavailable():
        raise StorageUnavailable("provider detail must not escape")

    monkeypatch.setattr(proposal_routes, "CreativeStorage", unavailable)
    with pytest.raises(HTTPException) as creative:
        proposal_routes.creative_image("c-1", SimpleNamespace(get=lambda *_: None))
    assert creative.value.status_code == 503 and creative.value.detail == "Media storage unavailable."
    request = Request({"type": "http", "method": "GET", "path": "/"})
    db = SimpleNamespace(no_autoflush=nullcontext(), get=lambda *_: None)
    with pytest.raises(HTTPException) as public:
        proposal_routes.public_creative_image("c-1", "a" * 64, request, db)
    assert public.value.status_code == 503 and public.value.detail == "Media storage unavailable."

    monkeypatch.setattr(proposal_routes, "AIGeneratedAssetStorage", unavailable)
    with pytest.raises(HTTPException) as asset:
        proposal_routes.generated_asset_image("a-1", SimpleNamespace(get=lambda *_: None))
    assert asset.value.status_code == 503 and asset.value.detail == "Media storage unavailable."