"""Verify the 0035 one-shot ledger against its frozen PostgreSQL contract.

No writes and no adoption/repair. Missing or unexpected public ledger catalog
objects fail closed. The earlier 0034 controller is separately verified first.
"""
import sqlalchemy as sa

TABLE = "routine_one_shot_preparation_operations"
COLS = [
    ("operation_key", "character varying(96)", False),
    ("preflight_fingerprint", "character varying(64)", False),
    ("batch_id", "character varying(36)", False),
    ("state", "character varying(20)", False),
    ("created_at", "timestamp with time zone", False),
]


def verify(connection):
    columns = connection.execute(sa.text("""
        SELECT a.attname, format_type(a.atttypid,a.atttypmod), a.attnotnull
        FROM pg_attribute a
        WHERE a.attrelid=to_regclass(:table)
          AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum
    """), {"table": "public." + TABLE}).all()
    if columns != [(name, typ, not nullable) for name, typ, nullable in COLS]:
        raise RuntimeError("0035 one-shot ledger columns differ")
    constraints = dict(connection.execute(sa.text("""
        SELECT con.conname, pg_get_constraintdef(con.oid)
        FROM pg_constraint con WHERE con.conrelid=to_regclass(:table)
    """), {"table": "public." + TABLE}).all())
    expected = {
        "routine_one_shot_preparation_operations_pkey",
        "routine_one_shot_preparation_operations_batch_id_key",
        "ck_one_shot_preparation_state",
        "ck_one_shot_preparation_fingerprint_length",
    }
    if set(constraints) != expected:
        raise RuntimeError("0035 one-shot ledger constraints differ")
    if (constraints["routine_one_shot_preparation_operations_pkey"] !=
            "PRIMARY KEY (operation_key)" or
            constraints["routine_one_shot_preparation_operations_batch_id_key"] !=
            "UNIQUE (batch_id)" or
            "length(" not in constraints["ck_one_shot_preparation_fingerprint_length"] or
            "state" not in constraints["ck_one_shot_preparation_state"]):
        raise RuntimeError("0035 one-shot ledger constraint drift")
    table_security = connection.execute(sa.text("""
        SELECT c.relkind, c.relrowsecurity, c.relforcerowsecurity
        FROM pg_class c WHERE c.oid=to_regclass(:table)
    """), {"table": "public." + TABLE}).one_or_none()
    if table_security != ("r", False, False):
        raise RuntimeError("0035 one-shot ledger relation drift")
