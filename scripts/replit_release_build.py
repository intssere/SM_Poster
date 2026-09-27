#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "backend" / ".build-provenance.json"
REQUIRED_EXPECTATIONS = {
    "EXPECTED_CANONICAL_COMMIT": 40,
    "EXPECTED_CANONICAL_TREE": 40,
    "EXPECTED_REPLIT_OVERLAY_SHA256": 64,
}

def main() -> None:
    for name, length in REQUIRED_EXPECTATIONS.items():
        if not re.fullmatch(rf"[0-9a-f]{{{length}}}", os.environ.get(name, "")):
            raise SystemExit(f"release preflight failed: independently pinned {name} required")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "write_build_provenance.py")], cwd=ROOT, check=True)
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version") != 3
        or payload.get("topology") != "canonical_parent_with_checkpoint_overlay"
        or payload.get("canonical_commit_sha") != os.environ["EXPECTED_CANONICAL_COMMIT"]
        or payload.get("canonical_tree_sha") != os.environ["EXPECTED_CANONICAL_TREE"]
        or not isinstance(payload.get("release_overlay"), dict)
        or payload["release_overlay"].get("path") != ".replit"
        or payload["release_overlay"].get("sha256") != os.environ["EXPECTED_REPLIT_OVERLAY_SHA256"]
    ):
        raise SystemExit("release preflight failed: certified schema-v3 checkpoint provenance missing")
    subprocess.run(["npm", "--prefix", "frontend", "run", "build"], cwd=ROOT, check=True)
    if not MANIFEST.is_file():
        raise SystemExit("release preflight failed: provenance missing after build")
    if json.loads(MANIFEST.read_text(encoding="utf-8")) != payload:
        raise SystemExit("release preflight failed: provenance changed during build")
    print("release preflight and build complete")

if __name__ == "__main__":
    main()
