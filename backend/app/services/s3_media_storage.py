"""Explicit S3-compatible configuration; no client or network at construction."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from urllib.parse import urlsplit

from .media_storage import StorageMissing, StorageUnavailable


REQUIRED = ("object_storage_endpoint", "object_storage_bucket",
            "object_storage_access_key", "object_storage_secret_key")
OPTIONAL = ("object_storage_region", "object_storage_path_style")


def explicitly_configured(settings):
    # The legacy bucket default alone is NOT S3 configuration. Pydantic records
    # explicitly supplied fields, including empty/partial environment values.
    supplied = getattr(settings, "model_fields_set", set())
    return bool(set(REQUIRED + OPTIONAL) & supplied) or any(
        getattr(settings, name, None) is not None
        for name in (REQUIRED[0], *REQUIRED[2:], *OPTIONAL)
    )


@dataclass(frozen=True)
class S3Config:
    endpoint: str = field(repr=False)
    bucket: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    region: str = "us-east-1"
    path_style: bool = True

    def __post_init__(self):
        try:
            parts = urlsplit(self.endpoint)
            valid = (
                parts.scheme in {"https", "http"} and parts.hostname
                and not parts.username and not parts.password
                and not parts.query and not parts.fragment
                and all(type(v) is str and v.strip() == v and v
                        for v in (self.endpoint, self.bucket, self.access_key,
                                  self.secret_key, self.region))
                and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket)
                and ".." not in self.bucket
                and re.fullmatch(r"[A-Za-z0-9-]{1,64}", self.region)
                and type(self.path_style) is bool
            )
        except Exception:
            valid = False
        if not valid:
            raise StorageUnavailable("Explicit S3 media configuration is invalid.") from None

    @classmethod
    def from_settings(cls, settings):
        supplied = getattr(settings, "model_fields_set", None)
        if supplied is not None and "object_storage_bucket" not in supplied:
            raise StorageUnavailable("Explicit S3 media configuration is incomplete.")
        return cls(
            endpoint=getattr(settings, REQUIRED[0], None),
            bucket=getattr(settings, REQUIRED[1], None),
            access_key=getattr(settings, REQUIRED[2], None),
            secret_key=getattr(settings, REQUIRED[3], None),
            region="us-east-1" if getattr(settings, OPTIONAL[0], None) is None
                   else getattr(settings, OPTIONAL[0]),
            path_style=True if getattr(settings, OPTIONAL[1], None) is None
                       else getattr(settings, OPTIONAL[1]),
        )


def _boto_client(config):
    # Explicit credentials prevent the ambient AWS/IMDS credential chain.
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3", endpoint_url=config.endpoint, region_name=config.region,
        aws_access_key_id=config.access_key, aws_secret_access_key=config.secret_key,
        config=Config(signature_version="s3v4",
                      s3={"addressing_style": "path" if config.path_style else "virtual"},
                      retries={"total_max_attempts": 1, "mode": "standard"},
                      request_checksum_calculation="when_required",
                      response_checksum_validation="when_required",
                      connect_timeout=10, read_timeout=30),
    )


def _missing(exc):
    response = getattr(exc, "response", None)
    return (isinstance(response, dict)
            and isinstance(response.get("Error"), dict)
            and response["Error"].get("Code") in {"NoSuchKey", "NotFound", "404"})


class S3CompatibleStorage:
    def __init__(self, config: S3Config, *, client_factory=None):
        self.config = config
        self._factory = client_factory or _boto_client
        self._client_instance = None

    def _client(self):
        if self._client_instance is None:
            try:
                self._client_instance = self._factory(self.config)
            except Exception:
                raise StorageUnavailable("Durable media storage unavailable.") from None
        return self._client_instance

    def put(self, key: str, data: bytes) -> None:
        try:
            self._client().put_object(Bucket=self.config.bucket, Key=key,
                                      Body=data, ContentType="image/png")
        except Exception:
            raise StorageUnavailable("Durable media storage unavailable.") from None

    def get(self, key: str) -> bytes:
        try:
            body = self._client().get_object(Bucket=self.config.bucket, Key=key)["Body"]
            try:
                return body.read()
            finally:
                body.close()
        except Exception as exc:
            if _missing(exc):
                raise StorageMissing("Media object was not found.") from None
            raise StorageUnavailable("Durable media storage unavailable.") from None

    def exists(self, key: str) -> bool:
        try:
            self._client().head_object(Bucket=self.config.bucket, Key=key)
            return True
        except Exception as exc:
            if _missing(exc):
                return False
            raise StorageUnavailable("Durable media storage unavailable.") from None