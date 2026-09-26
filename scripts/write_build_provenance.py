#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "backend" / ".build-provenance.json"
OVERLAY = ROOT / ".replit"
ALLOWED_OVERLAY_PATH = ".replit"
DIRTY_OVERLAY_TOPOLOGY = "canonical_with_worktree_overlay"
CHECKPOINT_TOPOLOGY = "canonical_parent_with_checkpoint_overlay"

def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, stderr=subprocess.STDOUT).strip()

def _changed_paths() -> list[str]:
    paths: list[str] = []
    for line in git("status", "--porcelain", "--untracked-files=no").splitlines():
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    return paths

def _checkpoint_parents() -> list[str]:
    fields = git("rev-list", "--parents", "-n", "1", "HEAD").split()
    return fields[1:]

def _checkpoint_changed_paths(parent: str) -> list[str]:
    return [line for line in git("diff", "--name-only", parent, "HEAD").splitlines() if line]

def build_provenance(*, expected_commit: str | None = None, expected_tree: str | None = None,
                     expected_overlay_sha256: str | None = None) -> dict:
    if not OVERLAY.is_file():
        raise SystemExit("refusing to attest: .replit overlay missing")

    release_commit_sha = git("rev-parse", "HEAD")
    release_tree_sha = git("rev-parse", "HEAD^{tree}")
    overlay_sha256 = hashlib.sha256(OVERLAY.read_bytes()).hexdigest()
    changed_paths = _changed_paths()

    if expected_overlay_sha256 and overlay_sha256 != expected_overlay_sha256:
        raise SystemExit("refusing to attest: reviewed .replit overlay hash mismatch")

    if changed_paths == [ALLOWED_OVERLAY_PATH]:
        canonical_commit_sha = release_commit_sha
        canonical_tree_sha = release_tree_sha
        topology = DIRTY_OVERLAY_TOPOLOGY
    elif changed_paths == []:
        if not expected_commit or not expected_tree or not expected_overlay_sha256:
            raise SystemExit("refusing to attest: checkpoint topology requires exact expected canonical identity and overlay hash")
        parents = _checkpoint_parents()
        if len(parents) != 1:
            raise SystemExit("refusing to attest: checkpoint release must have exactly one parent")
        canonical_commit_sha = parents[0]
        if canonical_commit_sha != expected_commit:
            raise SystemExit("refusing to attest: checkpoint parent is not canonical commit")
        canonical_tree_sha = git("rev-parse", f"{canonical_commit_sha}^{{tree}}")
        if canonical_tree_sha != expected_tree:
            raise SystemExit("refusing to attest: canonical tree mismatch")
        if _checkpoint_changed_paths(canonical_commit_sha) != [ALLOWED_OVERLAY_PATH]:
            raise SystemExit("refusing to attest: checkpoint delta must be exactly the reviewed .replit overlay")
        topology = CHECKPOINT_TOPOLOGY
    else:
        raise SystemExit("refusing to attest: tracked differences must be exactly the reviewed .replit overlay or clean checkpoint")

    if expected_commit and canonical_commit_sha != expected_commit:
        raise SystemExit("refusing to attest: canonical commit mismatch")
    if expected_tree and canonical_tree_sha != expected_tree:
        raise SystemExit("refusing to attest: canonical tree mismatch")

    return {
        "schema_version": 3,
        "topology": topology,
        "canonical_commit_sha": canonical_commit_sha,
        "canonical_tree_sha": canonical_tree_sha,
        "release_commit_sha": release_commit_sha,
        "release_tree_sha": release_tree_sha,
        "release_overlay": {"path": ALLOWED_OVERLAY_PATH, "sha256": overlay_sha256},
    }

def main() -> None:
    payload = build_provenance(
        expected_commit=os.getenv("EXPECTED_CANONICAL_COMMIT"),
        expected_tree=os.getenv("EXPECTED_CANONICAL_TREE"),
        expected_overlay_sha256=os.getenv("EXPECTED_REPLIT_OVERLAY_SHA256"),
    )
    OUTPUT.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    verified = json.loads(OUTPUT.read_text(encoding="utf-8"))
    if verified != payload:
        raise SystemExit("refusing to attest: provenance verification failed")
    print("release provenance generated and verified")

if __name__ == "__main__":
    main()
