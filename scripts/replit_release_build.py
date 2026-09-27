#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "backend" / ".build-provenance.json"
OVERLAY = ROOT / ".replit"
REQUIRED_EXPECTATIONS = {
    "EXPECTED_CANONICAL_COMMIT": 40,
    "EXPECTED_CANONICAL_TREE": 40,
}


def _overlay_sha256() -> str:
    if not OVERLAY.is_file():
        raise SystemExit("release preflight failed: .replit overlay missing")
    return hashlib.sha256(OVERLAY.read_bytes()).hexdigest()


def _validate_manifest(payload: dict, expected: dict[str, str], overlay_sha256: str) -> None:
    if (
        payload.get("schema_version") != 3
        or payload.get("topology") != "canonical_parent_with_checkpoint_overlay"
        or payload.get("canonical_commit_sha") != expected["EXPECTED_CANONICAL_COMMIT"]
        or payload.get("canonical_tree_sha") != expected["EXPECTED_CANONICAL_TREE"]
        or not isinstance(payload.get("release_overlay"), dict)
        or payload["release_overlay"].get("path") != ".replit"
        or payload["release_overlay"].get("sha256") != overlay_sha256
    ):
        raise SystemExit("release preflight failed: certified schema-v3 checkpoint provenance missing")


def main() -> None:
    expected: dict[str, str] = {}
    for name, length in REQUIRED_EXPECTATIONS.items():
        value = os.environ.get(name, "")
        if not re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
            raise SystemExit(f"release preflight failed: independently pinned {name} required")
        expected[name] = value

    overlay_sha256 = _overlay_sha256()
    subprocess.run([sys.executable, str(ROOT / "scripts" / "write_build_provenance.py")], cwd=ROOT, check=True)
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    _validate_manifest(payload, expected, overlay_sha256)

    subprocess.run(["npm", "--prefix", "frontend", "run", "build"], cwd=ROOT, check=True)

    if not MANIFEST.is_file():
        raise SystemExit("release preflight failed: provenance missing after build")
    if _overlay_sha256() != overlay_sha256:
        raise SystemExit("release preflight failed: .replit overlay changed during build")
    if json.loads(MANIFEST.read_text(encoding="utf-8")) != payload:
        raise SystemExit("release preflight failed: provenance changed during build")
    _validate_manifest(payload, expected, overlay_sha256)
    print("release preflight and build complete")


if __name__ == "__main__":
    main()
