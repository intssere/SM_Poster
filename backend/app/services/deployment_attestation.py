from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_MANIFEST_PATH = Path(__file__).resolve().parents[2] / ".build-provenance.json"


@dataclass(frozen=True)
class BuildProvenance:
    present: bool
    valid: bool
    commit_sha: str | None = None
    tree_sha: str | None = None
    error: str | None = None


def read_build_provenance(path: Path = DEFAULT_MANIFEST_PATH) -> BuildProvenance:
    """Read build-bound source identity without consulting Git or the network.

    The manifest is generated from a clean checkout immediately before a
    deployment build and is intentionally not tracked in Git. Missing or
    malformed evidence fails closed.
    """
    if not path.is_file():
        return BuildProvenance(present=False, valid=False, error="BUILD_PROVENANCE_MISSING")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return BuildProvenance(present=True, valid=False, error="BUILD_PROVENANCE_INVALID")

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return BuildProvenance(present=True, valid=False, error="BUILD_PROVENANCE_INVALID")
    commit_sha = payload.get("commit_sha")
    tree_sha = payload.get("tree_sha")
    if not (
        isinstance(commit_sha, str)
        and isinstance(tree_sha, str)
        and _SHA_RE.fullmatch(commit_sha)
        and _SHA_RE.fullmatch(tree_sha)
    ):
        return BuildProvenance(present=True, valid=False, error="BUILD_PROVENANCE_INVALID")
    return BuildProvenance(
        present=True,
        valid=True,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
    )


def safe_deployment_attestation(settings, *, path: Path = DEFAULT_MANIFEST_PATH) -> dict:
    provenance = read_build_provenance(path)
    return {
        "build_provenance": {
            "present": provenance.present,
            "valid": provenance.valid,
            "commit_sha": provenance.commit_sha,
            "tree_sha": provenance.tree_sha,
            "error": provenance.error,
        },
        # Boolean only: never disclose where the effective value came from or
        # any neighboring secret/configuration value.
        "pilot_disabled": settings.pinterest_single_pin_pilot_enabled is False,
    }
