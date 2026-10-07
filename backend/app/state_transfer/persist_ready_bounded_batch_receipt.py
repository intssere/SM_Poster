"""Persist exactly one sanitized READY-batch receipt after fresh certification."""
from __future__ import annotations

import json
import logging
import sys


def _base():
    return {
        "success": False,
        "mode": "DURABLE_FIVE_PIN_READY_BATCH_RECEIPT",
        "terminal_stage": "CERTIFICATION",
        "code": None,
        "database_writes": 0,
        "provider_calls": 0,
        "buffer_calls": 0,
        "pinterest_calls": 0,
        "oauth_calls": 0,
        "ai_calls": 0,
        "publishing_admission": "NOT_GRANTED",
        "receipt_id": None,
        "ready_batch_fingerprint": None,
        "receipt_sha256": None,
        "created": False,
    }


def run(*, certification_runner=None, session_factory=None):
    result = _base()
    db = None
    try:
        if certification_runner is None:
            from app.state_transfer.ready_bounded_batch_certification import run as certification_runner
        certified = certification_runner()

        from app.services.ready_bounded_batch_receipt import persist_receipt
        if session_factory is None:
            from app.db.session import SessionLocal
            session_factory = SessionLocal

        result["terminal_stage"] = "RECEIPT_WRITE"
        db = session_factory()
        stored = persist_receipt(db, certified)
        receipt = stored["receipt"]
        result.update({
            "success": True,
            "terminal_stage": "COMPLETE",
            "code": "PASS",
            "database_writes": 1 if stored["created"] else 0,
            "receipt_id": stored["receipt_id"],
            "ready_batch_fingerprint": receipt["ready_batch_fingerprint"],
            "receipt_sha256": receipt["receipt_sha256"],
            "created": bool(stored["created"]),
        })
    except Exception as exc:
        code = getattr(exc, "code", None)
        result["code"] = code if isinstance(code, str) else "READY_RECEIPT_UNEXPECTED_ERROR"
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                result.update(
                    success=False,
                    terminal_stage="DATABASE_CLOSE",
                    code="READY_RECEIPT_DATABASE_CLOSE_FAILED",
                )
    return result


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        if args:
            result = _base()
            result.update(terminal_stage="ARGUMENTS", code="ARGUMENTS_PROHIBITED")
        else:
            result = run()
    except Exception:
        result = _base()
        result.update(terminal_stage="UNEXPECTED", code="READY_RECEIPT_UNEXPECTED_ERROR")
    finally:
        logging.disable(previous)
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0 if result["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
