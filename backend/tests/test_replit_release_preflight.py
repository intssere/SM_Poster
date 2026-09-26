from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "write_build_provenance.py"

def load_module():
    spec = importlib.util.spec_from_file_location("task593c_provenance", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module

def bind(module, tmp_path: Path, monkeypatch, *, changed=None, commit="1"*40, tree="2"*40, overlay=b"reviewed"):
    overlay_path = tmp_path / ".replit"
    overlay_path.write_bytes(overlay)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "OVERLAY", overlay_path)
    monkeypatch.setattr(module, "OUTPUT", tmp_path / "backend" / ".build-provenance.json")
    paths = [".replit"] if changed is None else changed
    monkeypatch.setattr(module, "_changed_paths", lambda: paths)
    monkeypatch.setattr(module, "git", lambda *args: commit if args == ("rev-parse", "HEAD") else tree)
    return hashlib.sha256(overlay).hexdigest()

def test_exact_reviewed_overlay_succeeds(tmp_path, monkeypatch):
    module = load_module()
    digest = bind(module, tmp_path, monkeypatch)
    payload = module.build_provenance(expected_commit="1"*40, expected_tree="2"*40, expected_overlay_sha256=digest)
    assert payload["schema_version"] == 2
    assert payload["release_overlay"] == {"path": ".replit", "sha256": digest}

@pytest.mark.parametrize("changed", [[], [".replit", "backend/app/main.py"], ["backend/app/main.py"]])
def test_any_tracked_drift_other_than_exact_overlay_fails(tmp_path, monkeypatch, changed):
    module = load_module()
    bind(module, tmp_path, monkeypatch, changed=changed)
    with pytest.raises(SystemExit, match="tracked differences"):
        module.build_provenance()

def test_canonical_commit_mismatch_fails(tmp_path, monkeypatch):
    module = load_module()
    bind(module, tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="canonical commit mismatch"):
        module.build_provenance(expected_commit="9"*40)

def test_canonical_tree_mismatch_fails(tmp_path, monkeypatch):
    module = load_module()
    bind(module, tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="canonical tree mismatch"):
        module.build_provenance(expected_tree="9"*40)

def test_reviewed_overlay_hash_mismatch_fails(tmp_path, monkeypatch):
    module = load_module()
    bind(module, tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="overlay hash mismatch"):
        module.build_provenance(expected_overlay_sha256="9"*64)
