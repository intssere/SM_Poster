"""Task 61.47: strictly read-only Buffer destination certification regressions."""
from __future__ import annotations

import inspect
import json

import pytest
import sqlalchemy as sa

from app.state_transfer import buffer_destination_certification as cert
from app.state_transfer import certify_buffer_destination as cli
from tests.test_readiness_execution_admission_0032 import _isolated_database, pytestmark


PRIVATE_KEY = "BUFFER_PRIVATE_SENTINEL"


class FakeGateway:
    def __init__(self, settings, *, missing_board=False, fail=False):
        self.settings = settings
        self.missing_board = missing_board
        self.fail = fail
        self.calls = []

    async def organizations(self):
        self.calls.append(("organizations",))
        if self.fail:
            raise RuntimeError("PRIVATE_PROVIDER_RESPONSE_SENTINEL")
        return [
            {"id": "org-1", "name": "Organization"},
            {"id": "other-org", "name": "Other"},
        ]

    async def channels(self, organization_id):
        self.calls.append(("channels", organization_id))
        boards = [{"serviceId": "board-a", "name": "A"}]
        if not self.missing_board:
            boards.append({"serviceId": "board-b", "name": "B"})
        return [
            {
                "id": "channel-1",
                "name": "Pinterest",
                "displayName": "Pinterest",
                "service": "pinterest",
                "isDisconnected": False,
                "isLocked": False,
                "boards": boards,
            }
        ]

    async def create_pinterest_post(self, payload):
        pytest.fail("provider mutation path must never be called")


@pytest.fixture
def production_buffer(monkeypatch):
    with _isolated_database("0034") as (engine, database_url):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO public.routine_publishing_control (id,state) "
                "VALUES ('default','PAUSED') "
                "ON CONFLICT (id) DO UPDATE SET state='PAUSED'"
            )
            connection.exec_driver_sql(
                "INSERT INTO public.pinterest_connections "
                "(id,provider,external_user_id,granted_scopes,access_token_ciphertext,"
                "refresh_token_ciphertext,status) "
                "VALUES ('connection-1','pinterest','external-user','[]'::json,"
                "'cipher-access','cipher-refresh','CONNECTED')"
            )
            connection.exec_driver_sql(
                "INSERT INTO public.pinterest_boards "
                "(id,connection_id,external_board_id,name,is_ads_only,is_active,is_eligible) "
                "VALUES "
                "('local-a','connection-1','board-a','A',false,true,true),"
                "('local-b','connection-1','board-b','B',false,true,true)"
            )

        monkeypatch.setenv(cert.DATABASE_ENV, database_url)
        for name in cert.CLOSED_FALSE_GATES:
            monkeypatch.setenv(name, "false")
        for name, value in cert.SAFE_SCALARS.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv(cert.BUFFER_ENVS["api_key"], PRIVATE_KEY)
        monkeypatch.setenv(cert.BUFFER_ENVS["organization_id"], "org-1")
        monkeypatch.setenv(cert.BUFFER_ENVS["channel_id"], "channel-1")
        monkeypatch.delenv("BUFFER_API_BASE", raising=False)
        yield engine


def invoke(production_buffer, monkeypatch, *, missing_board=False, fail=False):
    holder = {}

    def factory(settings):
        gateway = FakeGateway(settings, missing_board=missing_board, fail=fail)
        holder["gateway"] = gateway
        return gateway

    monkeypatch.setattr(cert.sa, "create_engine", lambda *a, **k: production_buffer)
    result = cert.run(gateway_factory=factory)
    return result, holder.get("gateway")


