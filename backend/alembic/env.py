from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from app.db.base import Base
from app.core.config import get_settings
from app.db.database_identity import normalize_postgresql_url as sqlalchemy_database_url
from app.db.migration_adoption import verify_reconciled_bundle
from app.models import domain  # noqa: F401
from app.services.readiness_execution_admission import metadata as management_metadata
from app.db.bounded_batch_schema_0034 import metadata as bounded_batch_metadata
from app.state_transfer.one_shot_operation_reservation import metadata as one_shot_reservation_metadata
from app.db.migration_lock import (
    MIGRATION_ADVISORY_LOCK_KEY,
    require_transaction_lock,
)
from app.db.exact_revision_runner import _require_authorized_alembic_config


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
# Keep management tables visible to Alembic, without putting them into business
# ORM Base.metadata/create_all or changing historical model-based migrations.
target_metadata = [Base.metadata, management_metadata, bounded_batch_metadata, one_shot_reservation_metadata]
if config.attributes.get("connection") is None:
    config.set_main_option(
        "sqlalchemy.url",
        sqlalchemy_database_url(get_settings().database_url).replace("%", "%%"),
    )


def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run_online_migrations(connection, *, bounded=False):
    context.configure(connection=connection, target_metadata=target_metadata)
    if bounded and getattr(context.get_context().opts.get("fn"), "__name__", "") != "upgrade":
        raise RuntimeError("bounded runner supports upgrade only, never stamp or downgrade")
    with context.begin_transaction():
        context.run_migrations()
        # Revision 0020 marks an exact Issue #117 bundle reconciliation on the
        # migration connection. Revision 0027 verifies and clears that marker.
        # If a migration command stops short of 0027, this outer check refuses
        # the incomplete rebuild before the surrounding transaction can commit.
        if not bounded:
            verify_reconciled_bundle(connection, "0027")


def run_migrations_online():
    supplied = config.attributes.get("connection")
    if supplied is not None:
        if (
            supplied.dialect.name != "postgresql"
            or config.attributes.get("exact_revision") != "0032"
            or context.get_revision_argument() != "0032"
        ):
            raise RuntimeError("only the bounded PostgreSQL 0032 runner may supply a connection")
        require_transaction_lock(supplied)
        _require_authorized_alembic_config(config, supplied)
        _run_online_migrations(supplied, bounded=True)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    if connectable.dialect.name != "postgresql":
        with connectable.connect() as connection:
            _run_online_migrations(connection)
        return

    # Replit Autoscale may start multiple instances during the same rollout.
    # Hold one fixed PostgreSQL session advisory lock on a dedicated connection
    # while another connection performs Alembic work. All application-started
    # Alembic processes use this same lock, so only one migration runner can
    # mutate schema at a time. A crashed process releases the lock when its
    # PostgreSQL session closes.
    with connectable.connect() as lock_connection:
        lock_connection.execute(
            text("SELECT pg_advisory_lock(:lock_key)"),
            {"lock_key": MIGRATION_ADVISORY_LOCK_KEY},
        )
        try:
            with connectable.connect() as migration_connection:
                _run_online_migrations(migration_connection)
        finally:
            try:
                lock_connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": MIGRATION_ADVISORY_LOCK_KEY},
                )
            except Exception:
                # Closing the dedicated PostgreSQL session below releases any
                # remaining session advisory lock even when explicit unlock is
                # unavailable because the connection failed.
                pass


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
