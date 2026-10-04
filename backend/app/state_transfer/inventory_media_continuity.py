"""Manual, execution-gated local inventory/target-plan; never a startup hook."""
import json
import logging

from .migration_cli import SafeParser


def main(argv=None):
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        from .media_continuity import run_inventory
        parser = SafeParser(description=__doc__)
        parser.add_argument("--database-env", required=True)
        parser.add_argument("--source-root", action="append", required=True)
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--execution-env")
        parser.add_argument("--target-plan", action="store_true")
        args = parser.parse_args(argv)
        result = run_inventory(database_env=args.database_env, roots=args.source_root,
                               execute=args.execute, execution_env=args.execution_env,
                               plan=args.target_plan)
    except Exception:
        result = {"success": False, "complete": False,
                  "diagnostic": {"stage": "ARGUMENTS", "exception_class": "ValueError"}}
    finally:
        logging.disable(previous)
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0 if result["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())