"""Manual/disposable-service command only; never an app startup hook."""
from __future__ import annotations

import argparse
import json
import logging


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error can echo a supplied DSN or arbitrary input.
        raise ValueError()


def main(argv=None):
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        from app.state_transfer.one_shot_migration import run_migration
        parser = SafeParser(description=__doc__)
        parser.add_argument("--source-env", required=True)
        parser.add_argument("--target-env", required=True)
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--execution-env")
        parser.add_argument("--statement-timeout-ms", type=int, default=480000)
        parser.add_argument("--lock-timeout-ms", type=int, default=10000)
        args = parser.parse_args(argv)
        result = run_migration(
            source_env=args.source_env, target_env=args.target_env,
            execute=args.execute, execution_env=args.execution_env,
            statement_timeout_ms=args.statement_timeout_ms,
            lock_timeout_ms=args.lock_timeout_ms,
        )
    except Exception:
        result = {"success": False, "diagnostic": {
            "stage": "ARGUMENTS", "exception_class": "ValueError"}}
    finally:
        logging.disable(previous_logging)
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0 if result["success"] else 2