#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "backend" / ".build-provenance.json"
OVERLAY = ROOT / ".replit"
ALLOWED_OVERLAY_PATH = ".replit"


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, stderr=subprocess.STDOUT
    ).strip()


def main() -> None:
    status = git("status", "--porcelain", "--untracked-files=no").splitlines()
    changed = []
    for line in status:
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changed.append(path)
    if changed != [ALLOWED_OVERLAY_PATH]:
        raise SystemExit(
            "refusing to attest: tracked differences must be exactly the reviewed .replit overlay"
        )
    if not OVERLAY.is_file():
        raise SystemExit("refusing to attest: .replit overlay missing")

    commit_sha = git("rev-parse", "HEAD")
    tree_sha = git("rev-parse", "HEAD^{tree}")
    overlay_sha256 = hashlib.sha256(OVERLAY.read_bytes()).hexdigest()
    payload = {
        "schema_version": 2,
        "canonical_commit_sha": commit_sha,
        "canonical_tree_sha": tree_sha,
        "release_overlay": {
            "path": ALLOWED_OVERLAY_PATH,
            "sha256": overlay_sha256,
        },
    }
    OUTPUT.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(
        f"wrote {OUTPUT.relative_to(ROOT)} for canonical {commit_sha} "
        f"with reviewed {ALLOWED_OVERLAY_PATH} overlay {overlay_sha256}"
    )


if __name__ == "__main__":
    main()
