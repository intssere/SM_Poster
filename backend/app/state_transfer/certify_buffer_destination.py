"""Strictly read-only Buffer destination certification CLI."""
from __future__ import annotations

import json
import logging
import sys


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        if args:
            result = {
                "success": False,
                "terminal_stage": "ARGUMENTS",
                "database_writes": 0,
                "provider_calls": 0,
                "provider_reads": 0,
                "provider_writes": 0,
                "automatic_retries": 0,
                "publishing_admission": "NOT_GRANTED",
                "buffer_destination_certification": "NOT_GRANTED",
            }
        else:
            from .buffer_destination_certification import run
            result = run()
    except Exception:
        result = {
            "success": False,
            "terminal_stage": "UNEXPECTED",
            "database_writes": 0,
            "provider_calls": 0,
            "provider_reads": 0,
            "provider_writes": 0,
            "automatic_retries": 0,
            "publishing_admission": "NOT_GRANTED",
            "buffer_destination_certification": "NOT_GRANTED",
        }
    finally:
        logging.disable(previous)
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0 if result["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
