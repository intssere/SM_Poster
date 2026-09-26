from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "write_build_provenance.py"

def load_module():
    spec = importlib.util.spec_from_file_location("task593d_provenance", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module

def bind(module, tmp_path: Path, monkeypatch, *, changed=None, commit="1"*40, tree="2"*40,
         overlay=b"reviewed", parents=None, parent_tree="2"*40, checkpoint_paths=None):
    overlay_path = tmp_path / ".replit"
    overlay_path.write_bytes(overlay)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "OVERLAY", overlay_path)
    monkeypatch.setattr(module, "OUTPUT", tmp_path / "backend" / ".build-provenance.json")
    paths = [".replit"] if changed is None else changed
    monkeypatch.setattr(module, "_changed_paths", lambda: paths)
    monkeypatch.setattr(module, "_checkpoint_parents", lambda: ["3"*40] if parents is None else parents)
    monkeypatch.setattr(module, "_checkpoint_changed_paths", lambda parent: [".replit"] if checkpoint_paths is None else checkpoint_paths)
    def fake_git(*args):
        if args == ("rev-parse", "HEAD"):
            return commit
        if args == ("rev-parse", "HEAD^{tree}"):
            return tree
        if len(args) == 2 and args[0] == "rev-parse" and args[1].endswith("^{tree}"):
            return parent_tree
        raise AssertionError(args)
    monkeypatch.setattr(module, "git", fake_git)
    return hashlib.sha256(overlay).hexdigest()

def test_exact_reviewed_dirty_overlay_succeeds(tmp_path, monkeypatch):
    module = load_module()
    digest = bind(module, tmp_path, monkeypatch)
    payload = module.build_provenance(expected_commit="1"*40, expected_tree="2"*40, expected_overlay_sha256=digest)
    assert payload["schema_version"] == 3
    assert payload["topology"] == module.DIRTY_OVERLAY_TOPOLOGY
    assert payload["canonical_commit_sha"] == "1"*40
    assert payload["release_commit_sha"] == "1"*40
    assert payload["release_overlay"] == {"path": ".replit", "sha256": digest}

def test_exact_clean_checkpoint_succeeds(tmp_path, monkeypatch):
    module = load_module()
    digest = bind(module, tmp_path, monkeypatch, changed=[], commit="4"*40, tree="5"*40,
                  parents=["3"*40], parent_tree="2"*40, checkpoint_paths=[".replit"])
    payload = module.build_provenance(expected_commit="3"*40, expected_tree="2"*40, expected_overlay_sha256=digest)
    assert payload["schema_version"] == 3
    assert payload["topology"] == module.CHECKPOINT_TOPOLOGY
    assert payload["canonical_commit_sha"] == "3"*40
    assert payload["canonical_tree_sha"] == "2"*40
    assert payload["release_commit_sha"] == "4"*40
    assert payload["release_tree_sha"] == "5"*40

@pytest.mark.parametrize("changed", [[".replit", "backend/app/main.py"], ["backend/app/main.py"]])
def test_dirty_drift_other_than_exact_overlay_fails(tmp_path, monkeypatch, changed):
    module = load_module()
    bind(module, tmp_path, monkeypatch, changed=changed)
    with pytest.raises(SystemExit, match="tracked differences"):
        module.build_provenance()

def test_clean_checkpoint_requires_all_expected_identity(tmp_path, monkeypatch):
    module = load_module()
    digest = bind(module, tmp_path, monkeypatch, changed=[], commit="4"*40, parents=["3"*40])
    with pytest.raises(SystemExit, match="requires exact expected"):
        module.build_provenance(expected_commit="3"*40, expected_tree="2"*40)
    with pytest.raises(SystemExit, match="requires exact expected"):
        module.build_provenance(expected_commit="3"*40, expected_overlay_sha256=digest)

@pytest.mark.parametrize("parents", [[], ["3"*40, "6"*40]])
def test_checkpoint_requires_exactly_one_parent(tmp_path, monkeypatch, parents):
    module = load_module()
    digest = bind(module, tmp_path, monkeypatch, changed=[], parents=parents)
    with pytest.raises(SystemExit, match="exactly one parent"):
        module.build_provenance(expected_commit="3"*40, expected_tree="2"*40, expected_overlay_sha256=digest)

def test_checkpoint_wrong_parent_fails(tmp_path, monkeypatch):
    module = load_module()
    digest = bind(module, tmp_path, monkeypatch, changed=[], parents=["8"*40])
    with pytest.raises(SystemExit, match="parent is not canonical"):
        module.build_provenance(expected_commit="3"*40, expected_tree="2"*40, expected_overlay_sha256=digest)

def test_checkpoint_wrong_parent_tree_fails(tmp_path, monkeypatch):
    module = load_module()
    digest = bind(module, tmp_path, monkeypatch, changed=[], parents=["3"*40], parent_tree="9"*40)
    with pytest.raises(SystemExit, match="canonical tree mismatch"):
        module.build_provenance(expected_commit="3"*40, expected_tree="2"*40, expected_overlay_sha256=digest)

def test_checkpoint_extra_changed_path_fails(tmp_path, monkeypatch):
    module = load_module()
    digest = bind(module, tmp_path, monkeypatch, changed=[], parents=["3"*40],
                  checkpoint_paths=[".replit", "backend/app/main.py"])
    with pytest.raises(SystemExit, match="checkpoint delta"):
        module.build_provenance(expected_commit="3"*40, expected_tree="2"*40, expected_overlay_sha256=digest)

def test_checkpoint_with_dirty_worktree_fails_as_checkpoint(tmp_path, monkeypatch):
    module = load_module()
    digest = bind(module, tmp_path, monkeypatch, changed=["backend/app/main.py"], parents=["3"*40])
    with pytest.raises(SystemExit, match="tracked differences"):
        module.build_provenance(expected_commit="3"*40, expected_tree="2"*40, expected_overlay_sha256=digest)

def test_canonical_commit_mismatch_fails(tmp_path, monkeypatch):
    module = load_module()
    digest = bind(module, tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="canonical commit mismatch"):
        module.build_provenance(expected_commit="9"*40, expected_overlay_sha256=digest)

def test_canonical_tree_mismatch_fails(tmp_path, monkeypatch):
    module = load_module()
    digest = bind(module, tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="canonical tree mismatch"):
        module.build_provenance(expected_tree="9"*40, expected_overlay_sha256=digest)

def test_reviewed_overlay_hash_mismatch_fails_for_dirty_overlay(tmp_path, monkeypatch):
    module = load_module()
    bind(module, tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="overlay hash mismatch"):
        module.build_provenance(expected_overlay_sha256="9"*64)

def test_reviewed_overlay_hash_mismatch_fails_for_checkpoint(tmp_path, monkeypatch):
    module = load_module()
    bind(module, tmp_path, monkeypatch, changed=[], parents=["3"*40])
    with pytest.raises(SystemExit, match="overlay hash mismatch"):
        module.build_provenance(expected_commit="3"*40, expected_tree="2"*40, expected_overlay_sha256="9"*64)
