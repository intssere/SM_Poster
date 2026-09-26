import json
from pathlib import Path
from types import SimpleNamespace

from app.services.deployment_attestation import read_build_provenance, safe_deployment_attestation

COMMIT = "1" * 40
TREE = "2" * 40
RELEASE_COMMIT = "4" * 40
RELEASE_TREE = "5" * 40
OVERLAY_HASH = "3" * 64

def manifest(*, checkpoint=False):
    return {
        "schema_version": 3,
        "topology": "canonical_parent_with_checkpoint_overlay" if checkpoint else "canonical_with_worktree_overlay",
        "canonical_commit_sha": COMMIT,
        "canonical_tree_sha": TREE,
        "release_commit_sha": RELEASE_COMMIT if checkpoint else COMMIT,
        "release_tree_sha": RELEASE_TREE if checkpoint else TREE,
        "release_overlay": {"path": ".replit", "sha256": OVERLAY_HASH},
    }

def test_build_provenance_missing_fails_closed(tmp_path: Path):
    result = read_build_provenance(tmp_path / "missing.json")
    assert not result.present and not result.valid
    assert result.error == "BUILD_PROVENANCE_MISSING"

def test_build_provenance_malformed_fails_closed(tmp_path: Path):
    path = tmp_path / "manifest.json"
    path.write_text('{"schema_version":3,"canonical_commit_sha":"not-a-sha"}', encoding="utf-8")
    result = read_build_provenance(path)
    assert result.present and not result.valid
    assert result.error == "BUILD_PROVENANCE_INVALID"

def test_build_provenance_rejects_unapproved_overlay_path(tmp_path: Path):
    path = tmp_path / "manifest.json"
    payload = manifest()
    payload["release_overlay"]["path"] = "backend/app/main.py"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert not read_build_provenance(path).valid

def test_build_provenance_accepts_dirty_overlay_topology(tmp_path: Path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest()), encoding="utf-8")
    result = read_build_provenance(path)
    assert result.valid
    assert result.commit_sha == COMMIT and result.tree_sha == TREE
    assert result.release_commit_sha == COMMIT and result.release_tree_sha == TREE
    assert result.topology == "canonical_with_worktree_overlay"

def test_build_provenance_accepts_checkpoint_topology(tmp_path: Path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest(checkpoint=True)), encoding="utf-8")
    result = read_build_provenance(path)
    assert result.valid
    assert result.commit_sha == COMMIT and result.tree_sha == TREE
    assert result.release_commit_sha == RELEASE_COMMIT and result.release_tree_sha == RELEASE_TREE
    assert result.topology == "canonical_parent_with_checkpoint_overlay"

def test_dirty_overlay_topology_requires_release_equal_canonical(tmp_path: Path):
    path = tmp_path / "manifest.json"
    payload = manifest()
    payload["release_commit_sha"] = RELEASE_COMMIT
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert not read_build_provenance(path).valid

def test_schema_v2_fails_closed(tmp_path: Path):
    path = tmp_path / "manifest.json"
    payload = manifest()
    payload["schema_version"] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert not read_build_provenance(path).valid

def test_safe_attestation_exposes_only_boolean_pilot_state_and_provenance(tmp_path: Path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest(checkpoint=True)), encoding="utf-8")
    settings = SimpleNamespace(
        pinterest_single_pin_pilot_enabled=False,
        pinterest_single_pin_pilot_publication_id="secret-ish-id",
        pinterest_client_secret="do-not-expose",
    )
    result = safe_deployment_attestation(settings, path=path)
    assert result == {
        "build_provenance": {
            "present": True, "valid": True,
            "topology": "canonical_parent_with_checkpoint_overlay",
            "canonical_commit_sha": COMMIT, "canonical_tree_sha": TREE,
            "release_commit_sha": RELEASE_COMMIT, "release_tree_sha": RELEASE_TREE,
            "release_overlay": {"path": ".replit", "sha256": OVERLAY_HASH},
            "error": None,
        },
        "pilot_disabled": True,
    }
    serialized = json.dumps(result)
    assert "secret-ish-id" not in serialized and "do-not-expose" not in serialized

def test_safe_attestation_reports_enabled_pilot_without_raw_config(tmp_path: Path):
    settings = SimpleNamespace(pinterest_single_pin_pilot_enabled=True)
    result = safe_deployment_attestation(settings, path=tmp_path / "missing.json")
    assert result["pilot_disabled"] is False
    assert set(result) == {"build_provenance", "pilot_disabled"}
