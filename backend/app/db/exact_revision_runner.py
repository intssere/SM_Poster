"""Unwired library for an approved platform job, never an app/startup hook.

The trusted host supplies a caller-owned connection/root transaction and a
policy from outside the request. Ed25519 authorization binds the job to the
approved artifact and production endpoint. Supplying an attacker-selected key
or target policy is NOT a trust boundary. No supported Replit platform adapter
is implemented here; this module does not connect, launch, or contact providers.
"""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import math
import time

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import sqlalchemy as sa

from app.db.database_identity import (
    database_identity_sha256, connected_database_identity_sha256,
)
from app.db.migration_adoption import (
    LINEAGE_PRESERVED_TABLES, LINEAGE_RUN_TABLES,
    LINEAGE_RECONCILIATION_TABLE, SCHEDULED_QUOTA_RESERVATIONS_TABLE,
    verify_frozen_schema_at_head,
)
from app.db.migration_lock import acquire_transaction_lock, clear_lock_proof

BACKEND = Path(__file__).resolve().parents[2]
MIGRATION_HASH = "fac94a530f1ed98e8e862baa84fdacf31505c0f9aa29186898148f25479fbd14"
_ALEMBIC_PROOF = object()
DISABLED_GATES = (
    "PUBLISHING_ENABLED", "BUFFER_PUBLISHING_ENABLED",
    "BUFFER_SINGLE_PIN_PILOT_ENABLED", "PINTEREST_SINGLE_PIN_PILOT_ENABLED",
    "PINTEREST_WRITE_SCOPE_ENABLED", "PINTEREST_BOARD_WRITE_SCOPE_ENABLED",
    "OBJECT_STORAGE_READINESS_MANAGEMENT_ENABLED",
    "OBJECT_STORAGE_READINESS_PROBE_ENABLED", "ROUTINE_SCHEDULER_CANARY_ENABLED",
    "ROUTINE_PINTEREST_SCHEDULER_ENABLED", "ROUTINE_PINTEREST_WORKER_ENABLED",
    "ROUTINE_BUFFER_DISPATCH_ENABLED", "ROUTINE_SCHEDULED_LIVE_ADMISSION_ENABLED",
    "ROUTINE_SCHEDULED_AUTONOMY_ENABLED", "ROUTINE_AUTONOMOUS_AUTHORIZATION_ENABLED",
    "PINTEREST_PORTFOLIO_PLANNER_ENABLED", "PINTEREST_PORTFOLIO_ACTIVATION_ENABLED",
    "PINTEREST_OPTIMIZER_ENABLED", "PINTEREST_OPTIMIZER_APPLY_ENABLED",
    "PINTEREST_AUTONOMOUS_GENERATION_ENABLED", "PINTEREST_AUTONOMOUS_EXECUTION_ENABLED",
    "PINTEREST_AUTONOMOUS_BOARD_ENSURE_ENABLED", "PINTEREST_BOARD_PROVISIONING_ENABLED",
    "PINTEREST_SEO_BRIEF_PERSISTENCE_ENABLED", "PINTEREST_ANALYTICS_INGESTION_ENABLED",
    "PINTEREST_LEARNING_SNAPSHOT_PERSISTENCE_ENABLED", "AUTH_DISABLED",
)
CONSTRAINTS = (
    "pk_management_readiness_admissions", "uq_management_readiness_admissions_grant",
    "ck_management_readiness_admissions_operation",
    "ck_management_readiness_admissions_descriptor_hash",
    "ck_management_readiness_admissions_actor_hash",
    "ck_management_readiness_admissions_outcome",
    "ck_management_readiness_admissions_outcome_evidence",
)
FUNCTIONS = (
    "management_readiness_admission_guard",
    "management_readiness_admission_truncate_guard",
)
TRIGGERS = (
    "management_readiness_admissions_immutable",
    "management_readiness_admissions_no_truncate",
)


class MigrationRefused(RuntimeError):
    """Sanitized fail-closed result; never include database/credential errors."""


class CommitOutcomeUnknown(MigrationRefused):
    """Do not retry: inspect durable state under separately authorized access."""


