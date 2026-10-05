"""Task 61.44: strictly read-only production media certification regressions."""
from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

from app.services.media_storage import StorageMissing, StorageUnavailable, media_key
from app.state_transfer import certify_production_media as cli
from app.state_transfer import production_media_certification as cert
from app.state_transfer import transfer
from tests.test_media_continuity_postgres import prepared
from tests.test_readiness_execution_admission_0032 import _isolated_database, pytestmark


PNG_PREFIX = b"\x89PNG\r\n\x1a\nisolated-continuity:"


class ReadOnlyTarget:
    def __init__(self, values, *, fault=None):
        self.values = dict(values)
        self.fault = fault
        self.calls = []
        self.closed = False

    def get(self, key, size):
        self.calls.append(("get", key, size))
        if self.fault and key == sorted(self.values)[0]:
            if self.fault == "missing":
                raise StorageMissing("fixture missing")
            if self.fault == "unavailable":
                raise StorageUnavailable("fixture unavailable")
            if self.fault == "conflict":
                value = self.values[key]
                return value[:-1] + bytes([value[-1] ^ 1])
        return self.values[key]

    def close(self):
        self.closed = True


@pytest.fixture
def production_target(prepared, monkeypatch):
    source_engine, _, rows = prepared
    bundle = transfer.export_source(source_engine)
    fingerprint = bundle["manifest"]["manifest_sha256"]

    with _isolated_database("0034") as (engine, database_url):
        transfer.import_target(engine, bundle, fingerprint)

        monkeypatch.setenv(cert.DATABASE_ENV, database_url)
        for name in cert.CLOSED_FALSE_GATES:
            monkeypatch.setenv(name, "false")
        for name, value in cert.SAFE_SCALARS.items():
            monkeypatch.setenv(name, value)

        storage = {
            "endpoint": "https://fixture.railway.example",
            "bucket": "media-fixture",
            "access_key": "ACCESS_PRIVATE_SENTINEL",
            "secret_key": "SECRET_PRIVATE_SENTINEL",
            "region": "auto",
            "path_style": "true",
        }
        for field, env_name in cert.STORAGE_ENVS.items():
            monkeypatch.setenv(env_name, storage[field])

        values = {}
        for row in rows:
            payload = PNG_PREFIX + row["id"].encode()
            assert len(payload) == row["size_bytes"]
            values[media_key("creative", row["id"], row["sha256"])] = payload

        yield engine, rows, values


def invoke(production_target, monkeypatch, *, fault=None, observe_engine=False):
    engine, rows, values = production_target
    target = ReadOnlyTarget(values, fault=fault)
    if observe_engine:
        monkeypatch.setattr(cert.sa, "create_engine", lambda *a, **k: engine)
    result = cert.run(target_factory=lambda config, bindings: target)
    return result, target


