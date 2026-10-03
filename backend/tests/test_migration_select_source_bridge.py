"""Disposable SELECT bridge tests; must use the credential-fenced runner."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import threading
import time

import pytest
import sqlalchemy as sa

from app.state_transfer import select_bridge as bridge, transfer
from app.state_transfer.bridge_json import pg_json, strict_json
from app.state_transfer.catalog import Refused, canonical, dependencies, digest
from tests.state_transfer_fixture import CIPHERTEXT, identity
from tests.test_migration_closed_state_transfer import source
from tests.test_readiness_execution_admission_0032 import _isolated_database, pytestmark


def capture(engine, sql=None):
    with engine.connect() as c, c.begin():
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        return c.exec_driver_sql(sql or bridge.source_sql(),
                                 execution_options={"no_parameters": True}).scalar_one()


@pytest.fixture(scope="module")
def capsule(source):
    return capture(source)


def test_one_select_equivalent_and_importable(source, capsule):
    queries = []
    def observe(c, cursor, statement, parameters, context, many):
        queries.append(statement)
    with source.connect() as c, c.begin():
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        sa.event.listen(c, "before_cursor_execute", observe)
        result = c.exec_driver_sql(bridge.source_sql(),
                                  execution_options={"no_parameters": True}).scalar_one()
    assert len(queries) == 1 and queries[0] == bridge.source_sql()
    wrapped = bridge.wrap_source_result(result)
    dsn = transfer.export_source(source)
    for key in ("rows",):
        assert wrapped[key] == dsn[key]
    for key in ("tables", "source_schema_fingerprints", "dependency_order",
                "dependencies", "self_dependencies", "media", "publication_status_counts"):
        assert wrapped["manifest"][key] == dsn["manifest"][key]
    assert wrapped["manifest"]["snapshot"]["capture_mode"] == "single_statement_mvcc"
    assert wrapped["manifest"]["snapshot"]["isolation"] == "read committed"
    assert CIPHERTEXT in canonical(wrapped)
    with _isolated_database("0034") as (engine, _):
        sha = wrapped["manifest"]["manifest_sha256"]
        assert transfer.import_target(engine, wrapped, sha, plan=True)["writes"] == 0
        assert transfer.import_target(engine, wrapped, sha)["database_certification"] == "PASS"
        assert transfer.certify_target(engine, wrapped, sha)["database_certification"] == "PASS"
        with pytest.raises(Refused):
            transfer.import_target(engine, wrapped, sha)


def test_deterministic_encoding_and_digests(source, capsule):
    assert bridge.source_sql() == bridge.source_sql()
    assert hashlib.sha256(pg_json(capsule["payload"]).encode()).hexdigest() == capsule["capsule_sha256"]
    again = capture(source)
    assert again["payload"]["tables"] == capsule["payload"]["tables"]
    assert again["payload"]["catalog"] == capsule["payload"]["catalog"]
    # Snapshot/time evidence differs, not row digests. Check tricky key sorting,
    # Unicode/surrogates, control bytes, punctuation, and high-precision row JSON.
    with source.connect() as c:
        for value in ("é 😀 \n\\\"\t", "", "\x7f", "𝄞", "a:b", "汉字"):
            expression = bridge.ascii_quote(bridge.literal(value))
            actual = c.exec_driver_sql("SELECT " + expression,
                                       execution_options={"no_parameters": True}).scalar_one()
            assert actual == canonical(value)
        obj = {"longkey": ["é 😀", 3, None, True], "x": "\\\n", "é": "value"}
        actual = c.exec_driver_sql("SELECT " + bridge.literal(canonical(obj)) + "::jsonb::text",
                                  execution_options={"no_parameters": True}).scalar_one()
        assert actual == pg_json(obj)


def test_unicode_business_rows_and_sql_nulls_match_dsn_codec(source):
    with source.connect() as c:
        tx = c.begin()
        try:
            c.execute(sa.text("UPDATE public.products SET title=:title,shopify_data=CAST(:data AS json) WHERE id=:id"),
                      {"id": identity("products"), "title": "é 😀 \x7f \\\"\n 汉字",
                       "data": '{"😀":"𝄞","nested":{"unicode":"汉字","null":null,"precise":123456789.123456789123456789}}'})
            c.exec_driver_sql("SET TRANSACTION READ ONLY")
            result = c.exec_driver_sql(bridge.source_sql(),
                                      execution_options={"no_parameters": True}).scalar_one()
            bundle = bridge.wrap_source_result(result)
            metadata, _, _, _ = dependencies(c)
            assert bundle["rows"]["products"] == transfer.records(c, "products", metadata.tables["public.products"])
        finally:
            tx.rollback()


def test_documented_managed_result_and_truncation_refusal(capsule):
    result = {"success": True, "exitCode": 0, "exitReason": None,
              "output": canonical([{"capsule": capsule}])}
    assert bridge.wrap_source_result(result)["manifest"]["source_capsule_sha256"] == capsule["capsule_sha256"]
    for bad in (
        {**result, "output": result["output"][:-100]},
        {**result, "success": False, "exitReason": CIPHERTEXT},
        {**result, "output": canonical([{"capsule": capsule}, {"capsule": capsule}])},
    ):
        with pytest.raises(Refused, match="Source capsule verification refused"):
            bridge.wrap_source_result(bad)


def test_rehashed_foreign_key_or_capsule_evidence_tampering_refuses(capsule):
    bad = deepcopy(capsule)
    table = bad["payload"]["tables"]["products"]
    values = json.loads(table["rows"][0]["values"])
    values["store_id"] = "missing-parent"
    table["rows"][0]["values"] = json.dumps(values)
    table["rows"].sort(key=canonical)
    table["content_sha256"] = table["source_content_sha256"] = digest(table["rows"])
    bad["capsule_sha256"] = hashlib.sha256(pg_json(bad["payload"]).encode()).hexdigest()
    with pytest.raises(Refused):
        bridge.wrap_source_result(bad)
    bundle = bridge.wrap_source_result(capsule)
    bundle["manifest"]["source_capsule_sha256"] = "0" * 64
    bundle["manifest"]["manifest_sha256"] = digest({
        k: v for k, v in bundle["manifest"].items() if k != "manifest_sha256"})
    with pytest.raises(Refused, match="Source capsule evidence differs"):
        transfer.verify_bundle(bundle, bundle["manifest"]["manifest_sha256"])


@pytest.mark.parametrize("change", [
    "UPDATE public.alembic_version SET version_num='0030'",
    "ALTER TABLE public.products ADD COLUMN unexpected text",
    "ALTER TABLE public.products DROP CONSTRAINT products_store_id_fkey",
    "ALTER TYPE draftstatus ADD VALUE 'UNREVIEWED'",
    "CREATE TABLE public.unexpected (id integer)",
    "UPDATE public.routine_dispatch_permits SET status='ACTIVE' WHERE status='EXPIRED'",
    "UPDATE public.publication_dispatch_authorizations SET status='ACTIVE' WHERE status='EXPIRED'",
    "UPDATE public.buffer_pilot_activations SET consumed_at=NULL",
    "UPDATE public.pin_publications SET status='PUBLISH_UNKNOWN' WHERE status='CANCELLED'",
    "UPDATE public.routine_publishing_control SET state='LIVE'",
    "UPDATE public.routine_publishing_runs SET status='RUNNING' WHERE id=(SELECT id FROM public.routine_publishing_runs LIMIT 1)",
    "UPDATE public.catalog_sync_jobs SET status='STARTED'",
    "DELETE FROM public.audit_logs WHERE id=(SELECT id FROM public.audit_logs LIMIT 1)",
    "DELETE FROM public.pinterest_oauth_states WHERE id=(SELECT id FROM public.pinterest_oauth_states LIMIT 1)",
    "DELETE FROM public.routine_publishing_control",
])
def test_in_sql_refusal_without_sensitive_result(source, change):
    # Disposable-only fault transaction: changes visible to this SELECT, always rolled back.
    with source.connect() as c:
        tx = c.begin()
        try:
            c.exec_driver_sql(change)
            c.exec_driver_sql("SET TRANSACTION READ ONLY")
            with pytest.raises(sa.exc.DBAPIError):
                c.exec_driver_sql(bridge.source_sql(), execution_options={"no_parameters": True})
        finally:
            tx.rollback()


def test_readwrite_or_nonutc_session_refuses(source):
    with source.connect() as c:
        with pytest.raises(sa.exc.DBAPIError):
            c.exec_driver_sql(bridge.source_sql(), execution_options={"no_parameters": True})


def test_untrusted_public_helper_overload_cannot_run(source, capsule):
    with source.connect() as c:
        tx = c.begin()
        try:
            c.exec_driver_sql("""CREATE FUNCTION public.to_jsonb(public.products) RETURNS jsonb
                LANGUAGE sql IMMUTABLE AS $$ SELECT '{"spoofed":true}'::jsonb $$""")
            c.exec_driver_sql("""CREATE FUNCTION public.untrusted_domain_eq(
                information_schema.sql_identifier,text) RETURNS boolean LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'untrusted operator executed'; END $$""")
            c.exec_driver_sql("""CREATE OPERATOR public.= (LEFTARG=information_schema.sql_identifier,
                RIGHTARG=text,FUNCTION=public.untrusted_domain_eq)""")
            c.exec_driver_sql("SET TRANSACTION READ ONLY")
            result = c.exec_driver_sql(bridge.source_sql(),
                                      execution_options={"no_parameters": True}).scalar_one()
            assert result["payload"]["tables"] == capsule["payload"]["tables"]
        finally:
            tx.rollback()


def test_implicit_json_cast_refuses_before_row_conversion(source):
    with source.connect() as c:
        tx = c.begin()
        try:
            c.exec_driver_sql("""CREATE FUNCTION public.untrusted_enum_json(draftstatus)
              RETURNS json LANGUAGE plpgsql IMMUTABLE AS $$
              BEGIN RAISE EXCEPTION 'untrusted cast executed'; END $$""")
            c.exec_driver_sql("""CREATE CAST (draftstatus AS json)
              WITH FUNCTION public.untrusted_enum_json(draftstatus)""")
            c.exec_driver_sql("SET TRANSACTION READ ONLY")
            with pytest.raises(sa.exc.DBAPIError) as error:
                c.exec_driver_sql(bridge.source_sql(), execution_options={"no_parameters": True})
            assert "division by zero" in str(error.value.orig)
            assert "untrusted cast executed" not in str(error.value.orig)
        finally:
            tx.rollback()


def test_noncanonical_domain_type_refuses_before_descriptive_catalog(source):
    with source.connect() as c:
        tx = c.begin()
        try:
            c.exec_driver_sql("CREATE DOMAIN public.noncanonical_title AS varchar(512)")
            c.exec_driver_sql("ALTER TABLE public.products ALTER COLUMN title TYPE public.noncanonical_title")
            c.exec_driver_sql("SET TRANSACTION READ ONLY")
            with pytest.raises(sa.exc.DBAPIError) as error:
                c.exec_driver_sql(bridge.source_sql(), execution_options={"no_parameters": True})
            assert "division by zero" in str(error.value.orig)
        finally:
            tx.rollback()
    with source.connect() as c, c.begin():
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        c.exec_driver_sql("SET LOCAL timezone TO 'Europe/London'")
        with pytest.raises(sa.exc.DBAPIError):
            c.exec_driver_sql(bridge.source_sql(), execution_options={"no_parameters": True})


@pytest.mark.parametrize("mutation", ["digest", "count", "catalog", "isolation", "template", "rows", "oauth"])
def test_offline_tamper_refuses(capsule, mutation):
    bad = deepcopy(capsule)
    if mutation == "digest":
        bad["capsule_sha256"] = "0" * 64
    else:
        p = bad["payload"]
        if mutation == "count":
            p["tables"]["products"]["source_count"] += 1
        elif mutation == "catalog":
            p["catalog"]["products"]["security"]["relrowsecurity"] = True
        elif mutation == "isolation":
            p["snapshot"]["read_only"] = "off"
        elif mutation == "template":
            p["snapshot"]["query_template_sha256"] = "0" * 64
        elif mutation == "rows":
            p["tables"]["products"]["rows"][0]["values"] += CIPHERTEXT
        else:
            p["tables"]["pinterest_oauth_states"]["rows"] = p["tables"]["products"]["rows"][:1]
        bad["capsule_sha256"] = hashlib.sha256(pg_json(p).encode()).hexdigest()
    with pytest.raises(Refused, match="Source capsule verification refused"):
        bridge.wrap_source_result(bad)
    with pytest.raises(Refused):
        bridge.wrap_source_result(capsule, "0" * 64)


def test_one_statement_snapshot_during_concurrent_write(source, capsule):
    started, outcome = threading.Event(), []
    with source.connect() as observer:
        def writer(pid):
            started.wait(10)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                waiting = observer.execute(sa.text(
                    "SELECT wait_event FROM pg_stat_activity WHERE pid=:pid"
                ), {"pid": pid}).scalar_one_or_none()
                observer.commit()
                if waiting == "PgSleep":
                    with source.begin() as other:
                        other.execute(sa.text("UPDATE public.products SET title='concurrent-bridge-change' WHERE id=:id"),
                                      {"id": identity("products")})
                    outcome.append(True)
                    return
                time.sleep(.03)
            outcome.append(False)
        with source.connect() as c, c.begin():
            c.exec_driver_sql("SET TRANSACTION READ ONLY")
            pid = c.connection.driver_connection.info.backend_pid
            thread = threading.Thread(target=writer, args=(pid,))
            thread.start()
            started.set()
            sql = bridge.source_sql().replace('FROM public."products" t',
                      'FROM public."products" t CROSS JOIN (SELECT pg_sleep(2)) barrier')
            result = c.exec_driver_sql(sql, execution_options={"no_parameters": True}).scalar_one()
            thread.join(35)
        try:
            assert outcome == [True] and not thread.is_alive()
            assert result["payload"]["tables"]["products"] == capsule["payload"]["tables"]["products"]
            assert capture(source)["payload"]["tables"]["products"] != capsule["payload"]["tables"]["products"]
        finally:
            with source.begin() as c:
                # Find actual product identity rather than relying on sorted row position.
                original = next(json.loads(r["values"]) for r in capsule["payload"]["tables"]["products"]["rows"]
                                if json.loads(r["values"])["id"] == identity("products"))
                c.execute(sa.text("UPDATE public.products SET title=:title WHERE id=:id"),
                          {"title": original["title"], "id": identity("products")})


def test_cli_private_wrapper_and_safe_failures(capsule, tmp_path, monkeypatch, capsys):
    path = Path(__file__).resolve().parents[2] / "scripts/transfer_production_state.py"
    spec = importlib.util.spec_from_file_location("bridge_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main(["source-sql"]) == 0
    assert capsys.readouterr().out.strip() == bridge.source_sql()
    assert CIPHERTEXT not in bridge.source_sql()
    monkeypatch.setattr("sys.stdin", io.StringIO(canonical([{"capsule": capsule}])))
    out = tmp_path / "bundle.json"
    assert cli.main(["wrap-source-result", "--bundle", str(out)]) == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    printed = capsys.readouterr()
    assert CIPHERTEXT not in printed.out + printed.err
    assert "signed" not in printed.out and "media.fixture.invalid" not in printed.out
    bundle = strict_json(out.read_text())
    assert cli.main(["plan", "--bundle", str(out), "--expected-manifest-sha256",
                     bundle["manifest"]["manifest_sha256"]]) == 0
    capsys.readouterr()
    before = out.read_bytes()
    monkeypatch.setattr("sys.stdin", io.StringIO(canonical(capsule)))
    assert cli.main(["wrap-source-result", "--bundle", str(out)]) == 2
    assert out.read_bytes() == before
    absent = tmp_path / "must-not-exist"
    monkeypatch.setattr("sys.stdin", io.StringIO('{"payload":"' + CIPHERTEXT))
    assert cli.main(["wrap-source-result", "--bundle", str(absent)]) == 2
    assert not absent.exists()
    printed = capsys.readouterr()
    assert CIPHERTEXT not in printed.out + printed.err
    with pytest.raises(ValueError):
        strict_json('{"x":1,"x":2}')