@dataclass(frozen=True)
class InvocationPolicy:
    """Host-owned trust anchors, never populated from an untrusted request."""
    public_key: bytes
    app_id: str
    artifact_sha256: str
    production_identity: str
    development_identity: str
    job_id: str
    production_server_identity: str
    development_server_identity: str


@dataclass(frozen=True)
class MigrationResult:
    status: str
    revision: str = "0032"


def _authorize(payload, signature, policy, closed_state):
    try:
        Ed25519PublicKey.from_public_bytes(policy.public_key).verify(signature, payload)
        claims = json.loads(payload)
        if json.dumps(claims, sort_keys=True, separators=(",", ":")).encode() != payload:
            raise ValueError
        expected = {
            "operation": "alembic_0031_to_0032",
            "scope": "production",
            "invocation": "platform_migration_job",
            "app_id": policy.app_id,
            "artifact_sha256": policy.artifact_sha256,
            "database_identity": policy.production_identity,
            "server_identity": policy.production_server_identity,
            "job_id": policy.job_id,
        }
        if set(claims) != {*expected, "issued_at", "expires_at"}:
            raise ValueError
        if any(claims[k] != v or not isinstance(v, str) or not v for k, v in expected.items()):
            raise ValueError
        for digest in (
            policy.artifact_sha256, policy.production_identity, policy.development_identity,
            policy.production_server_identity, policy.development_server_identity,
        ):
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError
        issued, expires = claims["issued_at"], claims["expires_at"]
        if type(issued) not in (int, float) or type(expires) not in (int, float):
            raise ValueError
        if not math.isfinite(issued) or not math.isfinite(expires):
            raise ValueError
        now = time.time()
        if not issued <= now < expires or not 0 < expires - issued <= 300:
            raise ValueError
        if policy.production_identity == policy.development_identity:
            raise ValueError
        if policy.production_server_identity == policy.development_server_identity:
            raise ValueError
        if any(closed_state.get(k) != "false" for k in DISABLED_GATES):
            raise ValueError
        if (
            closed_state.get("AI_PROVIDER") != "none"
            or closed_state.get("ROUTINE_PINTEREST_DRY_RUN") != "true"
            or closed_state.get("APP_ENV") != "production"
        ):
            raise ValueError
    except (InvalidSignature, ValueError, TypeError, KeyError, AttributeError):
        raise MigrationRefused("untrusted invocation or non-closed execution context") from None


def _require_absent(connection):
    relations = ["management_readiness_admissions", *CONSTRAINTS[:2]]
    queries = (
        ("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
         "WHERE n.nspname='public' AND c.relname=ANY(:names)", relations),
        ("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
         "WHERE n.nspname='public' AND p.proname=ANY(:names)", list(FUNCTIONS)),
        ("SELECT count(*) FROM pg_trigger WHERE tgname=ANY(:names)", list(TRIGGERS)),
        ("SELECT count(*) FROM pg_constraint WHERE conname=ANY(:names)", list(CONSTRAINTS)),
    )
    if any(connection.scalar(sa.text(sql), {"names": names}) for sql, names in queries):
        raise MigrationRefused("pre-existing 0032 target objects")


def _paused(connection):
    rows = connection.execute(sa.text(
        "SELECT id,state FROM public.routine_publishing_control ORDER BY id"
    )).all()
    if [tuple(r) for r in rows] != [("default", "PAUSED")]:
        raise MigrationRefused("exact PAUSED control required")


def _lock_frozen_tables(connection, revision):
    # Stabilize the certified catalogs and closed-state rows through commit.
    # Fixed canonical names only; NOWAIT refuses concurrent writers/DDL.
    names = {
        "alembic_version", *LINEAGE_PRESERVED_TABLES, *LINEAGE_RUN_TABLES,
        LINEAGE_RECONCILIATION_TABLE, SCHEDULED_QUOTA_RESERVATIONS_TABLE,
        "routine_publishing_control",
    }
    if revision == "0032":
        names.add("management_readiness_admissions")
    for name in sorted(names):
        connection.exec_driver_sql(f'LOCK TABLE public."{name}" IN SHARE MODE NOWAIT')


def _certify(connection):
    verify_frozen_schema_at_head(connection, revision="0032")
    if connection.scalar(sa.text(
        "SELECT count(*) FROM public.management_readiness_admissions"
    )):
        raise MigrationRefused("admission evidence exists")
    _paused(connection)


