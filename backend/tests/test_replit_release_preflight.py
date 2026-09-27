from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tomllib

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "write_build_provenance.py"
BUILD_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "replit_release_build.py"
REPLIT = Path(__file__).resolve().parents[2] / ".replit"

def load_module():
    spec = importlib.util.spec_from_file_location("task593d_provenance", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module

def load_build_module():
    spec = importlib.util.spec_from_file_location("task595_release_build", BUILD_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module

def set_expected_identity(monkeypatch):
    expected = {
        "EXPECTED_CANONICAL_COMMIT": "1" * 40,
        "EXPECTED_CANONICAL_TREE": "2" * 40,
        "EXPECTED_REPLIT_OVERLAY_SHA256": "3" * 64,
    }
    for name, value in expected.items():
        monkeypatch.setenv(name, value)
    return expected

def expected_manifest(expected, overlay_sha256="3" * 64):
    return {
        "schema_version": 3,
        "topology": "canonical_parent_with_checkpoint_overlay",
        "canonical_commit_sha": expected["EXPECTED_CANONICAL_COMMIT"],
        "canonical_tree_sha": expected["EXPECTED_CANONICAL_TREE"],
        "release_overlay": {
            "path": ".replit",
            "sha256": overlay_sha256,
        },
    }

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
    payload = module.build_provenance(expected_commit="3"*40, expected_tree="2"*40)
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

def test_clean_checkpoint_requires_canonical_identity_but_not_overlay_pin(tmp_path, monkeypatch):
    module = load_module()
    digest = bind(module, tmp_path, monkeypatch, changed=[], commit="4"*40, parents=["3"*40])
    payload = module.build_provenance(expected_commit="3"*40, expected_tree="2"*40)
    assert payload["release_overlay"]["sha256"] == digest
    with pytest.raises(SystemExit, match="requires exact expected"):
        module.build_provenance(expected_commit="3"*40)

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

def test_deployment_build_invokes_pinned_provenance_before_frontend(tmp_path, monkeypatch):
    config = tomllib.loads(REPLIT.read_text(encoding="utf-8"))
    assert config["deployment"]["build"] == ["python", "scripts/replit_release_build.py"]
    module = load_build_module()
    expected = set_expected_identity(monkeypatch)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    manifest = tmp_path / "backend" / ".build-provenance.json"
    manifest.parent.mkdir()
    monkeypatch.setattr(module, "MANIFEST", manifest)
    commands = []

    def runner(command, *, cwd, check):
        assert cwd == tmp_path and check is True
        commands.append(command)
        if len(commands) == 1:
            manifest.write_text(json.dumps(expected_manifest(expected, overlay_sha256)), encoding="utf-8")

    monkeypatch.setattr(module.subprocess, "run", runner)
    module.main()
    assert commands == [
        [module.sys.executable, str(tmp_path / "scripts" / "write_build_provenance.py")],
        ["npm", "--prefix", "frontend", "run", "build"],
    ]

@pytest.mark.parametrize("missing", [
    "EXPECTED_CANONICAL_COMMIT",
    "EXPECTED_CANONICAL_TREE",
    "EXPECTED_REPLIT_OVERLAY_SHA256",
])
def test_release_build_refuses_missing_independent_pin(monkeypatch, missing):
    module = load_build_module()
    set_expected_identity(monkeypatch)
    monkeypatch.delenv(missing)
    monkeypatch.setattr(
        module.subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("build must not run without pins"),
    )
    with pytest.raises(SystemExit, match=missing):
        module.main()

def test_release_build_refuses_non_checkpoint_manifest_before_frontend(tmp_path, monkeypatch):
    module = load_build_module()
    expected = set_expected_identity(monkeypatch)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    manifest = tmp_path / "backend" / ".build-provenance.json"
    manifest.parent.mkdir()
    monkeypatch.setattr(module, "MANIFEST", manifest)
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        manifest.write_text(
            json.dumps({**expected_manifest(expected), "topology": "canonical_with_worktree_overlay"}),
            encoding="utf-8",
        )

    monkeypatch.setattr(module.subprocess, "run", runner)
    with pytest.raises(SystemExit, match="certified schema-v3 checkpoint"):
        module.main()
    assert len(commands) == 1

def test_release_build_refuses_unavailable_git_metadata_before_frontend(monkeypatch):
    module = load_build_module()
    set_expected_identity(monkeypatch)
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        raise subprocess.CalledProcessError(128, command)

    monkeypatch.setattr(module.subprocess, "run", runner)
    with pytest.raises(subprocess.CalledProcessError):
        module.main()
    assert len(commands) == 1


def test_release_build_refuses_overlay_mutation_during_frontend(tmp_path, monkeypatch):
    module = load_build_module()
    expected = set_expected_identity(monkeypatch)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    manifest = tmp_path / "backend" / ".build-provenance.json"
    manifest.parent.mkdir()
    monkeypatch.setattr(module, "MANIFEST", manifest)
    overlay = tmp_path / ".replit"
    overlay.write_bytes(b"reviewed")
    monkeypatch.setattr(module, "OVERLAY", overlay)
    digest = hashlib.sha256(b"reviewed").hexdigest()
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            manifest.write_text(json.dumps(expected_manifest(expected, digest)), encoding="utf-8")
        elif len(calls) == 2:
            overlay.write_bytes(b"mutated")

    monkeypatch.setattr(module.subprocess, "run", runner)
    with pytest.raises(SystemExit, match="overlay changed during build"):
        module.main()


def test_release_build_does_not_require_overlay_environment_pin(tmp_path, monkeypatch):
    module = load_build_module()
    expected = set_expected_identity(monkeypatch)
    monkeypatch.delenv("EXPECTED_REPLIT_OVERLAY_SHA256", raising=False)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    manifest = tmp_path / "backend" / ".build-provenance.json"
    manifest.parent.mkdir()
    monkeypatch.setattr(module, "MANIFEST", manifest)
    overlay = tmp_path / ".replit"
    overlay.write_bytes(b"reviewed")
    monkeypatch.setattr(module, "OVERLAY", overlay)
    digest = hashlib.sha256(b"reviewed").hexdigest()
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            manifest.write_text(json.dumps(expected_manifest(expected, digest)), encoding="utf-8")

    monkeypatch.setattr(module.subprocess, "run", runner)
    module.main()
    assert len(calls) == 2
