from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from app.db.base import Base
from app.core.config import get_settings
from app.db import migration_adoption
from app.db.session import sqlalchemy_database_url
from app.models import domain  # noqa: F401


MIGRATION_ADVISORY_LOCK_KEY = 490019

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata
config.set_main_option(
    "sqlalchemy.url",
    sqlalchemy_database_url(get_settings().database_url).replace("%", "%%"),
)


def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run_online_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        reconciled_bundle = False
        if connection.dialect.name == "postgresql":
            reconciled_bundle = migration_adoption.reconcile_preapplied_bundle(
                connection,
                "0020",
            )
        context.run_migrations()
        if reconciled_bundle:
            migration_adoption.verify_reconciled_bundle(connection)


def run_migrations_online():
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
