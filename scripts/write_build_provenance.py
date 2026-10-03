#!/usr/bin/env python3
"""Fresh, atomic schema-v4 receipts; never certify a stale manifest."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
from release_source_guard import (RECEIPT, SourceGuardError, expectations,
                                  parent_fd, physical_entry, require, verify_source)

ROOT = Path(__file__).resolve().parents[1]


def build_provenance(*, expected_commit=None, expected_tree=None,
                     expected_release_commit=None, expected_release_tree=None,
                     expected_overlay_sha256=None, root=ROOT) -> dict:
    pins = expectations({
        "EXPECTED_CANONICAL_COMMIT": expected_commit,
        "EXPECTED_CANONICAL_TREE": expected_tree,
        "EXPECTED_RELEASE_COMMIT": expected_release_commit,
        "EXPECTED_RELEASE_TREE": expected_release_tree,
        "EXPECTED_REPLIT_OVERLAY_SHA256": expected_overlay_sha256,
    })
    return verify_source(root, pins)


def discard_receipt(root: Path) -> None:
    parent, name = parent_fd(root, RECEIPT)
    try:
        for leaf in (name, name + ".tmp"):
            try:
                os.unlink(leaf, dir_fd=parent)
            except FileNotFoundError:
                pass
    finally:
        os.close(parent)


def receipt_absent(root: Path) -> bool:
    return not os.path.lexists(root / RECEIPT) and not os.path.lexists(root / (RECEIPT + ".tmp"))


def receipt_bytes(payload: dict) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def write_receipt(root: Path, payload: dict) -> None:
    require(receipt_absent(root), "unexpected/tampered provenance before receipt publication")
    parent, name = parent_fd(root, RECEIPT)
    try:
        fd = os.open(name + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(receipt_bytes(payload))
            stream.flush()
            os.fsync(stream.fileno())
        require(not os.path.lexists(root / RECEIPT), "provenance appeared during publication")
        os.replace(name + ".tmp", name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)
    verify_receipt(root, payload)


def verify_receipt(root: Path, payload: dict) -> None:
    mode, data = physical_entry(root, RECEIPT)
    require(mode == "100644" and data == receipt_bytes(payload),
            "fresh provenance receipt mismatch")


def main() -> None:
    # Source-only attestation is intentionally insufficient for a build receipt.
    raise SystemExit("Use scripts/replit_release_build.py; receipts require verified compilation")


if __name__ == "__main__":
    main()