def test_certification_is_one_readonly_repeatable_transaction_and_exact_17_gets(
    production_target, monkeypatch
):
    engine, rows, values = production_target
    before = [
        dict(row)
        for row in engine.connect().exec_driver_sql(
            "SELECT id,sha256,size_bytes,render_status FROM public.pin_creatives ORDER BY id"
        ).mappings()
    ]

    statements, begins = [], []

    def observe(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    def begin(connection):
        begins.append(1)

    sa.event.listen(engine, "before_cursor_execute", observe)
    sa.event.listen(engine, "begin", begin)
    try:
        result, target = invoke(production_target, monkeypatch, observe_engine=True)
    finally:
        sa.event.remove(engine, "before_cursor_execute", observe)
        sa.event.remove(engine, "begin", begin)

    with engine.connect() as connection:
        after = [
            dict(row)
            for row in connection.exec_driver_sql(
                "SELECT id,sha256,size_bytes,render_status FROM public.pin_creatives ORDER BY id"
            ).mappings()
        ]

    assert result["success"] is True
    assert result["terminal_stage"] == "COMPLETE"
    assert result["database_revision"] == "0034"
    assert result["schema_canonicality"] == result["closed_state"] == "PASS"
    assert result["routine_state"] == "PAUSED"
    assert result["authoritative_binding_count"] == 17
    assert result["target_counts"] == {"VERIFIED_EXISTING": 17}
    assert result["media_certification"] == "PASS"
    assert result["publishing_admission"] == "NOT_GRANTED"
    assert result["database_transactions"] == 1
    assert result["database_writes"] == result["object_writes"] == result["provider_calls"] == 0
    assert result["object_reads"] == 17 and result["automatic_retries"] == 0
    assert len(target.calls) == 17 and all(call[0] == "get" for call in target.calls)
    assert target.closed is True
    assert begins == [1]
    assert all(statement.lstrip().split(None, 1)[0].upper() in {"SELECT", "SET", "SHOW"}
               for statement in statements)
    assert any("SET TRANSACTION READ ONLY" in statement for statement in statements)
    assert before == after == rows
    rendered = json.dumps(result)
    assert "ACCESS_PRIVATE_SENTINEL" not in rendered
    assert "SECRET_PRIVATE_SENTINEL" not in rendered
    assert "postgresql" not in rendered
    assert all(item["key"] in values for item in result["objects"])


@pytest.mark.parametrize(
    "fault,status",
    [("missing", "MISSING"), ("conflict", "CONFLICT"), ("unavailable", "UNAVAILABLE")],
)
def test_object_storage_ambiguity_fails_closed_without_any_write(
    production_target, monkeypatch, fault, status
):
    result, target = invoke(production_target, monkeypatch, fault=fault)
    assert result["success"] is False
    assert result["terminal_stage"] == "OBJECT_READS"
    assert result["media_certification"] == "NOT_GRANTED"
    assert result["publishing_admission"] == "NOT_GRANTED"
    assert result["target_counts"][status] == 1
    assert result["target_counts"]["VERIFIED_EXISTING"] == 16
    assert result["object_reads"] == 17
    assert result["database_writes"] == result["object_writes"] == result["provider_calls"] == 0
    assert len(target.calls) == 17 and all(call[0] == "get" for call in target.calls)


def test_gate_mismatch_refuses_before_database_or_storage(production_target, monkeypatch):
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")

    def forbidden(*args, **kwargs):
        pytest.fail("database/storage access occurred after closed-gate mismatch")

    monkeypatch.setattr(cert.sa, "create_engine", forbidden)
    result = cert.run(target_factory=forbidden)
    assert result["success"] is False
    assert result["terminal_stage"] == "GATES"
    assert result["database_transactions"] == 0
    assert result["object_reads"] == result["object_writes"] == result["provider_calls"] == 0


def test_wrong_revision_or_live_control_refuses_before_object_storage(
    production_target, monkeypatch
):
    engine, _, values = production_target
    target = ReadOnlyTarget(values)

    with engine.begin() as connection:
        connection.exec_driver_sql("UPDATE public.alembic_version SET version_num='0033'")
    try:
        result = cert.run(target_factory=lambda config, bindings: target)
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql("UPDATE public.alembic_version SET version_num='0034'")
    assert result["success"] is False
    assert result["terminal_stage"] == "READ_ONLY_DATABASE"
    assert target.calls == []

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE public.routine_publishing_control SET state='LIVE'"
        )
    try:
        result = cert.run(target_factory=lambda config, bindings: target)
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE public.routine_publishing_control SET state='PAUSED'"
            )
    assert result["success"] is False
    assert result["terminal_stage"] == "READ_ONLY_DATABASE"
    assert target.calls == []


def test_fingerprints_are_deterministic_and_do_not_grant_publishing(
    production_target, monkeypatch
):
    first, _ = invoke(production_target, monkeypatch)
    second, _ = invoke(production_target, monkeypatch)
    assert first["success"] and second["success"]
    for name in (
        "binding_fingerprint",
        "database_fingerprint",
        "gate_fingerprint",
        "target_fingerprint",
        "storage_configuration_fingerprint",
        "certification_fingerprint",
    ):
        assert first[name] == second[name]
    assert first["publishing_admission"] == second["publishing_admission"] == "NOT_GRANTED"


def test_cli_has_no_target_arguments_and_never_echoes_argument_secrets(monkeypatch, capsys):
    private = "PRIVATE_ARGUMENT_SENTINEL"
    assert cli.main(["--database-url", private]) == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert private not in output.out
    result = json.loads(output.out)
    assert result["terminal_stage"] == "ARGUMENTS"
    assert result["database_writes"] == result["object_writes"] == result["provider_calls"] == 0


def test_cli_success_output_is_sanitized(production_target, monkeypatch, capsys):
    _, _, values = production_target
    target = ReadOnlyTarget(values)
    monkeypatch.setattr(
        cert, "run", lambda: cert.run.__wrapped__()
        if hasattr(cert.run, "__wrapped__")
        else None
    )
    # Patch the CLI's imported function by replacing the module attribute with a
    # bounded fixture invocation; the production CLI itself accepts no targets.
    original = cert.run
    monkeypatch.setattr(
        cert,
        "run",
        lambda: original(target_factory=lambda config, bindings: target),
    )
    assert cli.main([]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    result = json.loads(output.out)
    assert result["success"] is True
    assert result["target_counts"] == {"VERIFIED_EXISTING": 17}
    assert "ACCESS_PRIVATE_SENTINEL" not in output.out
    assert "SECRET_PRIVATE_SENTINEL" not in output.out
