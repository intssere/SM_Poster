#!/usr/bin/env python3
"""Explicit operator command, never a build/startup hook. Safe output only."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.state_transfer.catalog import Refused, canonical
from app.state_transfer.transfer import (
    certify_target, export_source, import_target, safe_plan, verify_bundle, write_bundle,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export", "plan", "import", "certify"))
    parser.add_argument("--dsn-file", type=Path,
                        help="Explicit operator-supplied DB URL file; no environment fallback")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    engine = None
    previous_logging_disable = logging.root.manager.disable
    try:
        # Suppress driver/SQL logging. Never echo DB URLs, parameters or exceptions.
        logging.disable(logging.CRITICAL)
        if args.command != "export":
            bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
            verify_bundle(bundle, args.expected_manifest_sha256)
        if args.command == "plan" and args.dsn_file is None:
            output = safe_plan(bundle)
        else:
            if args.dsn_file is None:
                raise Refused("Explicit --dsn-file required")
            import sqlalchemy as sa
            url = sa.engine.make_url(args.dsn_file.read_text().strip())
            if (url.get_backend_name() != "postgresql" or not url.database
                    or not url.username or not (url.host or url.query.get("host"))):
                raise Refused("Explicit PostgreSQL host, database and username required")
            engine = sa.create_engine(url,
                                      echo=False, hide_parameters=True)
            if args.command == "export":
                bundle = export_source(engine)
                if not args.dry_run:
                    write_bundle(bundle, args.bundle)
                output = safe_plan(bundle)
            elif args.command in {"import", "plan"}:
                output = import_target(engine, bundle, args.expected_manifest_sha256,
                                       plan=args.dry_run or args.command == "plan")
            else:
                output = certify_target(engine, bundle, args.expected_manifest_sha256)
        print(canonical(output))
        return 0
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except Exception:
        # Driver messages may contain offending rows/ciphertext: never stringify.
        print("REFUSED: database, bundle or filesystem operation failed", file=sys.stderr)
        return 2
    finally:
        logging.disable(previous_logging_disable)
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())