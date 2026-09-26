#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "backend" / ".build-provenance.json"

def main() -> None:
    subprocess.run([sys.executable, str(ROOT / "scripts" / "write_build_provenance.py")], cwd=ROOT, check=True)
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 2:
        raise SystemExit("release preflight failed: schema-v2 provenance missing")
    subprocess.run(["npm", "--prefix", "frontend", "run", "build"], cwd=ROOT, check=True)
    if not MANIFEST.is_file():
        raise SystemExit("release preflight failed: provenance missing after build")
    if json.loads(MANIFEST.read_text(encoding="utf-8")) != payload:
        raise SystemExit("release preflight failed: provenance changed during build")
    print("release preflight and build complete")

if __name__ == "__main__":
    main()
