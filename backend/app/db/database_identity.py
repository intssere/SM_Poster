"""Credential-free PostgreSQL identities; no settings or engine construction."""
import hashlib
import json
import sqlalchemy as sa
from sqlalchemy.engine import make_url


def normalize_postgresql_url(database_url: str) -> str:
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    return database_url


def database_identity_sha256(database_url: str) -> str:
    """Preserve the existing bookkeeping reconciler's identity algorithm."""
    url = make_url(normalize_postgresql_url(database_url))
    host = (url.host or "").lower()
    database = (url.database or "").strip()
    if url.get_backend_name() != "postgresql" or not host or not database:
        raise ValueError("target database identity is not a concrete PostgreSQL database")
    identity = f"postgresql://{host}:{url.port or 5432}/{database}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def connected_database_identity_sha256(connection) -> str:
    """Bind routing to the actual backend, not merely engine URL metadata.

    A trusted host must attest this fingerprint independently of the request.
    Missing metadata permission is a refusal, never a URL-only fallback. The
    physical identity is intentionally conservative across server changes:
    an approved job needs a fresh attestation after failover/reprovisioning.
    """
    row = connection.execute(sa.text(
        "SELECT current_database(), "
        "(SELECT oid FROM pg_database WHERE datname=current_database()), "
        "inet_server_addr()::text, inet_server_port(), "
        "(SELECT system_identifier::text FROM pg_control_system())"
    )).one()
    if not row[0] or not row[1] or not row[4]:
        raise ValueError("connected database identity is incomplete")
    return hashlib.sha256(json.dumps(list(row), separators=(",", ":")).encode()).hexdigest()