"""All DB tests are fenced by run_isolated_readiness_tests.py before collection."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import stat

import pytest
import sqlalchemy as sa

from app.state_transfer import transfer
from app.state_transfer.catalog import Refused, canonical, digest, ordered_rows
from app.state_transfer.policy import SOURCE, TARGET_ONLY
from tests.state_transfer_fixture import CIPHERTEXT, identity, seed_inventory
from tests.test_readiness_execution_admission_0032 import _isolated_database, pytestmark


@pytest.fixture(scope="module")
def source():
    with _isolated_database("0031") as (engine, _):
        seed_inventory(engine)
        yield engine


@pytest.fixture(scope="module")
def bundle(source):
    return transfer.export_source(source)


def fingerprint(bundle):
    return bundle["manifest"]["manifest_sha256"]


def resign(bundle):
    bundle["manifest"]["manifest_sha256"] = digest(
        {k: v for k, v in bundle["manifest"].items() if k != "manifest_sha256"})
    return bundle


def test_complete_inventory_and_readonly_export(source, bundle):
    assert len(SOURCE) == 48
    assert sum(p.count for p in SOURCE.values()) == 12459
    snapshot = bundle["manifest"]["snapshot"]
    assert snapshot["read_only"] == "on"
    assert snapshot["isolation"] == "repeatable read"
    assert snapshot["transaction_snapshot"] and snapshot["transaction_time"]
    assert bundle["manifest"]["source_revision"] == "0031"
    assert bundle["rows"]["pinterest_oauth_states"] == []
    assert "state_hash" not in canonical(bundle["rows"]["pinterest_oauth_states"])
    assert CIPHERTEXT in canonical(bundle["rows"]["pinterest_connections"])
    assert "alembic_version" not in bundle["rows"]
    second = transfer.export_source(source)
    assert second["rows"] == bundle["rows"]
    assert second["manifest"]["tables"] == bundle["manifest"]["tables"]
    with source.connect() as c:
        with pytest.raises(sa.exc.DBAPIError):
            with transfer.transaction(source) as readonly:
                readonly.execute(sa.text("UPDATE public.stores SET name='forbidden'"))


def test_roundtrip_certification_history_and_repeat_refusal(bundle, capsys):
    with _isolated_database("0034") as (engine, _):
        plan = transfer.import_target(engine, bundle, fingerprint(bundle), plan=True)
        assert plan["writes"] == 0 and CIPHERTEXT not in canonical(plan)
        assert "signed" not in canonical(plan)
        result = transfer.import_target(engine, bundle, fingerprint(bundle))
        assert result["database_certification"] == "PASS"
        assert transfer.certify_target(engine, bundle, fingerprint(bundle)) == result
        with engine.connect() as c:
            assert dict(c.execute(sa.text(
                "SELECT status::text,count(*) FROM public.pin_publications GROUP BY status"
            )).all()) == {"PUBLISHED": 4, "CANCELLED": 5}
            assert c.scalar(sa.text(
                "SELECT count(*) FROM public.publication_attempts WHERE status='UNKNOWN'"
            )) == 3
            assert c.scalar(sa.text("SELECT count(*) FROM public.publication_reconciliation_events")) == 7
            assert all(transfer.count(c, name) == 0 for name in TARGET_ONLY)
            assert transfer.count(c, "pinterest_oauth_states") == 0
            assert c.scalar(sa.text(
                "SELECT json_typeof(render_spec) FROM public.pin_creatives WHERE id=:id"
            ), {"id": identity("pin_creatives")}) == "null"
            assert c.scalar(sa.text(
                "SELECT price_min::text FROM public.products WHERE id=:id"
            ), {"id": identity("products")}) == "1234567.12"
            assert c.scalar(sa.text(
                "SELECT shopify_data->>'precise' FROM public.products WHERE id=:id"
            ), {"id": identity("products")}) == "1234567890123.12345678901234567890"
        with pytest.raises(Refused, match="empty|scaffold"):
            transfer.import_target(engine, bundle, fingerprint(bundle))
        transfer.certify_target(engine, bundle, fingerprint(bundle))
    assert CIPHERTEXT not in str(capsys.readouterr())


@pytest.mark.parametrize("change", [
    "UPDATE public.buffer_pilot_activations SET status='ACTIVE',consumed_at=NULL WHERE id=(SELECT id FROM public.buffer_pilot_activations LIMIT 1)",
    "UPDATE public.publication_dispatch_authorizations SET status='ACTIVE',consumed_at=NULL WHERE id=(SELECT id FROM public.publication_dispatch_authorizations LIMIT 1)",
    "UPDATE public.routine_dispatch_permits SET status='ACTIVE',consumed_at=NULL WHERE id=(SELECT id FROM public.routine_dispatch_permits LIMIT 1)",
    "UPDATE public.pin_publications SET status='PUBLISHING'",
    "UPDATE public.pin_publications SET status='PUBLISH_UNKNOWN'",
    "UPDATE public.routine_publishing_runs SET status='RUNNING' WHERE id=(SELECT id FROM public.routine_publishing_runs LIMIT 1)",
    "UPDATE public.routine_publishing_control SET state='LIVE'",
    "UPDATE public.pinterest_autonomous_execution_runs SET status='STARTED'",
    "UPDATE public.buffer_pilot_activations SET consumed_at=NULL",
    "DELETE FROM public.audit_logs",
    "ALTER TABLE public.stores ADD COLUMN unexpected text",
    "ALTER TABLE public.stores ENABLE ROW LEVEL SECURITY",
    "CREATE TABLE public.unreviewed_table (id text)",
    "UPDATE public.alembic_version SET version_num='0030'",
    "ALTER TYPE draftstatus ADD VALUE 'UNREVIEWED'",
    "ALTER TABLE public.products DROP CONSTRAINT products_store_id_fkey",
])
def test_source_refuses_changed_or_open_state(source, change):
    # Savepoints are invisible to the independent export transaction, so use a
    # disposable transaction on the same connection via a tiny engine adapter.
    with source.connect() as c:
        tx = c.begin()
        c.execute(sa.text(change))
        # Test the actual export preconditions without starting a nested DB tx.
        from contextlib import contextmanager

        @contextmanager
        def existing_transaction(*_, **__):
            yield c

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(transfer, "transaction", existing_transaction)
            with pytest.raises(Refused):
                transfer.export_source(source)
        tx.rollback()


@pytest.mark.parametrize("change", [
    "rows", "manifest", "excluded", "expected", "order", "media", "columns", "sql_null",
])
def test_bundle_tamper_refuses(bundle, change):
    bad = deepcopy(bundle)
    expected = fingerprint(bundle)
    if change == "rows":
        bad["rows"]["stores"][0]["values"] += " "
    elif change == "manifest":
        bad["manifest"]["source_revision"] = "0030"
    elif change == "excluded":
        bad["rows"]["pinterest_oauth_states"] = [{}]
    elif change == "expected":
        expected = "0" * 64
    elif change == "order":
        bad["manifest"]["dependency_order"].reverse()
        resign(bad)
        expected = fingerprint(bad)
    elif change == "media":
        bad["manifest"]["media"]["object_bytes_copied"] = True
        resign(bad)
        expected = fingerprint(bad)
    elif change in {"columns", "sql_null"}:
        row = bad["rows"]["stores"][0]
        values = json.loads(row["values"])
        if change == "columns":
            values["unreviewed"] = "x"
        else:
            row["sql_nulls"] = ["name"]
        row["values"] = canonical(values)
        bad["manifest"]["tables"]["stores"]["content_sha256"] = digest(bad["rows"]["stores"])
        bad["manifest"]["tables"]["stores"]["source_content_sha256"] = digest(bad["rows"]["stores"])
        resign(bad)
        expected = fingerprint(bad)
    with _isolated_database("0034") as (engine, _):
        with pytest.raises(Refused):
            transfer.import_target(engine, bad, expected)
        with engine.connect() as c:
            assert transfer.count(c, "stores") == 0


@pytest.mark.parametrize("change", [
    "INSERT INTO public.stores VALUES ('existing','existing','fixture.invalid','US',now())",
    "UPDATE public.routine_publishing_control SET state='LIVE'",
    "ALTER TABLE public.stores ADD COLUMN unexpected text",
    "UPDATE public.alembic_version SET version_num='0033'",
])
def test_target_preflight_refusal(bundle, change):
    with _isolated_database("0034") as (engine, _):
        with engine.begin() as c:
            c.execute(sa.text(change))
        with pytest.raises(Refused):
            transfer.import_target(engine, bundle, fingerprint(bundle))


def test_failure_rolls_back_entire_import(bundle, monkeypatch):
    with _isolated_database("0034") as (engine, _):
        def fail(*_):
            raise Refused("Injected final certification failure")

        with monkeypatch.context() as patch:
            patch.setattr(transfer, "certify_connection", fail)
            with pytest.raises(Refused):
                transfer.import_target(engine, bundle, fingerprint(bundle))
        with engine.connect() as c:
            assert all(transfer.count(c, n) == 0 for n in SOURCE if n != "routine_publishing_control")
            assert c.scalar(sa.text("SELECT state FROM public.routine_publishing_control")) == "PAUSED"
        assert transfer.import_target(engine, bundle, fingerprint(bundle))["database_certification"] == "PASS"


def test_certification_detects_target_content_change(bundle):
    with _isolated_database("0034") as (engine, _):
        transfer.import_target(engine, bundle, fingerprint(bundle))
        with engine.begin() as c:
            c.execute(sa.text("UPDATE public.stores SET name='changed'"))
        with pytest.raises(Refused, match="content differs"):
            transfer.certify_target(engine, bundle, fingerprint(bundle))


def test_cli_safe_output_failure_and_private_bundle(bundle, tmp_path, capsys):
    script = Path(__file__).resolve().parents[2] / "scripts/transfer_production_state.py"
    spec = importlib.util.spec_from_file_location("transfer_cli", script)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    path = tmp_path / "sensitive.json"
    transfer.write_bundle(bundle, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        transfer.write_bundle(bundle, path)
    assert cli.main(["plan", "--bundle", str(path),
                     "--expected-manifest-sha256", fingerprint(bundle)]) == 0
    output = capsys.readouterr()
    assert CIPHERTEXT not in output.out and "signed" not in output.out
    assert cli.main(["plan", "--bundle", str(path),
                     "--expected-manifest-sha256", "wrong"]) == 2
    output = capsys.readouterr()
    assert CIPHERTEXT not in output.err


def test_cli_source_dry_run_and_target_plan_are_readonly(source, bundle, tmp_path, capsys):
    script = Path(__file__).resolve().parents[2] / "scripts/transfer_production_state.py"
    spec = importlib.util.spec_from_file_location("transfer_dry_cli", script)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    source_dsn = tmp_path / "synthetic-source-dsn"
    source_dsn.write_text(source.url.render_as_string(hide_password=False))
    output_path = tmp_path / "not-created.json"
    assert cli.main(["export", "--dsn-file", str(source_dsn), "--bundle", str(output_path),
                     "--dry-run"]) == 0
    assert not output_path.exists()
    private_path = tmp_path / "bundle.json"
    transfer.write_bundle(bundle, private_path)
    with _isolated_database("0034") as (engine, url):
        target_dsn = tmp_path / "synthetic-target-dsn"
        target_dsn.write_text(url)
        assert cli.main(["import", "--dry-run", "--dsn-file", str(target_dsn),
                         "--bundle", str(private_path),
                         "--expected-manifest-sha256", fingerprint(bundle)]) == 0
        with engine.connect() as c:
            assert transfer.count(c, "stores") == 0
    output = capsys.readouterr()
    assert CIPHERTEXT not in output.out and "signed" not in output.out


def test_nonpostgres_refusal():
    engine = sa.create_engine("sqlite://")
    with pytest.raises(Refused, match="PostgreSQL"):
        transfer.export_source(engine)


def test_snapshot_is_consistent_during_concurrent_disposable_write(source, bundle):
    changed = False
    with source.connect() as c:
        original = c.scalar(sa.text("SELECT name FROM public.stores"))

    def concurrent_write(_c, _cursor, statement, *_):
        nonlocal changed
        if not changed and statement.startswith("SELECT to_jsonb(t)"):
            changed = True
            with source.begin() as other:
                other.execute(sa.text("UPDATE public.stores SET name='concurrent-fixture'"))

    sa.event.listen(source, "before_cursor_execute", concurrent_write)
    try:
        exported = transfer.export_source(source)
        assert changed
        assert exported["rows"] == bundle["rows"]
    finally:
        sa.event.remove(source, "before_cursor_execute", concurrent_write)
        with source.begin() as c:
            c.execute(sa.text("UPDATE public.stores SET name=:name"), {"name": original})


def test_self_reference_order_and_cycle_refusal(bundle):
    refs = bundle["manifest"]["self_dependencies"]["content_revisions"]
    ordered = ordered_rows(bundle["rows"]["content_revisions"], refs)
    positions = {json.loads(r["values"])["id"]: i for i, r in enumerate(ordered)}
    for row in ordered:
        values = json.loads(row["values"])
        if values["parent_revision_id"]:
            assert positions[values["parent_revision_id"]] < positions[values["id"]]
    cyclic = deepcopy(bundle["rows"]["content_revisions"][:2])
    a, b = [json.loads(r["values"]) for r in cyclic]
    a["parent_revision_id"], b["parent_revision_id"] = b["id"], a["id"]
    cyclic[0]["values"], cyclic[1]["values"] = canonical(a), canonical(b)
    with pytest.raises(Refused, match="Cyclic self-reference"):
        ordered_rows(cyclic, refs)


@pytest.mark.parametrize("name", ["routine_autonomous_batches", "management_readiness_admissions"])
def test_target_only_nonempty_refuses(bundle, name):
    with _isolated_database("0034") as (engine, _):
        with engine.begin() as c:
            if name == "routine_autonomous_batches":
                c.execute(sa.text("INSERT INTO public.routine_autonomous_batches (id) VALUES ('offline')"))
            else:
                c.execute(sa.text(
                    "INSERT INTO public.management_readiness_admissions "
                    "(operation,release_commit_sha,release_tree_sha,descriptor_sha256,grant_id,actor_hash) "
                    "VALUES ('object_storage_readiness_v1',:commit,:tree,:digest,'offline',:actor)"
                ), {"commit": "a"*40, "tree": "b"*40, "digest": "c"*64, "actor": "d"*64})
        with pytest.raises(Refused, match="Target must be empty"):
            transfer.import_target(engine, bundle, fingerprint(bundle))


def test_resigned_open_bundle_and_driver_errors_are_safe(bundle, monkeypatch, capsys):
    bad = deepcopy(bundle)
    row = bad["rows"]["routine_dispatch_permits"][0]
    values = json.loads(row["values"])
    values["status"] = "ACTIVE"
    row["values"] = canonical(values)
    bad["rows"]["routine_dispatch_permits"].sort(key=canonical)
    info = bad["manifest"]["tables"]["routine_dispatch_permits"]
    info["content_sha256"] = info["source_content_sha256"] = digest(bad["rows"]["routine_dispatch_permits"])
    resign(bad)
    with pytest.raises(Refused, match="open authorization"):
        transfer.verify_bundle(bad, fingerprint(bad))
    with _isolated_database("0034") as (engine, _):
        def driver_error(*_, **__):
            raise sa.exc.DBAPIError("INSERT", {"payload": CIPHERTEXT}, Exception(CIPHERTEXT))

        with monkeypatch.context() as patch:
            patch.setattr(transfer, "certify_connection", driver_error)
            with pytest.raises(Refused) as caught:
                transfer.import_target(engine, bundle, fingerprint(bundle))
        assert CIPHERTEXT not in str(caught.value)
        with engine.connect() as c:
            assert transfer.count(c, "stores") == 0
    assert CIPHERTEXT not in str(capsys.readouterr())


def test_missing_foundational_reference_refuses_even_on_target_without_fk(bundle):
    bad = deepcopy(bundle)
    row = bad["rows"]["products"][0]
    values = json.loads(row["values"])
    values["store_id"] = "missing-foundation-parent"
    row["values"] = canonical(values)
    bad["rows"]["products"].sort(key=canonical)
    info = bad["manifest"]["tables"]["products"]
    info["content_sha256"] = info["source_content_sha256"] = digest(bad["rows"]["products"])
    resign(bad)
    with _isolated_database("0034") as (engine, _):
        with pytest.raises(Refused, match="foreign-key reference missing"):
            transfer.import_target(engine, bad, fingerprint(bad), plan=True)
        with pytest.raises(Refused, match="foreign-key reference missing"):
            transfer.import_target(engine, bad, fingerprint(bad))
        with engine.connect() as c:
            assert transfer.count(c, "stores") == 0