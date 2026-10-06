"""Strictly read-only five-Pin bounded-pilot preflight CLI."""
from __future__ import annotations

import json
import logging
import sys


def _refused(stage):
    return {
        "success": False,
        "mode": "READ_ONLY_FIVE_PIN_BOUNDED_PREFLIGHT",
        "terminal_stage": stage,
        "database_transactions": 0,
        "database_writes": 0,
        "object_storage_reads": 0,
        "object_storage_writes": 0,
        "provider_calls": 0,
        "provider_reads": 0,
        "provider_writes": 0,
        "buffer_calls": 0,
        "pinterest_calls": 0,
        "oauth_calls": 0,
        "ai_calls": 0,
        "automatic_retries": 0,
        "publication_creations": 0,
        "creative_creations": 0,
        "approval_creations": 0,
        "permit_creations": 0,
        "batch_creations": 0,
        "scheduler_activations": 0,
        "worker_activations": 0,
        "autonomy_activations": 0,
        "publishing_admission": "NOT_GRANTED",
        "bounded_preflight_certification": "NOT_GRANTED",
    }


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        if args:
            result = _refused("ARGUMENTS")
        else:
            from .bounded_pilot_preflight import run
            result = run()
    except Exception:
        result = _refused("UNEXPECTED")
    finally:
        logging.disable(previous)
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0 if result["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
