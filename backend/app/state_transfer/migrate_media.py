"""Manual invocation only; each execute requires separate operator authorization."""
import json
import logging

from .migration_cli import SafeParser


def main(argv=None):
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        from .media_migration import run
        parser = SafeParser(description=__doc__)
        parser.add_argument("--database-env", required=True)
        parser.add_argument("--source-root", action="append", required=True)
        for name in ("endpoint", "bucket", "access-key", "secret-key", "region", "path-style"):
            parser.add_argument("--target-" + name + "-env", required=True)
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--execution-env")
        parser.add_argument("--dry-run", action="store_true")
        args = parser.parse_args(argv)
        result = run(database_env=args.database_env, roots=args.source_root,
                     target_envs={name: getattr(args, "target_" + name + "_env")
                                  for name in ("endpoint", "bucket", "access_key",
                                               "secret_key", "region", "path_style")},
                     execute=args.execute, dry_run=args.dry_run, execution_env=args.execution_env)
    except Exception:
        result = {"success": False, "terminal_stage": "ARGUMENTS",
                  "database_writes": 0, "target_put_attempts": 0}
    finally:
        logging.disable(previous)
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0 if result["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
