"""Bounded exact-key adapters used only by the manual migration command."""
from __future__ import annotations

import io
from urllib.parse import unquote, urlsplit

from app.services.media_storage import StorageMissing, StorageUnavailable
from app.services.s3_media_storage import _boto_client, _missing
from .one_shot_migration import _require


class S3ExactTarget:
    """One send per get/conditional-put, even if SDK redirect hooks request retry."""
    def __init__(self, config, bindings, *, client_factory=None):
        self.config = config
        self.keys = {b["key"] for b in bindings}
        self.client = (client_factory or _boto_client)(config)
        self._sent = 0
        self._key = self._method = None
        self.client.meta.events.register("before-send.s3", self._before_send)

    def _before_send(self, request, **kwargs):
        _require(self._sent == 0)
        parts, endpoint = urlsplit(request.url), urlsplit(self.config.endpoint)
        hosts = {endpoint.hostname}
        if not self.config.path_style:
            hosts.add(self.config.bucket + "." + endpoint.hostname)
        _require(parts.scheme == endpoint.scheme and parts.hostname in hosts
                 and parts.port == endpoint.port and request.method == self._method)
        path = unquote(parts.path)
        base = endpoint.path.rstrip("/")
        prefix = base + ("/" + self.config.bucket if parts.hostname == endpoint.hostname else "")
        _require(self._key in self.keys and path == prefix + "/" + self._key)
        self._sent += 1

    def get(self, key, size):
        _require(key in self.keys)
        self._sent = 0
        self._key, self._method = key, "GET"
        try:
            response = self.client.get_object(Bucket=self.config.bucket, Key=key,
                                              Range=f"bytes=0-{size}")
            body = response["Body"]
            try:
                if "ContentRange" in response:
                    _require(response["ContentRange"].rsplit("/", 1)[-1] == str(size))
                return body.read(size + 1)
            finally:
                body.close()
        except Exception as exc:
            if _missing(exc):
                raise StorageMissing("Exact object absent.") from None
            raise StorageUnavailable("Exact target read refused.") from None

    def put_missing(self, key, data):
        _require(key in self.keys)
        self._sent = 0
        self._key, self._method = key, "PUT"
        try:
            self.client.put_object(Bucket=self.config.bucket, Key=key, Body=data,
                                   ContentType="image/png", IfNoneMatch="*")
        except Exception:
            raise StorageUnavailable("Conditional target create refused.") from None

    def close(self):
        self.client.close()


class ReplitExactReader:
    """Default attached Replit bucket only. No listings, HTTPS fallback or retries.

    SDK transport internals are checked explicitly, never substituted with app
    settings or a different provider when SDK compatibility is unavailable.
    """
    def __init__(self, bindings):
        self.keys = {b["key"] for b in bindings}
        self._sent = set()
        self._auth_sent = set()
        self.sidecar = self.auth = self.client = None
        try:
            import requests
            from requests.adapters import HTTPAdapter
            from google.auth.transport.requests import AuthorizedSession, Request
            from replit.object_storage import Client
            from replit.object_storage._config import REPLIT_DEFAULT_BUCKET_URL

            owner = self

            class Sidecar(requests.Session):
                def send(self, request, **kwargs):
                    p = urlsplit(request.url)
                    _require(p.scheme == "http" and p.hostname == "127.0.0.1"
                             and p.port == 1106 and not p.query
                             and (request.method, p.path) in {
                                 ("GET", "/credential"), ("POST", "/token"),
                                 ("GET", "/object-storage/default-bucket")}
                             and p.path not in owner._auth_sent)
                    owner._auth_sent.add(p.path)
                    kwargs["allow_redirects"] = False
                    return super().send(request, **kwargs)

            class ExactSession(AuthorizedSession):
                def send(self, request, **kwargs):
                    p = urlsplit(request.url)
                    _require(p.scheme == "https" and p.hostname == "storage.googleapis.com"
                             and request.method == "GET" and "/o/" in p.path)
                    key = unquote(p.path.split("/o/", 1)[1])
                    _require(key in owner.keys and key not in owner._sent)
                    owner._sent.add(key)
                    kwargs["allow_redirects"] = False
                    return super().send(request, **kwargs)

            self.sidecar = Sidecar()
            self.sidecar.trust_env = False
            for scheme in ("http://", "https://"):
                self.sidecar.mount(scheme, HTTPAdapter(max_retries=0))
            with self.sidecar.get(REPLIT_DEFAULT_BUCKET_URL, timeout=10,
                                  allow_redirects=False) as response:
                _require(response.status_code == 200)
                bucket = response.json().get("bucketId")
                _require(type(bucket) is str and bool(bucket))
            sdk = Client(bucket_id=bucket)
            self.client = sdk._Client__gcs_client
            self.auth = ExactSession(self.client._credentials, refresh_status_codes=(),
                                     max_refresh_attempts=0, refresh_timeout=15,
                                     auth_request=Request(self.sidecar))
            self.auth.trust_env = False
            for scheme in ("http://", "https://"):
                self.auth.mount(scheme, HTTPAdapter(max_retries=0))
            self.client._http_internal = self.auth
            self.bucket = bucket
        except Exception:
            self.close()
            raise StorageUnavailable("Exact Replit source unavailable.") from None

    def get(self, key, size):
        _require(key in self.keys and key not in self._sent)
        try:
            # Raw single bounded GET: no disk file, checksum retry or chunks.
            blob = self.client.bucket(self.bucket).blob(key)
            class BoundedMemory(io.BytesIO):
                def write(self, block):
                    _require(self.tell() + len(block) <= size + 1)
                    return super().write(block)
            with BoundedMemory() as output:
                blob.download_to_file(output, raw_download=True, end=size, checksum=None,
                                      retry=None, timeout=20)
                return output.getvalue()
        except Exception:
            raise StorageUnavailable("Exact Replit source read refused.") from None

    def close(self):
        for resource in (self.auth, self.sidecar, self.client):
            if resource is not None:
                resource.close()