def test_success_is_one_readonly_transaction_and_exactly_two_provider_reads(
    production_buffer, monkeypatch
):
    engine = production_buffer
    before = {}
    with engine.connect() as connection:
        before["connections"] = connection.exec_driver_sql(
            "SELECT id,status FROM public.pinterest_connections ORDER BY id"
        ).all()
        before["boards"] = connection.exec_driver_sql(
            "SELECT id,external_board_id,is_active,is_eligible "
            "FROM public.pinterest_boards ORDER BY id"
        ).all()
        before["control"] = connection.exec_driver_sql(
            "SELECT id,state FROM public.routine_publishing_control ORDER BY id"
        ).all()

    statements, begins = [], []

    def observe(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    def begin(connection):
        begins.append(1)

    sa.event.listen(engine, "before_cursor_execute", observe)
    sa.event.listen(engine, "begin", begin)
    try:
        result, gateway = invoke(production_buffer, monkeypatch)
    finally:
        sa.event.remove(engine, "before_cursor_execute", observe)
        sa.event.remove(engine, "begin", begin)

    after = {}
    with engine.connect() as connection:
        after["connections"] = connection.exec_driver_sql(
            "SELECT id,status FROM public.pinterest_connections ORDER BY id"
        ).all()
        after["boards"] = connection.exec_driver_sql(
            "SELECT id,external_board_id,is_active,is_eligible "
            "FROM public.pinterest_boards ORDER BY id"
        ).all()
        after["control"] = connection.exec_driver_sql(
            "SELECT id,state FROM public.routine_publishing_control ORDER BY id"
        ).all()

    assert result["success"] is True
    assert result["terminal_stage"] == "COMPLETE"
    assert result["database_revision"] == "0034"
    assert result["schema_canonicality"] == "PASS"
    assert result["routine_state"] == "PAUSED"
    assert result["connected_connection_count"] == 1
    assert result["local_eligible_board_count"] == 2
    assert result["organization_verified"] is True
    assert result["channel_verified"] is True
    assert result["channel_available"] is True
    assert result["matched_local_board_count"] == 2
    assert result["missing_local_board_count"] == 0
    assert result["buffer_destination_certification"] == "PASS"
    assert result["publishing_admission"] == "NOT_GRANTED"
    assert result["database_transactions"] == 1
    assert result["database_writes"] == 0
    assert result["provider_calls"] == result["provider_reads"] == 2
    assert result["provider_writes"] == result["automatic_retries"] == 0
    assert gateway.calls == [("organizations",), ("channels", "org-1")]
    assert begins == [1]
    assert all(statement.lstrip().split(None, 1)[0].upper() in {"SELECT", "SET", "SHOW"}
               for statement in statements)
    assert any("SET TRANSACTION READ ONLY" in statement for statement in statements)
    assert before == after
    rendered = json.dumps(result)
    assert PRIVATE_KEY not in rendered
    assert "cipher-access" not in rendered
    assert "cipher-refresh" not in rendered
    assert "postgresql" not in rendered
    assert "create_pinterest_post" not in inspect.getsource(cert)


def test_missing_provider_board_fails_closed_without_write(production_buffer, monkeypatch):
    result, gateway = invoke(production_buffer, monkeypatch, missing_board=True)
    assert result["success"] is False
    assert result["terminal_stage"] == "BUFFER_READS"
    assert result["buffer_destination_certification"] == "NOT_GRANTED"
    assert result["publishing_admission"] == "NOT_GRANTED"
    assert result["database_writes"] == result["provider_writes"] == 0
    assert result["provider_calls"] == result["provider_reads"] == 2
    assert gateway.calls == [("organizations",), ("channels", "org-1")]


def test_provider_failure_is_sanitized_and_never_retried(production_buffer, monkeypatch):
    result, gateway = invoke(production_buffer, monkeypatch, fail=True)
    assert result["success"] is False
    assert result["terminal_stage"] == "BUFFER_READS"
    assert result["provider_calls"] == result["provider_reads"] == 1
    assert result["automatic_retries"] == result["provider_writes"] == 0
    assert gateway.calls == [("organizations",)]
    rendered = json.dumps(result)
    assert "PRIVATE_PROVIDER_RESPONSE_SENTINEL" not in rendered
    assert PRIVATE_KEY not in rendered


def test_gate_mismatch_refuses_before_database_or_provider(production_buffer, monkeypatch):
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")

    def forbidden(*args, **kwargs):
        pytest.fail("database/provider access occurred after gate mismatch")

    monkeypatch.setattr(cert.sa, "create_engine", forbidden)
    result = cert.run(gateway_factory=forbidden)
    assert result["success"] is False
    assert result["terminal_stage"] == "GATES"
    assert result["database_transactions"] == 0
    assert result["provider_calls"] == result["provider_reads"] == result["provider_writes"] == 0


def test_missing_credential_refuses_before_database_or_provider(production_buffer, monkeypatch):
    monkeypatch.delenv(cert.BUFFER_ENVS["api_key"])

    def forbidden(*args, **kwargs):
        pytest.fail("database/provider access occurred after configuration failure")

    monkeypatch.setattr(cert.sa, "create_engine", forbidden)
    result = cert.run(gateway_factory=forbidden)
    assert result["success"] is False
    assert result["terminal_stage"] == "CONFIGURATION"
    assert result["database_transactions"] == 0
    assert result["provider_calls"] == result["provider_reads"] == result["provider_writes"] == 0


def test_wrong_revision_or_live_control_refuses_before_provider(production_buffer, monkeypatch):
    engine = production_buffer

    def forbidden(*args, **kwargs):
        pytest.fail("provider access occurred after database-state mismatch")

    monkeypatch.setattr(cert.sa, "create_engine", lambda *a, **k: engine)

    with engine.begin() as connection:
        connection.exec_driver_sql("UPDATE public.alembic_version SET version_num='0033'")
    try:
        result = cert.run(gateway_factory=forbidden)
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql("UPDATE public.alembic_version SET version_num='0034'")
    assert result["success"] is False
    assert result["terminal_stage"] == "READ_ONLY_DATABASE"
    assert result["provider_calls"] == 0

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE public.routine_publishing_control SET state='LIVE' WHERE id='default'"
        )
    try:
        result = cert.run(gateway_factory=forbidden)
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE public.routine_publishing_control SET state='PAUSED' WHERE id='default'"
            )
    assert result["success"] is False
    assert result["terminal_stage"] == "READ_ONLY_DATABASE"
    assert result["provider_calls"] == 0


