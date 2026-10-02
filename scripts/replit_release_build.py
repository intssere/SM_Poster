#!/usr/bin/env python3
"""Exact source guard before/after compilation, followed by a fresh receipt."""
from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
from release_source_guard import (SourceGuardError, artifact_inventory, expectations,
                                  parent_fd, require, source_witness, verify_source)
from write_build_provenance import (discard_receipt, receipt_absent, verify_receipt,
                                    write_receipt)
from release_source_watch import SourceWatch

ROOT = Path(__file__).resolve().parents[1]


def clear_compiler_outputs(root: Path) -> None:
    """Require fresh outputs, not artifacts/TS incremental state from an old build."""
    parent, name = parent_fd(root, "frontend/dist")
    try:
        output = root / "frontend/dist"
        if os.path.lexists(output):
            require(output.is_dir() and not output.is_symlink(), "unsafe compiled output root")
            require(shutil.rmtree.avoids_symlink_attacks, "safe output cleanup unavailable")
            shutil.rmtree(name, dir_fd=parent)
        info = root / "frontend/tsconfig.tsbuildinfo"
        if os.path.lexists(info):
            require(info.is_file() and not info.is_symlink(), "unsafe compiler metadata")
            os.unlink("tsconfig.tsbuildinfo", dir_fd=parent)
    finally:
        os.close(parent)


def run_build(root: Path, runner=subprocess.run) -> dict:
    # A failed attempt must never leave an old receipt looking like its result.
    discard_receipt(root)
    try:
        pins = expectations()
        source = verify_source(root, pins)
        require(receipt_absent(root), "unexpected/tampered provenance before compilation")
        clear_compiler_outputs(root)
        with SourceWatch(root) as watch:
            require(verify_source(root, pins) == source, "source changed before compilation")
            witness = source_witness(root, pins)
            watch.check()
            runner(["npm", "--prefix", "frontend", "run", "build"], cwd=root, check=True)
            require(receipt_absent(root), "unexpected/tampered provenance during compilation")
            require(verify_source(root, pins) == source, "source inventory changed during build")
            require(source_witness(root, pins) == witness, "source/Git changed during build")
            artifacts = artifact_inventory(root)
            watch.check()
            payload = {**source, "artifacts": artifacts}
            write_receipt(root, payload)
            require(verify_source(root, pins) == source, "source changed during receipt publication")
            require(source_witness(root, pins) == witness, "source/Git changed during receipt publication")
            require(artifact_inventory(root) == artifacts, "artifacts changed during receipt publication")
            verify_receipt(root, payload)
            watch.check()
            return payload
    except BaseException:
        discard_receipt(root)
        raise


def main() -> None:
    try:
        run_build(ROOT)
    except (SourceGuardError, OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"release source guard failed: {error}") from error
    print("exact release source verified; compiled artifacts and fresh schema-v4 receipt verified")


if __name__ == "__main__":
    main()