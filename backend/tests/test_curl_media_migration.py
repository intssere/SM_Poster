"""Full manual migration protocol over a memory curl process, never HTTP."""
import json
from types import SimpleNamespace

import pytest

from app.services.media_storage import StorageUnavailable
from app.state_transfer import curl_migration_storage as c, media_migration as m
from app.state_transfer import migrate_media as cli, migration_storage
from tests.curl_migration_fixture import CurlMemory
from tests.test_media_migration import setup, NAMES, Memory, PRIVATE, invoke as boto_invoke

BINARY = SimpleNamespace(path="/fixture/curl", verify=lambda: None)


@pytest.fixture
def curl_wire(monkeypatch):
    wire = CurlMemory()
    monkeypatch.setattr(c, "check_curl_capability", lambda: BINARY)
    monkeypatch.setattr(c.subprocess, "Popen", wire)
    return wire


def invoke_curl(setup, **kwargs):
    root, _, values = setup
    return m.run(database_env="FIXTURE_SOURCE_DB", roots=[root], target_envs=NAMES,
                 source_factory=lambda bindings: Memory(values), target_transport="curl",
                 **{"execute": True, **kwargs})


def test_opt_in_dry_run_never_puts_and_matches_boto_protocol(setup, curl_wire):
    curl = invoke_curl(setup, execute=False, dry_run=True)
    boto = boto_invoke(setup, execute=False, dry_run=True)[0]
    assert curl == boto
    assert curl["target_counts"] == {"MISSING": 17} and curl["target_put_attempts"] == 0
    assert curl["database_writes"] == 0 and curl_wire.values == {}
    assert [m for m, _ in curl_wire.calls] == ["GET"] * 17
    assert PRIVATE not in json.dumps(curl)


def test_conditional_put_readback_and_verified_existing_parity(setup, curl_wire):
    curl = invoke_curl(setup)
    boto = boto_invoke(setup)[0]
    assert curl == boto and curl["media_certification"] == "PASS"
    assert [method for method, _ in curl_wire.calls] == ["GET"] * 17 + ["PUT", "GET"] * 17
    assert curl["publishing_admission"] == "NOT_GRANTED"
    curl_wire.calls.clear()
    again = invoke_curl(setup)
    assert again["target_put_attempts"] == 0
    assert again["target_counts"] == {"VERIFIED_EXISTING": 17}
    assert len(curl_wire.calls) == 17
    for key in ("source_fingerprint", "target_fingerprint", "transfer_fingerprint"):
        assert again[key] == curl[key]
    assert all(not record[3].parent.exists() for record in curl_wire.records)


@pytest.mark.parametrize("corruption", ["signature", "sha", "size"])
def test_all_target_preflights_before_conflict_refusal(setup, curl_wire, corruption):
    key, data = next(iter(setup[-1].items()))
    broken = {
        "signature": b"NOTPNG!!" + data[8:],
        "sha": data[:-1] + b"!",
        "size": data + b"too-large",
    }[corruption]
    curl_wire.values[key] = broken
    result = invoke_curl(setup)
    assert not result["success"] and result["terminal_stage"] == "TARGET_PREFLIGHT"
    assert result["target_counts"] == {"CONFLICT": 1, "MISSING": 16}
    assert result["target_put_attempts"] == 0
    assert [method for method, _ in curl_wire.calls] == ["GET"] * 17
    assert curl_wire.values == {key: broken}


def test_all_source_bytes_before_target_process(setup, curl_wire):
    root, rows, _ = setup
    (root / (rows[-1]["id"] + ".png")).write_bytes(b"invalid")
    result = invoke_curl(setup)
    assert not result["success"] and result["terminal_stage"] == "SOURCE_PREFLIGHT"
    assert curl_wire.calls == [] and result["target_put_attempts"] == 0


def test_race_and_readback_failure_never_retry_or_grant_admission(setup, curl_wire):
    curl_wire.race = True
    result = invoke_curl(setup)
    assert result["terminal_stage"] == "TARGET_WRITE" and not result["success"]
    assert result["target_put_attempts"] == 1 and curl_wire.values == {}
    assert result["objects"][0]["target_status"] == "PUT_ATTEMPTED_UNCONFIRMED"
    assert result["publishing_admission"] == "NOT_GRANTED"


def test_readback_corruption_stops_before_second_put(setup, curl_wire):
    curl_wire.corrupt_readback = True
    result = invoke_curl(setup)
    assert result["terminal_stage"] == "TARGET_READBACK" and not result["success"]
    assert result["target_put_attempts"] == 1 and len(curl_wire.values) == 1
    assert result["objects"][0]["target_status"] == "UPLOADED_UNVERIFIED"


def test_capability_refusal_before_db_root_or_storage_access(setup, monkeypatch):
    calls = []
    def refuse():
        calls.append("capability")
        raise StorageUnavailable(PRIVATE)
    monkeypatch.setattr(c, "check_curl_capability", refuse)
    monkeypatch.setattr(m, "target_config", lambda *a: pytest.fail("Configuration accessed"))
    monkeypatch.setattr(m, "local_roots", lambda *a: pytest.fail("Source accessed"))
    monkeypatch.setattr(m.sa, "create_engine", lambda *a, **kw: pytest.fail("Database accessed"))
    result = invoke_curl(setup)
    assert not result["success"] and result["terminal_stage"] == "TARGET_TRANSPORT_CAPABILITY"
    assert result["database_transactions"] == 0 and result["target_put_attempts"] == 0
    assert calls == ["capability"] and PRIVATE not in json.dumps(result)


def test_boto_remains_default_without_curl_dependency(setup, monkeypatch):
    monkeypatch.setattr(c, "check_curl_capability", lambda: pytest.fail("curl selected implicitly"))
    target = Memory()
    monkeypatch.setattr(migration_storage, "S3ExactTarget", lambda config, bindings: target)
    result = m.run(database_env="FIXTURE_SOURCE_DB", roots=[setup[0]], target_envs=NAMES,
                   dry_run=True)
    assert result["success"] and len(target.calls) == 17


def test_cli_explicit_curl_dry_run_and_invalid_selector_redaction(setup, curl_wire, capsys):
    args = ["--database-env", "FIXTURE_SOURCE_DB", "--source-root", str(setup[0]),
            "--target-transport", "curl", "--dry-run"]
    for key, name in NAMES.items():
        args += ["--target-" + key.replace("_", "-") + "-env", name]
    assert cli.main(args) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert output.err == "" and PRIVATE not in output.out and str(setup[0]) not in output.out
    assert result["target_put_attempts"] == 0 and result["success"]
    assert cli.main(["--target-transport", PRIVATE]) == 2
    output = capsys.readouterr()
    assert PRIVATE not in output.out and output.err == ""


def test_unknown_transport_refuses_without_access(setup):
    result = m.run(database_env="FIXTURE_SOURCE_DB", roots=[setup[0]], target_envs=NAMES,
                   execute=True, target_transport="other")
    assert result["terminal_stage"] == "TARGET_TRANSPORT_CAPABILITY"
    assert result["database_transactions"] == 0 and not result["success"]
