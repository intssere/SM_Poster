from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_MANIFEST_PATH = Path(__file__).resolve().parents[2] / ".build-provenance.json"


@dataclass(frozen=True)
class BuildProvenance:
    present: bool
    valid: bool
    commit_sha: str | None = None
    tree_sha: str | None = None
    overlay_path: str | None = None
    overlay_sha256: str | None = None
    error: str | None = None


def read_build_provenance(path: Path = DEFAULT_MANIFEST_PATH) -> BuildProvenance:
    """Read canonical source plus approved release-overlay identity offline."""
    if not path.is_file():
        return BuildProvenance(present=False, valid=False, error="BUILD_PROVENANCE_MISSING")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return BuildProvenance(present=True, valid=False, error="BUILD_PROVENANCE_INVALID")

    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        return BuildProvenance(present=True, valid=False, error="BUILD_PROVENANCE_INVALID")
    commit_sha = payload.get("canonical_commit_sha")
    tree_sha = payload.get("canonical_tree_sha")
    overlay = payload.get("release_overlay")
    if not (
        isinstance(commit_sha, str)
        and isinstance(tree_sha, str)
        and _SHA_RE.fullmatch(commit_sha)
        and _SHA_RE.fullmatch(tree_sha)
        and isinstance(overlay, dict)
        and overlay.get("path") == ".replit"
        and isinstance(overlay.get("sha256"), str)
        and _HEX_RE.fullmatch(overlay["sha256"])
    ):
        return BuildProvenance(present=True, valid=False, error="BUILD_PROVENANCE_INVALID")
    return BuildProvenance(
        present=True,
        valid=True,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        overlay_path=".replit",
        overlay_sha256=overlay["sha256"],
    )


def safe_deployment_attestation(settings, *, path: Path = DEFAULT_MANIFEST_PATH) -> dict:
    provenance = read_build_provenance(path)
    return {
        "build_provenance": {
            "present": provenance.present,
            "valid": provenance.valid,
            "canonical_commit_sha": provenance.commit_sha,
            "canonical_tree_sha": provenance.tree_sha,
            "release_overlay": (
                {"path": provenance.overlay_path, "sha256": provenance.overlay_sha256}
                if provenance.valid
                else None
            ),
            "error": provenance.error,
        },
        "pilot_disabled": settings.pinterest_single_pin_pilot_enabled is False,
    }
