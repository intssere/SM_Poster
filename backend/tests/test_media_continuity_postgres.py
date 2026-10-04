"""Closed-state PostgreSQL certification, never attached databases or storage."""
import hashlib
import json

import pytest
import sqlalchemy as sa

from app.state_transfer import media_continuity as media
from app.state_transfer import inventory_media_continuity as cli
from tests.test_migration_closed_state_transfer import source
from tests.test_readiness_execution_admission_0032 import pytestmark


@pytest.fixture
def prepared(source, tmp_path, monkeypatch):
    with source.connect() as c:
        prior = [dict(row) for row in c.exec_driver_sql(
            "SELECT id,sha256,size_bytes,render_status FROM public.pin_creatives ORDER BY id"
        ).mappings()]
    update = sa.text("UPDATE public.pin_creatives SET sha256=:sha256,"
                     "size_bytes=:size_bytes,render_status=:render_status WHERE id=:id")
    rows = []
    for old in prior:
        png = b"\x89PNG\r\n\x1a\nisolated-continuity:" + old["id"].encode()
        row = {"id": old["id"], "sha256": hashlib.sha256(png).hexdigest(),
               "size_bytes": len(png), "render_status": "RENDERED"}
        rows.append(row)
        (tmp_path / (old["id"] + ".png")).write_bytes(png)
    with source.begin() as c:
        c.execute(update, rows)
    monkeypatch.setenv("MEDIA_SOURCE_FIXTURE", source.url.render_as_string(hide_password=False))
    try:
        yield source, tmp_path, rows
    finally:
        with source.begin() as c:
            c.execute(update, prior)


def invocation(root, **kwargs):
    return media.run_inventory(database_env="MEDIA_SOURCE_FIXTURE", roots=[root],
                               **{"execute": True, **kwargs})


def test_complete_readonly_inventory_and_plan_no_row_mutation(prepared):
    engine, root, rows = prepared
    events = []

    def observe(c, cursor, statement, parameters, context, executemany):
        events.append(statement.split()[0].upper())

    sa.event.listen(engine, "before_cursor_execute", observe)
    try:
        first = media.certify_inventory(engine, [root], plan=True)
        second = media.certify_inventory(engine, [root], plan=True)
    finally:
        sa.event.remove(engine, "before_cursor_execute", observe)
    assert first == second
    assert first["complete"] is True and first["statuses"]["MATCHED"] == 17
    assert first["read_only"] == "on" and first["isolation"] == "repeatable read"
    assert first["closed_state"] == "PASS"
    assert first["database_revision"] == "0031"
    assert len(first["objects"]) == 17
    assert not set(events) & {"INSERT", "UPDATE", "DELETE", "ALTER", "CREATE"}
    assert first["provider_calls"] == first["storage_writes"] == first["database_writes"] == 0
    with engine.connect() as c:
        after = [dict(row) for row in c.exec_driver_sql(
            "SELECT id,sha256,size_bytes,render_status FROM public.pin_creatives ORDER BY id"
        ).mappings()]
        assert rows == after
    assert str(root) not in json.dumps(first)


@pytest.mark.parametrize("case,status", [
    ("missing", "MISSING"), ("corrupt", "DIGEST_MISMATCH"),
    ("duplicate", "DUPLICATE"), ("invalid-metadata", "UNSUPPORTED"),
])
def test_incomplete_persisted_coverage_refuses(prepared, case, status):
    engine, root, rows = prepared
    candidate = root / (rows[0]["id"] + ".png")
    if case == "missing":
        candidate.unlink()
    elif case == "corrupt":
        candidate.write_bytes(candidate.read_bytes() + b"bad")
    elif case == "duplicate":
        nested = root / "nested"
        nested.mkdir()
        (nested / candidate.name).write_bytes(candidate.read_bytes())
    else:
        with engine.begin() as c:
            c.execute(sa.text("UPDATE public.pin_creatives SET sha256='invalid' WHERE id=:id"),
                      {"id": rows[0]["id"]})
    result = invocation(root, plan=True)
    assert result["success"] is False and result["complete"] is False
    assert result["statuses"][status] == 1
    assert result["statuses"]["MATCHED"] == 16
    assert len(result["objects"]) == 16


def test_open_state_refuses_before_local_inventory(prepared, monkeypatch):
    engine, root, _ = prepared
    monkeypatch.setattr(media, "_files", lambda *_: pytest.fail("Open-state local scan"))
    with engine.begin() as c:
        c.exec_driver_sql("UPDATE public.routine_publishing_control SET state='LIVE'")
    try:
        result = invocation(root)
    finally:
        with engine.begin() as c:
            c.exec_driver_sql("UPDATE public.routine_publishing_control SET state='PAUSED'")
    assert result["diagnostic"]["stage"] == "READ_ONLY_INVENTORY"
    assert result["success"] is False


def test_named_ack_cli_safe_result_and_no_dsn_paths(prepared, monkeypatch, capsys):
    engine, root, _ = prepared
    monkeypatch.setenv("MEDIA_ACK_FIXTURE", media.EXECUTION_ACK)
    args = ["--database-env", "MEDIA_SOURCE_FIXTURE", "--source-root", str(root),
            "--execution-env", "MEDIA_ACK_FIXTURE", "--target-plan"]
    assert cli.main(args) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert output.err == ""
    assert str(root) not in output.out
    assert engine.url.render_as_string(hide_password=False) not in output.out
    assert result["success"] is True and result["mode"] == "TARGET_PLAN"
    assert result["provider_certification"] == "NOT_CHECKED"


def test_wrong_revision_and_missing_explicit_env_are_safe(prepared, monkeypatch):
    engine, root, _ = prepared
    with engine.begin() as c:
        c.exec_driver_sql("UPDATE public.alembic_version SET version_num='0032'")
    try:
        assert invocation(root)["success"] is False
    finally:
        with engine.begin() as c:
            c.exec_driver_sql("UPDATE public.alembic_version SET version_num='0031'")
    monkeypatch.delenv("MEDIA_SOURCE_FIXTURE")
    assert invocation(root)["diagnostic"]["stage"] == "CONFIGURATION"


def test_imported_0034_metadata_certifies_same_local_bindings(prepared):
    from app.state_transfer import transfer
    from tests.test_readiness_execution_admission_0032 import _isolated_database
    source_engine, root, _ = prepared
    bundle = transfer.export_source(source_engine)
    fingerprint = bundle["manifest"]["manifest_sha256"]
    with _isolated_database("0034") as (target, _):
        # Only fixture setup imports into this disposable database.
        transfer.import_target(target, bundle, fingerprint)
        result = media.certify_inventory(target, [root], plan=True)
        assert result["success"] is True and result["database_revision"] == "0034"
        assert result["statuses"]["MATCHED"] == 17 and result["read_only"] == "on"
        assert transfer.certify_target(target, bundle, fingerprint)["database_certification"] == "PASS"