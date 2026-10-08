"""Restart-safe Railway runtime wrapper for one guarded bounded preparation."""
from __future__ import annotations

import json
import os

import uvicorn

from app.state_transfer.prepare_bounded_pilot import (
    INVOCATION_ENV,
    _base_result,
    run as prepare_run,
)


def main(*, preparation_runner=None, server_runner=None) -> int:
    preparation_runner = preparation_runner or prepare_run
    server_runner = server_runner or uvicorn.run
    try:
        result = preparation_runner(
            invocation_id=os.environ.get(INVOCATION_ENV),
            require_invocation_guard=True,
        )
    except Exception:
        result = _base_result()
        result.update(
            terminal_stage="RUNTIME_WRAPPER",
            code="BOUNDED_PREPARATION_UNEXPECTED_ERROR",
        )

    print(json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ), flush=True)

    # Always become the normal long-lived API process. A Railway container
    # restart re-enters the durable invocation guard before this point and can
    # never repeat the authorized mutation.
    server_runner("app.main:app", host="0.0.0.0", port=8000)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
