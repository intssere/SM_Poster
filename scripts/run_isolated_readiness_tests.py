#!/usr/bin/env python3
"""Run offline backend tests with no inherited service credentials.

Only local sockets are permitted, even during collection. A child pytest process
is used so these fences cannot affect the running application.
"""
from __future__ import annotations

import ipaddress
from contextlib import contextmanager
import getpass
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _disposable_postgres(env):
    """Create an isolated Unix-socket-only cluster, never use an attached URL."""
    initdb = shutil.which("initdb", path=env["PATH"])
    pg_ctl = shutil.which("pg_ctl", path=env["PATH"])
    if not initdb or not pg_ctl:
        raise SystemExit("Disposable PostgreSQL binaries are required")
    with tempfile.TemporaryDirectory(prefix="readiness-ci-postgres-") as folder:
        root = Path(folder)
        data, socket_dir = root / "data", root / "socket"
        socket_dir.mkdir(mode=0o700)
        subprocess.run([
            initdb, "-D", str(data), "-U", getpass.getuser(),
            "--auth-local=trust", "--auth-host=reject", "--no-instructions",
        ], env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started = False
        try:
            subprocess.run([
                pg_ctl, "-D", str(data), "-l", str(root / "postgres.log"),
                "-o", f"-c listen_addresses='' -c unix_socket_directories={socket_dir} "
                "-c port=55492 -c unix_socket_permissions=0700", "-w", "start",
            ], env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            started = True
            url = (f"postgresql+psycopg://{quote(getpass.getuser())}@/postgres"
                   f"?host={quote(str(socket_dir), safe='')}&port=55492")
            yield url
        finally:
            if started:
                subprocess.run([
                    pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop",
                ], env=env, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _pytest_main(args: list[str]) -> int:
    def is_local(host):
        if host is None or host == "localhost":
            return True
        if isinstance(host, bytes):
            host = host.decode("ascii")
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    resolve, connect, connect_ex = (
        socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex,
    )

    def local_resolve(host, *args, **kwargs):
        if not is_local(host):
            raise AssertionError("External DNS forbidden before collection")
        return resolve(host, *args, **kwargs)

    def local_connect(sock, address):
        if isinstance(address, tuple) and not is_local(address[0]):
            raise AssertionError("External network forbidden before collection")
        return connect(sock, address)

    def local_connect_ex(sock, address):
        if isinstance(address, tuple) and not is_local(address[0]):
            raise AssertionError("External network forbidden before collection")
        return connect_ex(sock, address)

    socket.getaddrinfo = local_resolve
    socket.socket.connect = local_connect
    socket.socket.connect_ex = local_connect_ex
    import replit.object_storage as sdk

    def forbidden_client(*args, **kwargs):
        raise AssertionError("Live Object Storage forbidden before collection")

    sdk.Client = forbidden_client
    import pytest
    return pytest.main(args)


def main(args: list[str]) -> int:
    if args[:1] == ["--isolated-child"]:
        return _pytest_main(args[1:])
    if any((ROOT / path).exists() for path in (".env", "backend/.env")):
        raise SystemExit("Isolation refused: dotenv configuration is present")
    # Never enumerate, print or read credentials; only copy this allowlist.
    env = {name: os.environ[name] for name in ("PATH", "HOME", "USER", "LANG")
           if name in os.environ}
    env.update({
        "PYTHONPATH": str(ROOT / "backend"),
        "DATABASE_URL": "sqlite:///:memory:",
        "APP_ENV": "test", "AUTH_DISABLED": "false",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "OBJECT_STORAGE_READINESS_MANAGEMENT_ENABLED": "false",
        "OBJECT_STORAGE_READINESS_PROBE_ENABLED": "false",
    })
    postgres = "--with-postgres" in args
    args = [arg for arg in args if arg != "--with-postgres"]

    def run():
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--isolated-child",
             "-p", "pytest_asyncio.plugin", *args],
            cwd=ROOT / "backend", env=env, check=False,
        ).returncode

    if not postgres:
        return run()
    with _disposable_postgres(env) as url:
        for key in (
            "TASK61_DISPOSABLE_POSTGRES_URL", "TASK58_POSTGRES_URL", "TASK58_CATALOG_URL",
            "TASK60_POSTGRES_URL", "TASK595_POSTGRES_URL", "TASK46_POSTGRES_URL",
        ):
            env[key] = url
        # Existing adoption regressions use this dedicated source catalog as
        # well as creating fresh per-test databases. Certify only this newly
        # created disposable cluster; never seed a caller/attached endpoint.
        seed_env = {**env, "DATABASE_URL": url}
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT / "backend", env=seed_env, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))