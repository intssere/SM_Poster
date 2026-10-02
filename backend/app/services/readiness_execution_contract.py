"""Management-only bindings; no application models, SDK or provider imports."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json

OPERATION = "object_storage_readiness_v1"
# Historical descriptor protocol label, not the physical Alembic head.
# Keep it stable at database head 0033 to preserve persisted binding digests.
SCHEMA_REVISION = "0032"
PROBE_VERSION = "1"
PROBE_PREFIX = "task61-readiness/"
PROBE_SIZE = 70
PROBE_DIGEST = "dc5687b50f70cb95379bfced5a0eae768dd4382cd6b393ee77d65bbdd6373fbf"


class ReadinessError(Exception):
    """Only fixed codes may reach responses/logs; never underlying exceptions."""

    def __init__(self, code: str, status_code: int = 503):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True)
class ReadinessBinding:
    canonical_commit_sha: str
    canonical_tree_sha: str
    release_commit_sha: str
    release_tree_sha: str
    overlay_sha256: str
    probe_sha256: str
    topology: str

    @property
    def scope(self) -> tuple[str, str, str]:
        return OPERATION, self.release_commit_sha, self.release_tree_sha

    @property
    def descriptor(self) -> dict:
        return {
            **asdict(self), "operation": OPERATION,
            "schema_revision": SCHEMA_REVISION, "probe_version": PROBE_VERSION,
            "prefix": PROBE_PREFIX, "byte_size": PROBE_SIZE,
            "expected_digest": PROBE_DIGEST,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.descriptor, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()