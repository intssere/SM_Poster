"""Real disposable PostgreSQL with memory subprocesses and business snapshots."""
import json
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.services.media_storage import StorageUnavailable
from app.state_transfer import curl_migration_storage as c, media_migration as m, transfer
from tests.test_media_continuity_postgres import prepared, source, pytestmark
from tests.test_media_migration_postgres import configured
from tests.test_media_migration import NAMES
from tests.curl_migration_fixture import CurlMemory


@pytest.fixture
def curl_wire(monkeypatch):
    wire = CurlMemory()
    monkeypatch.setattr(c, "check_curl_capability",
                        lambda: SimpleNamespace(path="/fixture/curl", verify=lambda: None))
    monkeypatch.setattr(c.subprocess, "Popen", wire)
    return wire


def invoke(configured, **kwargs):
    engine, root, _, _ = configured
    return m.run(database_env="MEDIA_SOURCE_FIXTURE", roots=[root], target_envs=NAMES,
                 target_transport="curl", **{"dry_run": True, **kwargs})


def test_curl_dry_run_one_readonly_transaction_and_unchanged_business_state(configured, curl_wire):
    engine, _, _, _ = configured
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
    assert result["success"] and result["target_counts"] == {"MISSING": 17}
    assert result["database_transactions"] == 1 and result["database_writes"] == 0
    assert result["target_put_attempts"] == 0 and curl_wire.values == {}
    assert begins == [1] and "SET TRANSACTION READ ONLY" in statements
    assert all(s.split()[0] in {"SELECT", "SET", "SHOW"} for s in statements)
    assert [method for method, _ in curl_wire.calls] == ["GET"] * 17
    assert result["publishing_admission"] == "NOT_GRANTED"


@pytest.mark.parametrize("case", ["revision", "open-authorization", "running-job", "publication-history"])
def test_unchanged_closed_state_guards_refuse_before_target_process(configured, curl_wire, case):
    engine = configured[0]
    if case == "revision":
        with engine.begin() as connection:
            connection.exec_driver_sql("UPDATE public.alembic_version SET version_num='0034'")
        try:
            result = invoke(configured)
        finally:
            with engine.begin() as connection:
                connection.exec_driver_sql("UPDATE public.alembic_version SET version_num='0031'")
    else:
        table, bad = {
            "open-authorization": ("buffer_pilot_activations", "ACTIVE"),
            "running-job": ("catalog_sync_jobs", "RUNNING"),
            "publication-history": ("pin_publications", "APPROVED"),
        }[case]
        with engine.begin() as connection:
            old = connection.exec_driver_sql(
                f"SELECT id,status::text AS status FROM public.{table} ORDER BY id LIMIT 1"
            ).mappings().one()
            connection.execute(sa.text(f"UPDATE public.{table} SET status=:status WHERE id=:id"),
                               {"status": bad, "id": old["id"]})
        try:
            result = invoke(configured)
        finally:
            with engine.begin() as connection:
                connection.execute(sa.text(f"UPDATE public.{table} SET status=:status WHERE id=:id"),
                                   dict(old))
    assert result["terminal_stage"] == "READ_ONLY_METADATA" and not result["success"]
    assert result["objects"] == [] and curl_wire.calls == []
    assert result["database_writes"] == 0 and result["target_put_attempts"] == 0


def test_curl_capability_failure_does_not_connect_postgres(configured, monkeypatch):
    def refuse():
        raise StorageUnavailable("fixture")
    monkeypatch.setattr(c, "check_curl_capability", refuse)
    monkeypatch.setattr(m.sa, "create_engine", lambda *a, **k: pytest.fail("Connection attempted"))
    result = invoke(configured)
    assert result["terminal_stage"] == "TARGET_TRANSPORT_CAPABILITY"
    assert result["database_transactions"] == 0
