#!/usr/bin/env python3
"""Offline CI-only runtime/import proof; never construct an SDK client."""
from __future__ import annotations

import os
import subprocess
import sys


IMPORT_CHECK = r"""
import importlib.metadata
import platform
import sys

def deny_network(event, args):
    if event.startswith("socket.") or event in {"urllib.Request", "http.client.connect"}:
        raise RuntimeError("Network forbidden during SDK import certification")

sys.addaudithook(deny_network)
assert sys.version_info[:2] == (3, 12), "Certification requires Python 3.12"
from packaging.specifiers import SpecifierSet
distribution = importlib.metadata.distribution("replit-object-storage")
requirement = distribution.metadata["Requires-Python"]
assert requirement and SpecifierSet(requirement).contains(platform.python_version())
from replit.object_storage import Client
from replit.object_storage.errors import ObjectNotFoundError
assert isinstance(Client, type) and Client.__module__ == "replit.object_storage.client"
assert issubclass(ObjectNotFoundError, Exception)
print("SDK_IMPORT_PASS python=" + platform.python_version()
      + " sdk=" + distribution.version + " requires-python=" + requirement
      + " network=denied client-construction=not-invoked")
"""


def main() -> int:
    # Do not inherit credentials, deployment markers, or operational gates.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "USER", "LANG") if key in os.environ}
    version = subprocess.run(
        ["postgres", "--version"], env=env, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    assert version.startswith("postgres (PostgreSQL) 16."), "Certification requires PostgreSQL 16"
    print("POSTGRES_VERSION_PASS " + version, flush=True)
    subprocess.run(
        [sys.executable, "-I", "-B", "-c", IMPORT_CHECK],
        env=env, check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())