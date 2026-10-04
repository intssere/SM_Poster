#!/usr/bin/env python3
"""Explicit operator command, never a build/startup hook. Safe output only."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.state_transfer.catalog import Refused, canonical
from app.state_transfer.transfer import (
    certify_target, export_source, import_target, safe_plan, verify_bundle, write_bundle,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export", "plan", "import", "certify",
                                          "source-sql", "wrap-source-result",
                                          "capture-source-result"))
    parser.add_argument("--dsn-file", type=Path,
                        help="Explicit operator-supplied DB URL file; no environment fallback")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--expected-capsule-sha256")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--capsule-file", type=Path,
                        help="Private output for the direct-client capture command")
    parser.add_argument("--dsn-env",
                        help="Explicit secret name for direct capture only; no fallback")
    args = parser.parse_args(argv)
    engine = None
    previous_logging_disable = logging.root.manager.disable
    try:
        # Suppress driver/SQL logging. Never echo DB URLs, parameters or exceptions.
        logging.disable(logging.CRITICAL)
        if args.command == "capture-source-result":
            from app.state_transfer.capture_diagnostics import CaptureStage, safe_diagnostic
            from app.state_transfer.capture_runner import capture_source_result
            if (args.bundle is None or args.capsule_file is None
                    or bool(args.dsn_file) == bool(args.dsn_env)
                    or args.dry_run or args.expected_manifest_sha256
                    or args.expected_capsule_sha256):
                output = {"success": False,
                          "diagnostic": safe_diagnostic(ValueError(), CaptureStage.CONNECT)}
            else:
                def load_dsn():
                    if args.dsn_file:
                        return args.dsn_file.read_text(encoding="utf-8").strip()
                    return os.environ[args.dsn_env]
                output = capture_source_result(load_dsn=load_dsn,
                                               capsule_file=args.capsule_file,
                                               bundle_file=args.bundle)
            print(canonical(output))
            return 0 if output["success"] else 2
        if args.capsule_file or args.dsn_env:
            raise Refused("Direct capture options require capture-source-result")
        if args.command == "source-sql":
            if (args.dsn_file or args.bundle or args.dry_run or args.expected_manifest_sha256
                    or args.expected_capsule_sha256):
                raise Refused("source-sql accepts no database, bundle or dry-run option")
            from app.state_transfer.select_bridge import source_sql
            print(source_sql())
            return 0
        if args.bundle is None:
            raise Refused("Explicit --bundle required")
        if args.command == "wrap-source-result":
            if args.dsn_file or args.dry_run or args.expected_manifest_sha256:
                raise Refused("Offline wrapper accepts no database or dry-run option")
            from app.state_transfer.bridge_json import strict_json
            from app.state_transfer.select_bridge import wrap_source_result
            bundle = wrap_source_result(strict_json(sys.stdin.read()), args.expected_capsule_sha256)
            write_bundle(bundle, args.bundle)
            print(canonical({**safe_plan(bundle),
                             "source_capsule_sha256": bundle["manifest"]["source_capsule_sha256"]}))
            return 0
        if args.expected_capsule_sha256:
            raise Refused("Capsule fingerprint option requires offline wrapper")
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