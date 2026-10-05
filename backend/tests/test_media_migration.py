"""No attached services: all byte adapters and PostgreSQL handles are injected."""
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from app.services.media_storage import StorageMissing
from app.state_transfer import media_migration as m, migrate_media as cli

PNG = b"\x89PNG\r\n\x1a\n"
NAMES = {k: "FIXTURE_TARGET_" + k.upper() for k in
         ("endpoint", "bucket", "access_key", "secret_key", "region", "path_style")}
PRIVATE = "PRIVATE_NEVER_OUTPUT"


class Memory:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls = []
        self.corrupt_readback = False
        self.fail_put = False

    def get(self, key, size):
        self.calls.append(("get", key))
        if key not in self.values:
            raise StorageMissing()
        value = self.values[key]
        return value + b"corrupt" if self.corrupt_readback and any(
            c == ("put", key) for c in self.calls) else value

    def put_missing(self, key, data):
        self.calls.append(("put", key))
        assert key not in self.values, "Never overwrite an existing object"
        if self.fail_put:
            raise RuntimeError(PRIVATE)
        self.values[key] = data

    def close(self):
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    rows, values = [], {}
    for i in range(17):
        identity = f"creative-{i:02}"
        data = PNG + identity.encode()
        sha = hashlib.sha256(data).hexdigest()
        rows.append({"id": identity, "sha256": sha, "size_bytes": len(data),
                     "render_status": "RENDERED"})
        values[f"creative/{identity}/{sha}.png"] = data
        (tmp_path / f"{identity}.png").write_bytes(data)
    for name, value in {
        "endpoint": "https://bucket.railway.example", "bucket": "media-fixture",
        "access_key": PRIVATE, "secret_key": PRIVATE, "region": "auto", "path_style": "true",
    }.items():
        monkeypatch.setenv(NAMES[name], value)
    monkeypatch.setenv("FIXTURE_SOURCE_DB", "postgresql+psycopg://test@localhost/fixture")
    monkeypatch.setattr(m.sa, "create_engine", lambda *a, **kw: SimpleNamespace(dispose=lambda: None))
    monkeypatch.setattr(m, "metadata", lambda e: m.bindings(rows))
    return tmp_path, rows, values


def invoke(setup, *, target=None, source=None, **kwargs):
    root, rows, values = setup
    target = target if target is not None else Memory()
    source = source if source is not None else Memory(values)
    result = m.run(database_env="FIXTURE_SOURCE_DB", roots=[root], target_envs=NAMES,
                   source_factory=lambda bindings: source,
                   target_factory=lambda config, bindings: target,
                   **{"execute": True, **kwargs})
    return result, source, target


def test_all_sources_and_targets_preflight_immediate_readback(setup):
    result, source, target = invoke(setup)
    assert result["success"] and result["terminal_stage"] == "MEDIA_CERTIFIED"
    assert result["media_certification"] == "PASS"
    assert source.calls == []
    assert [c[0] for c in target.calls] == ["get"] * 17 + ["put", "get"] * 17
    assert result["source_counts"] == {"LOCAL_VERIFIED": 17}
    assert result["target_counts"] == {"UPLOADED_VERIFIED": 17}
    assert result["target_put_attempts"] == 17 and result["target_reads"] == 34
    assert result["database_writes"] == 0 and result["publishing_admission"] == "NOT_GRANTED"
    assert PRIVATE not in json.dumps(result) and str(setup[0]) not in json.dumps(result)


def test_exact_replit_fallback_only_for_absent_locals(setup):
    root, rows, values = setup
    for row in rows[:5]:
        (root / f"{row['id']}.png").unlink()
    result, source, target = invoke(setup)
    assert result["success"] and result["source_counts"] == {"REPLIT_VERIFIED": 5, "LOCAL_VERIFIED": 12}
    assert {key for op, key in source.calls} == {
        f"creative/{r['id']}/{r['sha256']}.png" for r in rows[:5]}
    assert len(source.calls) == 5


