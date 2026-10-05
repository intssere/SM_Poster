"""Real disposable PostgreSQL; fake storage only, with durable no-mutation proofs."""
import json

import pytest
import sqlalchemy as sa

from app.state_transfer import media_migration as m, transfer, migrate_media as cli
from tests.test_media_continuity_postgres import prepared, source, pytestmark
from tests.test_media_migration import Memory, NAMES


@pytest.fixture
def configured(prepared, monkeypatch):
    engine, root, rows = prepared
    for key, value in {"endpoint": "https://fixture.railway.example", "bucket": "media-fixture",
                       "access_key": "test", "secret_key": "test", "region": "auto",
                       "path_style": "true"}.items():
        monkeypatch.setenv(NAMES[key], value)
    monkeypatch.setattr(m.sa, "create_engine", lambda *a, **kw: engine)
    target = Memory()
    return engine, root, rows, target


def invoke(configured, **kwargs):
    engine, root, rows, target = configured
    return m.run(database_env="MEDIA_SOURCE_FIXTURE", roots=[root], target_envs=NAMES,
                 source_factory=lambda bindings: pytest.fail("Unexpected Replit client"),
                 target_factory=lambda config, bindings: target,
                 **{"execute": True, **kwargs})


def test_one_readonly_repeatable_transaction_and_all_business_fingerprints_unchanged(configured):
    engine, root, rows, target = configured
    before = transfer.export_source(engine)
    statements, begins = [], []
    def observe(c, cursor, statement, params, context, many):
        statements.append(statement)
    def begin(c):
        begins.append(1)
    sa.event.listen(engine, "before_cursor_execute", observe)
    sa.event.listen(engine, "begin", begin)
    try:
        result = invoke(configured)
    finally:
        sa.event.remove(engine, "before_cursor_execute", observe)
        sa.event.remove(engine, "begin", begin)
    after = transfer.export_source(engine)
    assert before["rows"] == after["rows"]
    assert before["manifest"]["tables"] == after["manifest"]["tables"]
    assert result["success"] and result["media_certification"] == "PASS"
    assert begins == [1]
    assert all(s.split()[0] in {"SELECT", "SET", "SHOW"} for s in statements)
    assert "SET TRANSACTION READ ONLY" in statements
    assert result["database_transactions"] == 1 and result["database_writes"] == 0
    assert len(target.values) == 17
    assert str(root) not in json.dumps(result)


@pytest.mark.parametrize("case", ["0034", "open-control", "open-auth", "bad-published-count",
                                 "running-job", "invalid-sha", "missing-size"])
def test_strict_revision_closed_state_and_metadata_before_storage(configured, case):
    engine, root, rows, target = configured
    changes = {
        "0034": ("UPDATE public.alembic_version SET version_num='0034'",
                 "UPDATE public.alembic_version SET version_num='0031'"),
        "open-control": ("UPDATE public.routine_publishing_control SET state='LIVE'",
                         "UPDATE public.routine_publishing_control SET state='PAUSED'"),
    }
    if case in changes:
        update, restore = changes[case]
        with engine.begin() as c:
            c.exec_driver_sql(update)
        try:
            result = invoke(configured)
        finally:
            with engine.begin() as c:
                c.exec_driver_sql(restore)
    elif case in {"invalid-sha", "missing-size"}:
        row = rows[0]
        column = "sha256" if case == "invalid-sha" else "size_bytes"
        with engine.begin() as c:
            c.execute(sa.text(f"UPDATE public.pin_creatives SET {column}=:value WHERE id=:id"),
                      {"value": "invalid" if column == "sha256" else None, "id": row["id"]})
        try:
            result = invoke(configured)
        finally:
            with engine.begin() as c:
                c.execute(sa.text(f"UPDATE public.pin_creatives SET {column}=:value WHERE id=:id"),
                          {"value": row[column], "id": row["id"]})
    else:
        table, bad = {
            "bad-published-count": ("pin_publications", "APPROVED"),
            "running-job": ("catalog_sync_jobs", "RUNNING"),
            "open-auth": ("buffer_pilot_activations", "ACTIVE"),
        }[case]
        with engine.begin() as c:
            old = c.exec_driver_sql(
                f"SELECT id,status::text AS status FROM public.{table} ORDER BY id LIMIT 1"
            ).mappings().one()
            c.execute(sa.text(f"UPDATE public.{table} SET status=:status WHERE id=:id"),
                      {"id": old["id"], "status": bad})
        try:
            # Run a genuine, separate read-only transaction against committed
            # fixture damage; never replace transaction/closed-state guards.
            result = invoke(configured)
        finally:
            with engine.begin() as c:
                c.execute(sa.text(f"UPDATE public.{table} SET status=:status WHERE id=:id"),
                          dict(old))
    assert not result["success"] and result["terminal_stage"] == "READ_ONLY_METADATA"
    assert result["objects"] == [] and target.calls == []


def test_zero_write_dry_run_and_separately_gated_second_invocation(configured):
    first = invoke(configured, dry_run=True, execute=False)
    assert first["success"] and first["target_put_attempts"] == 0
    assert configured[-1].values == {}
    unauthorized = invoke(configured, execute=False)
    assert not unauthorized["success"] and unauthorized["database_transactions"] == 0
    assert invoke(configured)["success"]
    again = invoke(configured)
    assert again["target_counts"] == {"VERIFIED_EXISTING": 17}
    assert again["target_put_attempts"] == 0


def test_real_cli_dry_run_safe_output(configured, monkeypatch, capsys):
    from app.state_transfer import migration_storage
    engine, root, rows, target = configured
    monkeypatch.setattr(migration_storage, "S3ExactTarget", lambda config, bindings: target)
    args = ["--database-env", "MEDIA_SOURCE_FIXTURE", "--source-root", str(root), "--dry-run"]
    for k, name in NAMES.items():
        args += ["--target-" + k.replace("_", "-") + "-env", name]
    assert cli.main(args) == 0
    out = capsys.readouterr()
    assert out.err == "" and str(root) not in out.out
    assert json.loads(out.out)["target_put_attempts"] == 0
