"""Real temporary Git objects/worktrees; compilation is exclusively a local mock."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import release_source_guard as guard
import replit_release_build as build
import write_build_provenance as writer
import release_source_watch as monitor
from app.services.deployment_attestation import read_build_provenance, safe_deployment_attestation


def git(root, *args):
    env = {"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", *args],
                                   cwd=root, env=env, stderr=subprocess.PIPE).decode().strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "--initial-branch=fixture")
    git(root, "config", "user.name", "Isolated fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    for path, text in {
        ".replit": "[deployment]\n# canonical\n",
        ".gitignore": "ignored.py\nfrontend/dist/\nbackend/.build-provenance.json\n",
        "README.md": "verified source\n",
        "backend/marker.txt": "no application/service imports\n",
        "frontend/src/main.ts": "export const value = 1\n",
        "frontend/tsconfig.json": "{}\n",
        "scripts/check.sh": "#!/bin/sh\nexit 0\n",
    }.items():
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)
    (root / "scripts/check.sh").chmod(0o755)
    (root / "source-link").symlink_to("README.md")
    git(root, "add", ".")
    git(root, "commit", "-m", "canonical fixture")
    canonical = git(root, "rev-parse", "HEAD")
    tree = git(root, "rev-parse", "HEAD^{tree}")
    (root / ".replit").write_text("[deployment]\n# closed release overlay\n")
    git(root, "add", ".replit")
    git(root, "commit", "-m", "closed checkpoint")
    pins = {
        "EXPECTED_CANONICAL_COMMIT": canonical,
        "EXPECTED_CANONICAL_TREE": tree,
        "EXPECTED_RELEASE_COMMIT": git(root, "rev-parse", "HEAD"),
        "EXPECTED_RELEASE_TREE": git(root, "rev-parse", "HEAD^{tree}"),
        "EXPECTED_REPLIT_OVERLAY_SHA256": hashlib.sha256((root / ".replit").read_bytes()).hexdigest(),
    }
    for key, value in pins.items():
        monkeypatch.setenv(key, value)
    return root, pins


def compiler(command, *, cwd, check):
    assert command == ["npm", "--prefix", "frontend", "run", "build"]
    assert check is True
    output = cwd / "frontend/dist"
    output.mkdir(parents=True, exist_ok=True)
    (output / "index.html").write_text("<main>isolated compiled fixture</main>\n")
    (output / "main.js").write_text("export const value = 1;\n")
    (cwd / "frontend/tsconfig.tsbuildinfo").write_text('{"fixture":true}\n')


def refuses_before_compilation(root, match=None):
    calls = []
    with pytest.raises((guard.SourceGuardError, OSError, ValueError), match=match):
        build.run_build(root, lambda *args, **kwargs: calls.append(args))
    assert not calls
    assert writer.receipt_absent(root)


def repin_head(root, monkeypatch):
    monkeypatch.setenv("EXPECTED_RELEASE_COMMIT", git(root, "rev-parse", "HEAD"))
    monkeypatch.setenv("EXPECTED_RELEASE_TREE", git(root, "rev-parse", "HEAD^{tree}"))


def test_exact_checkpoint_builds_fresh_deterministic_receipt(repository):
    root, pins = repository
    source = guard.verify_source(root, pins)
    payload = build.run_build(root, compiler)
    assert payload == {**source, "artifacts": guard.artifact_inventory(root)}
    assert payload["schema_version"] == 4
    assert payload["release_commit_sha"] == pins["EXPECTED_RELEASE_COMMIT"]
    assert payload["release_tree_sha"] == pins["EXPECTED_RELEASE_TREE"]
    assert len(payload["source_inventory_sha256"]) == 64
    before = (root / guard.RECEIPT).read_bytes()
    assert build.run_build(root, compiler) == payload
    assert (root / guard.RECEIPT).read_bytes() == before
    assert read_build_provenance(root / guard.RECEIPT).valid


@pytest.mark.parametrize("name", list(guard.REQUIRED_EXPECTATIONS))
@pytest.mark.parametrize("bad", [None, "ABCDEF", "0" * 39, "G" * 64,
                                "uppercase-hex", "whitespace"])
def test_every_pin_is_mandatory_and_exact(repository, monkeypatch, name, bad):
    root, _ = repository
    if bad is None:
        monkeypatch.delenv(name)
    else:
        if bad == "uppercase-hex":
            bad = "A" * guard.REQUIRED_EXPECTATIONS[name]
        elif bad == "whitespace":
            bad = "0" * guard.REQUIRED_EXPECTATIONS[name] + "\n"
        monkeypatch.setenv(name, bad)
    refuses_before_compilation(root, name)


def test_wrong_release_sha_even_with_identical_tree_and_parent(repository):
    root, pins = repository
    other = git(root, "commit-tree", pins["EXPECTED_RELEASE_TREE"],
                "-p", pins["EXPECTED_CANONICAL_COMMIT"], "-m", "different commit same tree")
    git(root, "reset", "--hard", other)
    assert git(root, "rev-parse", "HEAD^{tree}") == pins["EXPECTED_RELEASE_TREE"]
    refuses_before_compilation(root, "release commit mismatch")


def test_wrong_release_tree(repository, monkeypatch):
    root, _ = repository
    monkeypatch.setenv("EXPECTED_RELEASE_TREE", "0" * 40)
    refuses_before_compilation(root, "release tree mismatch")


def test_metadata_descendant_is_rejected(repository):
    root, _ = repository
    path = root / ".agents/agent_assets_metadata.toml"
    path.parent.mkdir()
    path.write_text("fixture = true\n")
    git(root, "add", ".agents")
    git(root, "commit", "-m", "Initialize agent assets metadata file")
    refuses_before_compilation(root, "release commit mismatch")


@pytest.mark.parametrize("key", ["EXPECTED_CANONICAL_COMMIT", "EXPECTED_CANONICAL_TREE"])
def test_wrong_canonical_identity(repository, monkeypatch, key):
    root, _ = repository
    monkeypatch.setenv(key, "0" * 40)
    refuses_before_compilation(root, "canonical")


@pytest.mark.parametrize("parents", [0, 2])
def test_exactly_one_parent_required(repository, monkeypatch, parents):
    root, pins = repository
    args = ["commit-tree", pins["EXPECTED_RELEASE_TREE"]]
    if parents:
        args += ["-p", pins["EXPECTED_CANONICAL_COMMIT"], "-p", pins["EXPECTED_RELEASE_COMMIT"]]
    sha = git(root, *args, "-m", "invalid parent topology")
    git(root, "reset", "--hard", sha)
    repin_head(root, monkeypatch)
    refuses_before_compilation(root, "exactly one canonical parent")


def test_checkpoint_delta_must_only_be_overlay(repository, monkeypatch):
    root, pins = repository
    git(root, "reset", "--soft", pins["EXPECTED_CANONICAL_COMMIT"])
    (root / "README.md").write_text("extra tracked delta\n")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "extra delta")
    repin_head(root, monkeypatch)
    refuses_before_compilation(root, "exactly .replit")


@pytest.mark.parametrize("path", [
    ".agents/agent_assets_metadata.toml", "frontend/src/extra.ts", "ignored.py",
    "frontend/node_modules_fake/rogue.js", "frontend/dist_fake/rogue.js",
    "backend/__pycache__/rogue.py",
])
def test_untracked_and_ignored_source_is_not_blanket_excluded(repository, path):
    root, _ = repository
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("unexpected source\n")
    refuses_before_compilation(root, "unexpected untracked/ignored")


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_index_flags_cannot_hide_physical_content(repository, flag):
    root, _ = repository
    git(root, "update-index", flag, "README.md")
    (root / "README.md").write_text("hidden physical drift\n")
    refuses_before_compilation(root, "source content drift")


def test_staged_index_drift_is_rejected_even_if_worktree_restored(repository):
    root, _ = repository
    original = (root / "README.md").read_bytes()
    (root / "README.md").write_text("staged difference\n")
    git(root, "add", "README.md")
    (root / "README.md").write_bytes(original)
    refuses_before_compilation(root, "index differs")


@pytest.mark.parametrize("change", ["content", "overlay", "mode", "symlink-target",
                                    "file-to-symlink", "symlink-to-file", "fifo", "ancestor"])
def test_physical_mode_symlink_and_content_drift(repository, change, tmp_path):
    root, _ = repository
    if change == "content":
        (root / "README.md").write_text("drift\n")
    elif change == "overlay":
        (root / ".replit").write_text("unapproved overlay\n")
    elif change == "mode":
        git(root, "config", "core.filemode", "false")
        (root / "README.md").chmod(0o755)
    elif change == "symlink-target":
        (root / "source-link").unlink()
        (root / "source-link").symlink_to(".gitignore")
    elif change == "file-to-symlink":
        (root / "README.md").unlink()
        (root / "README.md").symlink_to("backend/marker.txt")
    elif change == "symlink-to-file":
        (root / "source-link").unlink()
        (root / "source-link").write_text("README.md")
    elif change == "fifo":
        (root / "README.md").unlink()
        os.mkfifo(root / "README.md")
    else:
        (root / "frontend/src").rename(tmp_path / "outside")
        (root / "frontend/src").symlink_to(tmp_path / "outside", target_is_directory=True)
    refuses_before_compilation(root)


def test_overlay_pin_must_match_physical_overlay(repository, monkeypatch):
    root, _ = repository
    monkeypatch.setenv("EXPECTED_REPLIT_OVERLAY_SHA256", "0" * 64)
    refuses_before_compilation(root, "reviewed .replit hash mismatch")


@pytest.mark.parametrize("unusable", ["absent", "corrupt", "objects"])
def test_missing_or_unusable_git_never_falls_back_to_receipt(repository, unusable):
    root, _ = repository
    (root / guard.RECEIPT).write_text('{"schema_version":4,"stale":true}')
    if unusable == "absent":
        (root / ".git").rename(root.parent / "detached-git")
    elif unusable == "corrupt":
        (root / ".git").rename(root.parent / "detached-git")
        (root / ".git").write_text("not a Git pointer\n")
    else:
        (root / ".git/objects").rename(root / ".git/no-objects")
    refuses_before_compilation(root, "Git")


def test_inherited_git_overrides_do_not_change_attested_source(repository, monkeypatch):
    root, pins = repository
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.setenv(key, "/nonexistent/untrusted")
    assert guard.verify_source(root, pins)["release_commit_sha"] == pins["EXPECTED_RELEASE_COMMIT"]


@pytest.mark.parametrize("kind", ["release", "canonical", "tree", "subtree", "blob"])
def test_corrupt_git_objects_cannot_redefine_pinned_identity(repository, kind):
    import zlib
    root, pins = repository
    if kind == "release":
        oid = pins["EXPECTED_RELEASE_COMMIT"]
    elif kind == "canonical":
        oid = pins["EXPECTED_CANONICAL_COMMIT"]
    elif kind == "tree":
        oid = pins["EXPECTED_RELEASE_TREE"]
    else:
        oid = git(root, "rev-parse", "HEAD:frontend" if kind == "subtree" else "HEAD:README.md")
    file = root / ".git/objects" / oid[:2] / oid[2:]
    header, content = zlib.decompress(file.read_bytes()).split(b"\0", 1)
    changed = content[:-1] + b"!"
    # Git loose objects are deliberately read-only; fault injection is confined
    # to this disposable fixture, never shared/worktree object storage.
    file.chmod(0o600)
    file.write_bytes(zlib.compress(header + b"\0" + changed))
    if kind == "blob":
        (root / "README.md").write_bytes(changed)
    refuses_before_compilation(root)


def test_grafts_do_not_redefine_verified_commit_parents(repository):
    root, pins = repository
    (root / ".git/info/grafts").write_text(pins["EXPECTED_RELEASE_COMMIT"] + "\n")
    assert guard.verify_source(root, pins)["canonical_commit_sha"] == pins["EXPECTED_CANONICAL_COMMIT"]


def test_approved_caches_are_narrow_and_not_part_of_source_digest(repository):
    root, pins = repository
    before = guard.verify_source(root, pins)
    for directory in guard.DEPENDENCY_DIRS:
        path = root / directory / "dependency.dat"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("explicitly outside source contract\n")
    assert guard.verify_source(root, pins) == before
    assert build.run_build(root, compiler)["source_inventory_sha256"] == before["source_inventory_sha256"]


def test_cache_root_cannot_be_symlink_escape(repository, tmp_path):
    root, _ = repository
    (root / "node_modules").symlink_to(tmp_path, target_is_directory=True)
    refuses_before_compilation(root, "real directory")


def test_stale_receipt_is_discarded_and_not_used_as_authority(repository):
    root, _ = repository
    (root / guard.RECEIPT).write_text("malformed stale data")
    def compile_fresh(*args, **kwargs):
        assert writer.receipt_absent(root)
        compiler(*args, **kwargs)
    payload = build.run_build(root, compile_fresh)
    assert json.loads((root / guard.RECEIPT).read_text()) == payload


def test_stale_receipt_symlink_cannot_mutate_external_file(repository, tmp_path):
    root, _ = repository
    outside = tmp_path / "do-not-touch"
    outside.write_text("outside unchanged")
    (root / guard.RECEIPT).symlink_to(outside)
    build.run_build(root, compiler)
    assert outside.read_text() == "outside unchanged"
    assert not (root / guard.RECEIPT).is_symlink()


@pytest.mark.parametrize("drift", ["source", "overlay", "head", "untracked", "provenance", "mode"])
def test_during_build_drift_fails_without_receipt(repository, drift):
    root, _ = repository
    def mutate(*args, **kwargs):
        compiler(*args, **kwargs)
        if drift == "source":
            (root / "README.md").write_text("during build drift\n")
        elif drift == "overlay":
            (root / ".replit").write_text("during build overlay\n")
        elif drift == "head":
            git(root, "commit", "--allow-empty", "-m", "during build HEAD drift")
        elif drift == "untracked":
            (root / "ignored.py").write_text("ignored drift\n")
        elif drift == "provenance":
            (root / guard.RECEIPT).write_text('{"tampered":true}')
        else:
            (root / "scripts/check.sh").chmod(0o644)
    with pytest.raises(guard.SourceGuardError):
        build.run_build(root, mutate)
    assert writer.receipt_absent(root)


@pytest.mark.parametrize("drift", ["content", "mode", "head"])
def test_during_build_change_then_restore_is_also_rejected(repository, drift):
    root, pins = repository
    def mutate_then_restore(*args, **kwargs):
        compiler(*args, **kwargs)
        if drift == "content":
            path = root / "README.md"
            original = path.read_bytes()
            path.write_text("temporary source drift\n")
            path.write_bytes(original)
        elif drift == "mode":
            path = root / "README.md"
            path.chmod(0o755)
            path.chmod(0o644)
        else:
            git(root, "commit", "--allow-empty", "-m", "transient HEAD drift")
            git(root, "reset", "--soft", pins["EXPECTED_RELEASE_COMMIT"])
    with pytest.raises(guard.SourceGuardError, match="changed during build"):
        build.run_build(root, mutate_then_restore)
    assert writer.receipt_absent(root)


@pytest.mark.parametrize("path", ["ignored.py", "frontend/src/transient.ts",
                                "empty-untracked/transient.py"])
def test_transient_untracked_source_is_rejected_even_after_removal(repository, path):
    root, _ = repository
    (root / "empty-untracked").mkdir()
    def transient(*args, **kwargs):
        compiler(*args, **kwargs)
        file = root / path
        file.write_text("temporary untracked build input")
        file.unlink()
    with pytest.raises(guard.SourceGuardError, match="filesystem changed"):
        build.run_build(root, transient)
    assert writer.receipt_absent(root)


def test_source_watch_unavailable_is_fail_closed(repository, monkeypatch):
    root, _ = repository
    monkeypatch.setattr(monitor.ctypes, "CDLL", lambda *args, **kwargs: object())
    refuses_before_compilation(root, "monitoring unavailable")


@pytest.mark.parametrize("mask", [monitor.QUEUE_OVERFLOW, monitor.WATCH_LOST])
def test_source_watch_loss_or_overflow_is_fail_closed(repository, monkeypatch, mask):
    import struct
    root, _ = repository
    with monitor.SourceWatch(root) as watch:
        original = monitor.os.read
        def overflow(fd, count):
            if fd == watch.fd:
                return struct.pack("iIII", -1, mask, 0, 0)
            return original(fd, count)
        monkeypatch.setattr(monitor.os, "read", overflow)
        with pytest.raises(guard.SourceGuardError, match="overflow/lost"):
            watch.check()


@pytest.mark.parametrize("drift", ["receipt", "artifact", "source"])
def test_receipt_publication_revalidates_everything(repository, monkeypatch, drift):
    root, _ = repository
    original = build.write_receipt
    def tamper(root, payload):
        original(root, payload)
        if drift == "receipt":
            (root / guard.RECEIPT).write_text('{"tampered":true}')
        elif drift == "artifact":
            (root / "frontend/dist/main.js").write_text("tampered artifact")
        else:
            (root / "README.md").write_text("tampered source")
    monkeypatch.setattr(build, "write_receipt", tamper)
    with pytest.raises(guard.SourceGuardError):
        build.run_build(root, compiler)
    assert writer.receipt_absent(root)


def test_old_artifacts_cannot_substitute_for_fresh_compilation(repository):
    root, _ = repository
    compiler(["npm", "--prefix", "frontend", "run", "build"], cwd=root, check=True)
    with pytest.raises(guard.SourceGuardError, match="frontend/dist"):
        build.run_build(root, lambda *args, **kwargs: None)
    assert writer.receipt_absent(root)


def test_build_failure_has_no_receipt(repository):
    root, _ = repository
    def failure(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "local mocked compiler")
    with pytest.raises(subprocess.CalledProcessError):
        build.run_build(root, failure)
    assert writer.receipt_absent(root)


def test_artifact_symlink_is_rejected(repository):
    root, _ = repository
    def bad_artifact(*args, **kwargs):
        compiler(*args, **kwargs)
        (root / "frontend/dist/escape").symlink_to("../../README.md")
    with pytest.raises(guard.SourceGuardError, match="artifacts"):
        build.run_build(root, bad_artifact)
    assert writer.receipt_absent(root)


def test_real_git_worktree_pointer_supported(repository, tmp_path, monkeypatch):
    root, pins = repository
    target = tmp_path / "worktree"
    git(root, "worktree", "add", "--detach", str(target), pins["EXPECTED_RELEASE_COMMIT"])
    assert (target / ".git").is_file()
    assert guard.verify_source(target, pins) == guard.verify_source(root, pins)
    assert build.run_build(target, compiler)["release_commit_sha"] == pins["EXPECTED_RELEASE_COMMIT"]


def test_writer_requires_all_exact_expectations(repository):
    root, pins = repository
    with pytest.raises(guard.SourceGuardError, match="EXPECTED_RELEASE_COMMIT"):
        writer.build_provenance(expected_commit=pins["EXPECTED_CANONICAL_COMMIT"],
                                expected_tree=pins["EXPECTED_CANONICAL_TREE"], root=root)
    result = writer.build_provenance(
        expected_commit=pins["EXPECTED_CANONICAL_COMMIT"],
        expected_tree=pins["EXPECTED_CANONICAL_TREE"],
        expected_release_commit=pins["EXPECTED_RELEASE_COMMIT"],
        expected_release_tree=pins["EXPECTED_RELEASE_TREE"],
        expected_overlay_sha256=pins["EXPECTED_REPLIT_OVERLAY_SHA256"], root=root)
    assert result == guard.verify_source(root, pins)
    assert writer.receipt_absent(root)


def test_standalone_writer_cannot_issue_uncompiled_receipt():
    with pytest.raises(SystemExit, match="verified compilation"):
        writer.main()


@pytest.mark.parametrize("field", ["source_inventory_sha256", "artifacts", "source_file_count"])
def test_v4_runtime_reader_rejects_incomplete_receipt(repository, field):
    root, _ = repository
    payload = build.run_build(root, compiler)
    payload.pop(field)
    (root / guard.RECEIPT).write_text(json.dumps(payload))
    assert not read_build_provenance(root / guard.RECEIPT).valid


@pytest.mark.parametrize("field,bad", [
    ("schema_version", []), ("schema_version", 4.0), ("topology", []),
    ("source_inventory_sha256", "G" * 64), ("source_file_count", True),
    ("artifacts", {"frontend/dist": {"inventory_sha256": "0" * 64, "file_count": True}}),
    ("artifacts", {"frontend/dist": {"inventory_sha256": "0" * 64, "file_count": 1},
                   "frontend/tsconfig.tsbuildinfo": None}),
])
def test_malformed_v4_receipt_fails_closed_without_reader_exception(repository, field, bad):
    root, _ = repository
    payload = build.run_build(root, compiler)
    payload[field] = bad
    (root / guard.RECEIPT).write_text(json.dumps(payload))
    assert not read_build_provenance(root / guard.RECEIPT).valid


def test_v4_public_attestation_exposes_bound_inventory_not_secret_pins(repository):
    from types import SimpleNamespace
    root, _ = repository
    payload = build.run_build(root, compiler)
    result = safe_deployment_attestation(
        SimpleNamespace(pinterest_single_pin_pilot_enabled=False), path=root / guard.RECEIPT)
    assert result["build_provenance"]["source_inventory_sha256"] == payload["source_inventory_sha256"]
    assert result["build_provenance"]["artifacts"] == payload["artifacts"]
    assert result["pilot_disabled"] is True


def test_closed_deployment_configuration_unchanged():
    import tomllib
    deployment = tomllib.loads((SCRIPTS.parent / ".replit").read_text())["deployment"]
    assert deployment["build"] == ["python", "scripts/replit_release_build.py"]
    assert "PUBLISHING_ENABLED=false" in deployment["run"]
    assert "ROUTINE_PINTEREST_DRY_RUN=true" in deployment["run"]