@pytest.mark.parametrize("case", ["corrupt", "duplicate", "symlink", "hardlink", "size", "parent-link"])
def test_unsafe_local_never_falls_back_or_constructs_target(setup, case, monkeypatch):
    root, rows, _ = setup
    path = root / f"{rows[0]['id']}.png"
    if case == "corrupt":
        path.write_bytes(b"x" * path.stat().st_size)
    elif case == "size":
        path.write_bytes(path.read_bytes() + b"more")
    elif case == "duplicate":
        other = root / f"creative/{rows[0]['id']}/{rows[0]['sha256']}.png"
        other.parent.mkdir(parents=True)
        other.write_bytes(path.read_bytes())
    elif case == "symlink":
        path.unlink()
        path.symlink_to(root / f"{rows[1]['id']}.png")
    elif case == "hardlink":
        os.link(path, root / "alias.png")
    else:
        (root / "creative").symlink_to(root, target_is_directory=True)
    result, source, target = invoke(setup)
    assert not result["success"] and result["terminal_stage"] == "SOURCE_PREFLIGHT"
    assert source.calls == target.calls == []
    assert result["target_put_attempts"] == 0


@pytest.mark.parametrize("bad", [None, b"wrong", PNG + b"wrong"])
def test_missing_or_corrupt_replit_refuses_before_any_target_probe(setup, bad):
    root, rows, values = setup
    (root / f"{rows[-1]['id']}.png").unlink()
    source = Memory(values)
    key = f"creative/{rows[-1]['id']}/{rows[-1]['sha256']}.png"
    if bad is None:
        del source.values[key]
    else:
        source.values[key] = bad
    result, source, target = invoke(setup, source=source)
    assert not result["success"] and result["terminal_stage"] == "SOURCE_PREFLIGHT"
    assert len(source.calls) == 1 and target.calls == []


def test_conflict_anywhere_preflights_all_keys_and_never_writes(setup):
    target = Memory({next(iter(setup[2])): b"bad"})
    result, _, target = invoke(setup, target=target)
    assert not result["success"] and result["terminal_stage"] == "TARGET_PREFLIGHT"
    assert result["target_counts"] == {"CONFLICT": 1, "MISSING": 16}
    assert len(target.calls) == 17 and all(op == "get" for op, key in target.calls)


def test_dry_run_does_not_put_and_does_not_certify_missing_objects(setup):
    result, _, target = invoke(setup, dry_run=True, execute=False)
    assert result["success"] and result["terminal_stage"] == "DRY_RUN_COMPLETE"
    assert result["media_certification"] == "NOT_GRANTED"
    assert result["target_counts"] == {"MISSING": 17}
    assert result["target_put_attempts"] == 0 and len(target.calls) == 17


def test_reauthorized_rerun_idempotent_and_stable_fingerprints(setup):
    first, _, target = invoke(setup)
    target.calls.clear()
    # This is a separate explicit authorized invocation, not a command retry.
    second, _, target = invoke(setup, target=target)
    assert second["target_counts"] == {"VERIFIED_EXISTING": 17}
    assert second["target_put_attempts"] == 0 and len(target.calls) == 17
    for name in ("source_fingerprint", "target_fingerprint", "transfer_fingerprint"):
        assert first[name] == second[name]


@pytest.mark.parametrize("failure,stage,status", [
    ("put", "TARGET_WRITE", "PUT_ATTEMPTED_UNCONFIRMED"),
    ("readback", "TARGET_READBACK", "UPLOADED_UNVERIFIED"),
])
def test_partial_write_stops_no_retry_and_no_admission(setup, failure, stage, status):
    target = Memory()
    target.fail_put = failure == "put"
    target.corrupt_readback = failure == "readback"
    result, _, target = invoke(setup, target=target)
    assert not result["success"] and result["terminal_stage"] == stage
    assert result["media_certification"] == result["publishing_admission"] == "NOT_GRANTED"
    assert result["target_put_attempts"] == 1
    assert result["objects"][0]["target_status"] == status
    assert PRIVATE not in json.dumps(result)


