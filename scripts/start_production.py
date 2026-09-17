#!/usr/bin/env python3
"""Fail-closed production process supervisor for Replit Autoscale.

The public frontend is not started until the backend health contract proves both
``status == "ok"`` and ``database_connected is True``. The wrapper uses only
Python's standard library so deployment startup does not depend on curl/jq or
other auxiliary binaries.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
READINESS_URL = "http://127.0.0.1:8000/api/health"
DEFAULT_READY_TIMEOUT_SECONDS = 180.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
LOG_PREFIX = "production-startup"


class StartupError(RuntimeError):
    """Raised when production startup cannot safely expose the frontend."""


class ShutdownRequested(BaseException):
    """Raised by SIGTERM/SIGINT so child processes can be cleaned up."""


def log_lifecycle_event(
    event: str,
    *,
    started_at: float,
    monotonic: Callable[[], float] | None = None,
) -> None:
    """Emit a deterministic startup event without sensitive runtime details."""

    now = (monotonic or time.monotonic)()
    elapsed_ms = max(0, int(round((now - started_at) * 1000)))
    print(f"{LOG_PREFIX} event={event} elapsed_ms={elapsed_ms}", flush=True)


def backend_command() -> list[str]:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]


def frontend_command() -> list[str]:
    return [
        "npm",
        "--prefix",
        "frontend",
        "run",
        "preview",
        "--",
        "--host",
        "0.0.0.0",
        "--port",
        os.getenv("PORT", "5000"),
    ]


def probe_backend_ready(
    *,
    opener: Callable[..., object] | None = None,
    url: str = READINESS_URL,
) -> bool:
    """Return true only for the strict backend/database readiness contract."""

    open_url = opener or urlopen
    try:
        with open_url(url, timeout=2.0) as response:  # type: ignore[attr-defined]
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("database_connected") is True
    )


def wait_for_backend_ready(
    process: subprocess.Popen[bytes] | subprocess.Popen[str] | object,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    probe: Callable[[], bool] = probe_backend_ready,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait for strict readiness or fail before the frontend can be exposed."""

    if timeout_seconds <= 0:
        raise StartupError("backend readiness timeout must be positive")
    if poll_interval_seconds <= 0:
        raise StartupError("backend readiness poll interval must be positive")

    deadline = monotonic() + timeout_seconds
    while True:
        return_code = process.poll()  # type: ignore[attr-defined]
        if return_code is not None:
            raise StartupError(
                f"backend exited before readiness with status {return_code}"
            )
        if probe():
            return
        if monotonic() >= deadline:
            raise StartupError(
                f"backend readiness timed out after {timeout_seconds:g} seconds"
            )
        sleep(poll_interval_seconds)


def start_backend() -> subprocess.Popen[bytes]:
    return subprocess.Popen(backend_command(), cwd=BACKEND_DIR)


def start_frontend() -> subprocess.Popen[bytes]:
    return subprocess.Popen(frontend_command(), cwd=ROOT)


def terminate_process(process: object | None) -> None:
    if process is None or process.poll() is not None:  # type: ignore[attr-defined]
        return
    process.terminate()  # type: ignore[attr-defined]
    try:
        process.wait(timeout=10)  # type: ignore[attr-defined]
    except subprocess.TimeoutExpired:
        process.kill()  # type: ignore[attr-defined]
        process.wait(timeout=5)  # type: ignore[attr-defined]


def supervise(
    backend: object,
    frontend: object,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Keep both production processes alive and fail if either exits."""

    while True:
        backend_code = backend.poll()  # type: ignore[attr-defined]
        if backend_code is not None:
            terminate_process(frontend)
            return backend_code if backend_code != 0 else 1

        frontend_code = frontend.poll()  # type: ignore[attr-defined]
        if frontend_code is not None:
            terminate_process(backend)
            return int(frontend_code)

        sleep(0.5)


def _timeout_from_environment() -> float:
    raw = os.getenv("BACKEND_READY_TIMEOUT_SECONDS", str(DEFAULT_READY_TIMEOUT_SECONDS))
    try:
        value = float(raw)
    except ValueError as exc:
        raise StartupError("BACKEND_READY_TIMEOUT_SECONDS must be numeric") from exc
    if value <= 0 or value > 900:
        raise StartupError("BACKEND_READY_TIMEOUT_SECONDS must be > 0 and <= 900")
    return value


def run() -> int:
    backend: object | None = None
    frontend: object | None = None
    previous_handlers: dict[int, object] = {}
    started_at = time.monotonic()

    def lifecycle(event: str) -> None:
        log_lifecycle_event(event, started_at=started_at)

    def request_shutdown(signum: int, _frame: object) -> None:
        raise ShutdownRequested(signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)

    try:
        lifecycle("wrapper_start")
        backend = start_backend()
        lifecycle("backend_process_started")
        wait_for_backend_ready(
            backend,
            timeout_seconds=_timeout_from_environment(),
        )
        lifecycle("backend_readiness_succeeded")
        # This is the only point at which the public frontend may start.
        frontend = start_frontend()
        lifecycle("frontend_process_started")
        lifecycle("supervision_entered")
        return supervise(backend, frontend)
    except StartupError as exc:
        print(f"production startup refused: {exc}", file=sys.stderr, flush=True)
        return 1
    except ShutdownRequested as exc:
        return 128 + int(exc.args[0])
    finally:
        terminate_process(frontend)
        terminate_process(backend)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
