#!/usr/bin/env python3
"""Manual/disposable-service command only; never an app startup hook."""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.state_transfer.migration_cli import SafeParser, main


if __name__ == "__main__":
    raise SystemExit(main())