def test_concurrent_create_after_preflight_refuses_without_overwriting(setup):
    class RacingTarget(Memory):
        def put_missing(self, key, data):
            self.values[key] = b"concurrently-created-do-not-touch"
            super().put_missing(key, data)
    target = RacingTarget()
    result, _, _ = invoke(setup, target=target)
    assert not result["success"] and result["terminal_stage"] == "TARGET_WRITE"
    assert result["target_put_attempts"] == 1
    assert len(target.values) == 1
    assert next(iter(target.values.values())) == b"concurrently-created-do-not-touch"


@pytest.mark.parametrize("change", ["count", "extra", "sha", "size", "state", "duplicate"])
def test_metadata_requires_exact_17_complete_valid_bindings(setup, change):
    rows = setup[1]
    if change == "count":
        rows.pop()
    elif change == "extra":
        rows.append(dict(rows[0], id="extra"))
    elif change == "sha":
        rows[0]["sha256"] = "INVALID"
    elif change == "size":
        rows[0]["size_bytes"] = None
    elif change == "state":
        rows[0]["render_status"] = "PENDING"
    else:
        rows[1]["id"] = rows[0]["id"]
    result, source, target = invoke(setup)
    assert not result["success"] and result["terminal_stage"] == "READ_ONLY_METADATA"
    assert result["objects"] == [] and source.calls == target.calls == []


@pytest.mark.parametrize("missing", list(NAMES))
def test_no_ambient_settings_or_target_defaults(setup, monkeypatch, missing):
    monkeypatch.delenv(NAMES[missing])
    result, source, target = invoke(setup)
    assert not result["success"] and result["terminal_stage"] == "CONFIGURATION"
    assert result["database_transactions"] == 0 and source.calls == target.calls == []


def test_gate_precedes_every_access(monkeypatch):
    def forbidden(*a, **k):
        pytest.fail("Unauthorized access")
    monkeypatch.setattr(m, "_env", forbidden)
    monkeypatch.setattr(m, "local_roots", forbidden)
    monkeypatch.setattr(m.sa, "create_engine", forbidden)
    r = m.run(database_env="UNREAD", roots=["UNREAD"], target_envs={})
    assert not r["success"] and r["terminal_stage"] == "EXECUTION_GATE"


def test_ack_must_be_exact_and_is_not_a_persistent_execution_switch(setup, monkeypatch):
    monkeypatch.setenv("FIXTURE_ACK", m.ACK)
    r, _, _ = invoke(setup, execute=False, execution_env="FIXTURE_ACK")
    assert r["success"]
    monkeypatch.setenv("FIXTURE_ACK", m.ACK + " ")
    r, _, _ = invoke(setup, execute=False, execution_env="FIXTURE_ACK")
    assert r["terminal_stage"] == "EXECUTION_GATE" and not r["success"]
    r, _, _ = invoke(setup, dry_run=True, execute=True)
    assert r["terminal_stage"] == "EXECUTION_GATE"


def test_cli_never_echoes_arguments_or_secrets(capsys):
    assert cli.main(["--unknown", PRIVATE]) == 2
    output = capsys.readouterr()
    assert output.err == "" and PRIVATE not in output.out
    assert json.loads(output.out)["terminal_stage"] == "ARGUMENTS"


def test_digest_layout_and_orphans_do_not_trigger_scans(setup):
    root, rows, _ = setup
    row = rows[0]
    path = root / f"creative/{row['id']}/{row['sha256']}.png"
    path.parent.mkdir(parents=True)
    (root / f"{row['id']}.png").rename(path)
    (root / "unrelated.png").write_bytes(b"not-read")
    assert invoke(setup)[0]["success"]
