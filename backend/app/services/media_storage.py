"""Durable, digest-addressed PNG storage.

The provider is deliberately behind this small interface so tests can use an
in-memory backend and production cannot accidentally fall back to disk.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class StorageUnavailable(RuntimeError):
    """The configured storage provider cannot currently be used."""


class StorageCorrupt(RuntimeError):
    """An object failed its content or digest integrity check."""


class StorageMissing(RuntimeError):
    """An object does not exist."""


def _provider_missing(exc: Exception) -> bool:
    """Recognize SDK versions without coupling imports to one layout."""
    try:
        from replit.object_storage.errors import ObjectNotFoundError
        if isinstance(exc, ObjectNotFoundError):
            return True
    except (ImportError, AttributeError):
        pass
    try:
        from replit.object_storage.exceptions import ObjectNotFoundError
        if isinstance(exc, ObjectNotFoundError):
            return True
    except (ImportError, AttributeError):
        pass
    return exc.__class__.__name__ == "ObjectNotFoundError" or "could not be found" in str(exc).lower()


class StorageBackend(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


def media_key(kind: str, object_id: str, digest: str) -> str:
    if kind not in {"creative", "ai-asset"}:
        raise ValueError("Invalid media type.")
    if not object_id or "/" in object_id or "\\" in object_id:
        raise ValueError("Invalid media identifier.")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Invalid media digest.")
    return f"{kind}/{object_id}/{digest}.png"


def verify_png(data: bytes, digest: str) -> bytes:
    if not data.startswith(PNG_SIGNATURE) or hashlib.sha256(data).hexdigest() != digest:
        raise StorageCorrupt("Stored media failed integrity verification.")
    return data


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise StorageUnavailable("Invalid storage key.")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError as exc:
            raise StorageMissing("Media object was not found.") from exc
        except OSError as exc:
            raise StorageUnavailable("Media storage unavailable.") from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


class ReplitObjectStorage:
    """Adapter for the official replit-object-storage Client SDK."""
    def __init__(self, bucket: str | None = None):
        try:
            from replit.object_storage import Client
            # App Storage's attached default bucket is the production source
            # of truth. An explicit bucket is only useful for injection.
            self.client = Client(bucket_id=bucket) if bucket else Client()
        except Exception as exc:
            raise StorageUnavailable("Durable media storage unavailable.") from exc

    def put(self, key: str, data: bytes) -> None:
        try:
            if hasattr(self.client, "upload_from_bytes"):
                self.client.upload_from_bytes(key, data)
            elif hasattr(self.client, "upload"):
                self.client.upload(key, data)
            else:
                raise AttributeError("unsupported client")
        except Exception as exc:
            raise StorageUnavailable("Durable media storage unavailable.") from exc

    def get(self, key: str) -> bytes:
        try:
            if hasattr(self.client, "download_as_bytes"):
                return self.client.download_as_bytes(key)
            if hasattr(self.client, "download"):
                return self.client.download(key)
            raise AttributeError("unsupported client")
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404 or _provider_missing(exc):
                raise StorageMissing("Media object was not found.") from exc
            raise StorageUnavailable("Durable media storage unavailable.") from exc

    def exists(self, key: str) -> bool:
        try:
            if hasattr(self.client, "exists"):
                return bool(self.client.exists(key))
            self.get(key)
            return True
        except Exception as exc:
            if _provider_missing(exc):
                return False
            raise StorageUnavailable("Durable media storage unavailable.") from exc


def default_storage(*, kind: str, root: Path | None = None, settings=None) -> StorageBackend:
    """Choose App Storage in exposed runtimes; disk is development-only."""
    if settings is None:
        from app.core.config import get_settings
        settings = get_settings()
    if settings.is_exposed:
        return ReplitObjectStorage()
    return LocalStorage(root or Path(__file__).resolve().parents[2] / f"generated-{kind}s")


class PNGMediaStorage:
    def __init__(self, kind: str, root: Path | None = None, backend: StorageBackend | None = None, settings=None):
        self.kind = kind
        self.backend = backend or default_storage(kind=kind, root=root, settings=settings)
        self.root = getattr(self.backend, "root", None)

    def write(self, object_id: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        verify_png(data, digest)
        key = media_key(self.kind, object_id, digest)
        try:
            self.backend.put(key, data)
            verify_png(self.backend.get(key), digest)
        except (StorageCorrupt, StorageUnavailable):
            raise
        except Exception as exc:
            raise StorageUnavailable("Media storage unavailable.") from exc
        return key

    def read(self, object_id: str, digest: str) -> bytes:
        key = media_key(self.kind, object_id, digest)
        return verify_png(self.backend.get(key), digest)

    def key(self, object_id: str, digest: str) -> str:
        return media_key(self.kind, object_id, digest)