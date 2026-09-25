import json
from pathlib import Path
from types import SimpleNamespace

from app.services.deployment_attestation import (
    read_build_provenance,
    safe_deployment_attestation,
)


COMMIT = "1" * 40
TREE = "2" * 40


def test_build_provenance_missing_fails_closed(tmp_path: Path):
    result = read_build_provenance(tmp_path / "missing.json")
    assert result.present is False
    assert result.valid is False
    assert result.commit_sha is None
    assert result.tree_sha is None
    assert result.error == "BUILD_PROVENANCE_MISSING"


def test_build_provenance_malformed_fails_closed(tmp_path: Path):
    path = tmp_path / "manifest.json"
    path.write_text('{"schema_version":1,"commit_sha":"not-a-sha"}', encoding="utf-8")
    result = read_build_provenance(path)
    assert result.present is True
    assert result.valid is False
    assert result.commit_sha is None
    assert result.tree_sha is None
    assert result.error == "BUILD_PROVENANCE_INVALID"


def test_build_provenance_accepts_exact_build_identity(tmp_path: Path):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"schema_version": 1, "commit_sha": COMMIT, "tree_sha": TREE}),
        encoding="utf-8",
    )
    result = read_build_provenance(path)
    assert result.present is True
    assert result.valid is True
    assert result.commit_sha == COMMIT
    assert result.tree_sha == TREE
    assert result.error is None


def test_safe_attestation_exposes_only_boolean_pilot_state_and_provenance(tmp_path: Path):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"schema_version": 1, "commit_sha": COMMIT, "tree_sha": TREE}),
        encoding="utf-8",
    )
    settings = SimpleNamespace(
        pinterest_single_pin_pilot_enabled=False,
        pinterest_single_pin_pilot_publication_id="secret-ish-id",
        pinterest_client_secret="do-not-expose",
    )
    result = safe_deployment_attestation(settings, path=path)
    assert result == {
        "build_provenance": {
            "present": True,
            "valid": True,
            "commit_sha": COMMIT,
            "tree_sha": TREE,
            "error": None,
        },
        "pilot_disabled": True,
    }
    serialized = json.dumps(result)
    assert "secret-ish-id" not in serialized
    assert "do-not-expose" not in serialized


def test_safe_attestation_reports_enabled_pilot_without_raw_config(tmp_path: Path):
    settings = SimpleNamespace(pinterest_single_pin_pilot_enabled=True)
    result = safe_deployment_attestation(settings, path=tmp_path / "missing.json")
    assert result["pilot_disabled"] is False
    assert set(result) == {"build_provenance", "pilot_disabled"}