def _alembic_config(connection):
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.attributes["connection"] = connection
    cfg.attributes["exact_revision"] = "0032"
    return cfg


def _require_authorized_alembic_config(cfg, connection):
    """Only a config armed after signed authorization and frozen preflight."""
    if (
        cfg.attributes.get("_exact_0032_proof") is not _ALEMBIC_PROOF
        or cfg.attributes.get("_exact_0032_transaction") is not connection.get_transaction()
    ):
        raise RuntimeError("authorized exact runner context required")


def run_exact_0032(
    connection, transaction, *, payload: bytes, signature: bytes,
    policy: InvocationPolicy, closed_state, lock_timeout: float = 5.0,
) -> MigrationResult:
    """Certify and commit the supplied root transaction, or roll it back.

    A transaction must be freshly begun on the caller-owned connection. The
    caller must not reuse it for unrelated writes. The host verifies the running
    artifact against policy.artifact_sha256 before invoking this library.
    No URL overrides, CLI, target revision, or implicit production connection.
    """
    if not getattr(transaction, "is_active", False) or connection.get_transaction() is not transaction:
        raise MigrationRefused("active caller-owned root transaction required")
    try:
        _authorize(payload, signature, policy, closed_state)
        if connection.dialect.name != "postgresql" or connection.get_isolation_level() != "READ COMMITTED":
            raise MigrationRefused("PostgreSQL READ COMMITTED required")
        url = connection.engine.url
        if database_identity_sha256(url.set(username=None, password=None).render_as_string()) != policy.production_identity:
            raise MigrationRefused("target identity mismatch or development target")
        connection.exec_driver_sql("SET LOCAL search_path TO public, pg_catalog")
        connection.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
        connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
        if connection.scalar(sa.text("SELECT current_database()")) != url.database:
            raise MigrationRefused("connected database identity mismatch")
        if connected_database_identity_sha256(connection) != policy.production_server_identity:
            raise MigrationRefused("connected server identity mismatch or development target")
        acquire_transaction_lock(connection, lock_timeout)
        _authorize(payload, signature, policy, closed_state)
        # The advisory lock serializes cooperative migration callers. Relation
        # locks additionally fence non-cooperative writes/DDL through commit.
        connection.exec_driver_sql("LOCK TABLE public.alembic_version IN SHARE ROW EXCLUSIVE MODE NOWAIT")
        revisions = connection.execute(sa.text(
            "SELECT version_num FROM public.alembic_version ORDER BY version_num FOR UPDATE"
        )).scalars().all()
        if revisions == ["0032"]:
            _lock_frozen_tables(connection, "0032")
            _certify(connection)
            status = "verified_noop"
        elif revisions == ["0031"]:
            _lock_frozen_tables(connection, "0031")
            verify_frozen_schema_at_head(connection, revision="0031")
            _paused(connection)
            _require_absent(connection)
            cfg = _alembic_config(connection)
            script = ScriptDirectory.from_config(cfg)
            pending = list(script.iterate_revisions("0032", "0031"))
            if len(pending) != 1 or pending[0].revision != "0032" or pending[0].down_revision != "0031":
                raise MigrationRefused("migration graph is not the exact approved transition")
            migration = Path(pending[0].path)
            if hashlib.sha256(migration.read_bytes()).hexdigest() != MIGRATION_HASH:
                raise MigrationRefused("canonical 0032 migration changed")
            cfg.attributes["_exact_0032_proof"] = _ALEMBIC_PROOF
            cfg.attributes["_exact_0032_transaction"] = transaction
            command.upgrade(cfg, "0032")
            _certify(connection)
            status = "migrated"
        else:
            raise MigrationRefused("exact revision 0031 or certified 0032 required")
        _authorize(payload, signature, policy, closed_state)
    except BaseException as exc:
        transaction.rollback()
        clear_lock_proof(connection)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, MigrationRefused):
            raise
        raise MigrationRefused("migration or pre-commit certification refused") from None
    try:
        transaction.commit()
    except BaseException:
        raise CommitOutcomeUnknown("commit outcome unknown; stop without retry or downgrade") from None
    finally:
        clear_lock_proof(connection)
    return MigrationResult(status)