#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "backend" / ".build-provenance.json"


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, stderr=subprocess.STDOUT
    ).strip()


def main() -> None:
    # Refuse to attest a checkout with tracked changes. The generated manifest
    # itself is ignored and therefore does not weaken this check.
    if git("status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("refusing to attest a checkout with tracked changes")
    commit_sha = git("rev-parse", "HEAD")
    tree_sha = git("rev-parse", "HEAD^{tree}")
    payload = {
        "schema_version": 1,
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
    }
    OUTPUT.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUTPUT.relative_to(ROOT)} for {commit_sha}")


if __name__ == "__main__":
    main()
