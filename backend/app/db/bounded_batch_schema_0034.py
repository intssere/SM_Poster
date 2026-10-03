"""Frozen PostgreSQL DDL for the five-pin contract; not business create_all."""
from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()
references = sa.MetaData()


def reference(name):
    return sa.Table(name, references, sa.Column("id", sa.String(36), primary_key=True))


parents = {name: reference(name) for name in (
    "pinterest_portfolio_plan_items", "products", "pin_publications",
    "pinterest_boards", "routine_dispatch_permits", "publication_attempts",
)}

batches = sa.Table(
    "routine_autonomous_batches", metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("target_count", sa.Integer, nullable=False, server_default="5"),
    sa.Column("attempts_reserved", sa.Integer, nullable=False, server_default="0"),
    sa.Column("state", sa.String(20), nullable=False, server_default=sa.text("'OPEN'")),
    sa.Column("admission_closed", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("manifest_sha256", sa.String(64)),
    sa.Column("owner", sa.String(36)),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column("reason", sa.String(100)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("target_count = 5", name="ck_autonomous_batch_target"),
    sa.CheckConstraint("attempts_reserved BETWEEN 0 AND 5", name="ck_autonomous_batch_attempts"),
    sa.CheckConstraint("state IN ('OPEN','PREPARING','READY','RUNNING','PAUSED','COMPLETED','FAILED')",
                       name="ck_autonomous_batch_state"),
    sa.CheckConstraint("attempts_reserved < 5 OR admission_closed", name="ck_autonomous_batch_exhausted"),
    sa.CheckConstraint("state IN ('READY','RUNNING') OR admission_closed", name="ck_autonomous_batch_closed"),
)

entries = sa.Table(
    "routine_autonomous_batch_entries", metadata,
    sa.Column("batch_id", sa.String(36), sa.ForeignKey(batches.c.id, ondelete="RESTRICT"), primary_key=True),
    sa.Column("slot", sa.Integer, primary_key=True),
    sa.Column("item_id", sa.String(36), sa.ForeignKey(parents["pinterest_portfolio_plan_items"].c.id, ondelete="RESTRICT"), nullable=False, unique=True),
    sa.Column("product_id", sa.String(36), sa.ForeignKey(parents["products"].c.id, ondelete="RESTRICT"), nullable=False),
    sa.Column("board_id", sa.String(36), sa.ForeignKey(parents["pinterest_boards"].c.id, ondelete="RESTRICT"), nullable=False),
    sa.Column("external_board_id", sa.String(255), nullable=False),
    sa.Column("item_fingerprint", sa.String(64), nullable=False),
    sa.Column("publication_id", sa.String(36), sa.ForeignKey(parents["pin_publications"].c.id, ondelete="RESTRICT"), unique=True),
    sa.Column("permit_id", sa.String(36), sa.ForeignKey(parents["routine_dispatch_permits"].c.id, ondelete="RESTRICT"), unique=True),
    sa.Column("publication_fingerprint", sa.String(64)),
    sa.Column("request_fingerprint", sa.String(64)),
    sa.Column("reserved_at", sa.DateTime(timezone=True)),
    sa.Column("attempt_id", sa.String(36), sa.ForeignKey(parents["publication_attempts"].c.id, ondelete="RESTRICT"), unique=True),
    sa.Column("outcome", sa.String(20)),
    sa.CheckConstraint("slot BETWEEN 0 AND 4", name="ck_autonomous_entry_slot"),
    sa.CheckConstraint("outcome IS NULL OR outcome IN ('PUBLISHED','FAILED','UNKNOWN')", name="ck_autonomous_entry_outcome"),
    sa.CheckConstraint("attempt_id IS NULL OR reserved_at IS NOT NULL", name="ck_autonomous_entry_attempt"),
)

TABLES = (batches.name, entries.name)

# Identity and consumption are immutable even if a caller bypasses the service.
GUARD_SQL = """
CREATE FUNCTION public.bounded_batch_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP IN ('DELETE','TRUNCATE') THEN
    RAISE EXCEPTION 'bounded batch evidence cannot be deleted';
  END IF;
  IF TG_TABLE_NAME = 'routine_autonomous_batches' THEN
    IF TG_OP = 'UPDATE' THEN
      IF NEW.id <> OLD.id OR NEW.target_count <> OLD.target_count
         OR NEW.attempts_reserved < OLD.attempts_reserved
         OR NEW.attempts_reserved > OLD.attempts_reserved + 1
         OR (OLD.manifest_sha256 IS NOT NULL AND NEW.manifest_sha256 IS DISTINCT FROM OLD.manifest_sha256)
         OR (OLD.admission_closed AND OLD.state <> 'PREPARING' AND NOT NEW.admission_closed)
      THEN RAISE EXCEPTION 'bounded batch immutable boundary'; END IF;
      IF NEW.state <> OLD.state AND NOT (
        (OLD.state='OPEN' AND NEW.state IN ('PREPARING','PAUSED','FAILED')) OR
        (OLD.state='PREPARING' AND NEW.state IN ('READY','PAUSED','FAILED')) OR
        (OLD.state='READY' AND NEW.state IN ('RUNNING','PAUSED','FAILED')) OR
        (OLD.state='RUNNING' AND NEW.state IN ('PAUSED','FAILED','COMPLETED')) OR
        (OLD.state='PAUSED' AND NEW.state IN ('FAILED','COMPLETED'))
      ) THEN RAISE EXCEPTION 'bounded batch state transition denied'; END IF;
      IF NEW.attempts_reserved <> (
        SELECT count(*) FROM public.routine_autonomous_batch_entries
        WHERE batch_id=NEW.id AND reserved_at IS NOT NULL
      ) THEN RAISE EXCEPTION 'bounded batch reservation ledger mismatch'; END IF;
      IF NEW.state IN ('READY','RUNNING','COMPLETED') AND (
        NEW.manifest_sha256 IS NULL OR 5 <> (
          SELECT count(*) FROM public.routine_autonomous_batch_entries
          WHERE batch_id=NEW.id AND publication_id IS NOT NULL AND permit_id IS NOT NULL
          AND publication_fingerprint IS NOT NULL AND request_fingerprint IS NOT NULL
        )
      ) THEN RAISE EXCEPTION 'bounded batch requires exactly five frozen identities'; END IF;
      IF NEW.state='COMPLETED' AND (NEW.attempts_reserved<>5 OR 5<>(
        SELECT count(*) FROM public.routine_autonomous_batch_entries
        WHERE batch_id=NEW.id AND outcome='PUBLISHED'
      )) THEN RAISE EXCEPTION 'bounded batch completion evidence missing'; END IF;
    END IF;
    RETURN NEW;
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NOT EXISTS (SELECT 1 FROM public.routine_autonomous_batches
                   WHERE id = NEW.batch_id AND state = 'PREPARING')
    THEN RAISE EXCEPTION 'batch manifest is closed'; END IF;
    RETURN NEW;
  END IF;
  IF ROW(NEW.batch_id,NEW.slot,NEW.item_id,NEW.product_id,NEW.board_id,NEW.external_board_id,NEW.item_fingerprint)
     IS DISTINCT FROM ROW(OLD.batch_id,OLD.slot,OLD.item_id,OLD.product_id,OLD.board_id,OLD.external_board_id,OLD.item_fingerprint)
     OR (OLD.publication_id IS NOT NULL AND
         ROW(NEW.publication_id,NEW.permit_id,NEW.publication_fingerprint,NEW.request_fingerprint)
         IS DISTINCT FROM ROW(OLD.publication_id,OLD.permit_id,OLD.publication_fingerprint,OLD.request_fingerprint))
     OR (OLD.reserved_at IS NOT NULL AND NEW.reserved_at IS DISTINCT FROM OLD.reserved_at)
     OR (OLD.attempt_id IS NOT NULL AND NEW.attempt_id IS DISTINCT FROM OLD.attempt_id)
     OR (OLD.outcome IN ('PUBLISHED','FAILED') AND NEW.outcome IS DISTINCT FROM OLD.outcome)
  THEN RAISE EXCEPTION 'batch manifest or attempt is immutable'; END IF;
  IF OLD.publication_id IS NULL AND NEW.publication_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM public.routine_autonomous_batches WHERE id=NEW.batch_id AND state='PREPARING'
  ) THEN RAISE EXCEPTION 'batch publication binding is closed'; END IF;
  IF OLD.reserved_at IS NULL AND NEW.reserved_at IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM public.routine_autonomous_batches
    WHERE id=NEW.batch_id AND state='RUNNING' AND NOT admission_closed AND attempts_reserved<5
  ) THEN RAISE EXCEPTION 'batch reservation is closed'; END IF;
  RETURN NEW;
END $$;
"""


def install_guards(connection):
    connection.exec_driver_sql(GUARD_SQL)
    for name in TABLES:
        connection.exec_driver_sql(
            f"CREATE TRIGGER {name}_immutable BEFORE INSERT OR UPDATE OR DELETE ON public.{name} "
            "FOR EACH ROW EXECUTE FUNCTION public.bounded_batch_guard()"
        )
        connection.exec_driver_sql(
            f"CREATE TRIGGER {name}_no_truncate BEFORE TRUNCATE ON public.{name} "
            "FOR EACH STATEMENT EXECUTE FUNCTION public.bounded_batch_guard()"
        )


def verify(connection):
    """Compare every catalog property to frozen, migration-created contracts."""
    from app.db.migration_adoption import _table_fingerprints
    if _table_fingerprints(connection, TABLES) != FINGERPRINTS:
        raise RuntimeError("0034 bounded batch canonical schema mismatch")
    functions = connection.exec_driver_sql(
        "SELECT prosrc FROM pg_proc JOIN pg_namespace n ON n.oid=pronamespace "
        "WHERE n.nspname='public' AND proname='bounded_batch_guard'"
    ).scalars().all()
    expected = GUARD_SQL.split("AS $$", 1)[1].split("$$;", 1)[0]
    if functions != [expected]:
        raise RuntimeError("0034 bounded batch guard mismatch")
    for name in TABLES:
        triggers = connection.execute(sa.text(
            "SELECT tgname, tgenabled, tgtype FROM pg_trigger "
            "WHERE tgrelid=to_regclass(:name) AND NOT tgisinternal ORDER BY tgname"
        ), {"name": f"public.{name}"}).all()
        if triggers != [(f"{name}_immutable", "O", 31), (f"{name}_no_truncate", "O", 34)]:
            raise RuntimeError("0034 bounded batch trigger mismatch")


# Populated from disposable PostgreSQL migration catalogs, never live metadata.
FINGERPRINTS = {
    "routine_autonomous_batches": "6ffb29a0121f83e32d886e6863937aade77bbb66538be685dbe4b54d058fdc7a",
    "routine_autonomous_batch_entries": "dd28e65a92f06505c648d1a2f8fc8ef5e8a06b9f09ae767ddf8e9bc9f8c96348",
}