def test_fingerprints_are_deterministic_and_do_not_grant_publishing(
    production_buffer, monkeypatch
):
    first, _ = invoke(production_buffer, monkeypatch)
    second, _ = invoke(production_buffer, monkeypatch)
    assert first["success"] and second["success"]
    for name in (
        "gate_fingerprint",
        "database_fingerprint",
        "buffer_configuration_fingerprint",
        "destination_fingerprint",
        "certification_fingerprint",
    ):
        assert first[name] == second[name]
    assert first["publishing_admission"] == second["publishing_admission"] == "NOT_GRANTED"


def test_cli_rejects_arguments_without_echoing_secrets(monkeypatch, capsys):
    private = "PRIVATE_ARGUMENT_SENTINEL"
    assert cli.main(["--buffer-api-key", private]) == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert private not in output.out
    result = json.loads(output.out)
    assert result["terminal_stage"] == "ARGUMENTS"
    assert result["database_writes"] == result["provider_calls"] == result["provider_writes"] == 0


def test_cli_success_output_is_sanitized(production_buffer, monkeypatch, capsys):
    original = cert.run

    def wrapped():
        return original(gateway_factory=lambda settings: FakeGateway(settings))

    monkeypatch.setattr(cert, "run", wrapped)
    monkeypatch.setattr(cert.sa, "create_engine", lambda *a, **k: production_buffer)
    assert cli.main([]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    result = json.loads(output.out)
    assert result["success"] is True
    assert result["buffer_destination_certification"] == "PASS"
    assert PRIVATE_KEY not in output.out
