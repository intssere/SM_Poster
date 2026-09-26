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

def build_provenance(*, expected_commit: str | None = None, expected_tree: str | None = None,
                     expected_overlay_sha256: str | None = None) -> dict:
    if _changed_paths() != [ALLOWED_OVERLAY_PATH]:
        raise SystemExit("refusing to attest: tracked differences must be exactly the reviewed .replit overlay")
    if not OVERLAY.is_file():
        raise SystemExit("refusing to attest: .replit overlay missing")
    commit_sha = git("rev-parse", "HEAD")
    tree_sha = git("rev-parse", "HEAD^{tree}")
    overlay_sha256 = hashlib.sha256(OVERLAY.read_bytes()).hexdigest()
    if expected_commit and commit_sha != expected_commit:
        raise SystemExit("refusing to attest: canonical commit mismatch")
    if expected_tree and tree_sha != expected_tree:
        raise SystemExit("refusing to attest: canonical tree mismatch")
    if expected_overlay_sha256 and overlay_sha256 != expected_overlay_sha256:
        raise SystemExit("refusing to attest: reviewed .replit overlay hash mismatch")
    return {
        "schema_version": 2,
        "canonical_commit_sha": commit_sha,
        "canonical_tree_sha": tree_sha,
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
