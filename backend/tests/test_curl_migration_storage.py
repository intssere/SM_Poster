"""Offline contract tests for the manual curl/SigV4 migration target."""
import io
import json
import os
import stat
from types import SimpleNamespace

import pytest

from app.services.media_storage import StorageMissing, StorageUnavailable
from app.services.s3_media_storage import S3Config
from app.state_transfer import curl_migration_storage as curl_target
from app.state_transfer import media_migration as migration
from app.state_transfer import migrate_media as cli


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64
KEY = "creative/fixture/" + "a" * 64 + ".png"
ACCESS = "ACCESS_PRIVATE_SENTINEL"
SECRET = "SECRET_PRIVATE_SENTINEL"
CONFIG = S3Config(
    "https://objects.example.test",
    "fixture-bucket",
    ACCESS,
    SECRET,
    region="auto",
    path_style=False,
)
BINDINGS = [{"key": KEY}]


class FakeProcess:
    def __init__(self, body=b"", status=200, returncode=0):
        self.stdout = io.BytesIO(body)
        self.stderr = io.BytesIO(str(status).encode("ascii"))
        self.returncode = returncode
        self.killed = False
        self.input = None

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True

    def communicate(self, input=None, timeout=None):
        self.input = input
        return b"", self.stderr.read()


class CaptureFactory:
    def __init__(self, *, body=PNG, status=200, returncode=0, fail=False):
        self.body = body
        self.status = status
        self.returncode = returncode
        self.fail = fail
        self.calls = []
        self.config_paths = []
        self.config_contents = []
        self.config_modes = []
        self.processes = []

    def __call__(self, argv, **kwargs):
        config_path = argv[argv.index("--config") + 1]
        self.config_paths.append(config_path)
        self.config_contents.append(open(config_path, "rb").read())
        self.config_modes.append(stat.S_IMODE(os.stat(config_path).st_mode))
        self.calls.append((list(argv), dict(kwargs)))
        if self.fail:
            raise RuntimeError("PRIVATE_SUBPROCESS_FAILURE")
        process = FakeProcess(self.body, self.status, self.returncode)
        self.processes.append(process)
        return process


def make_target(factory, config=CONFIG):
    return curl_target.CurlS3ExactTarget(
        config, BINDINGS, curl_path="/usr/bin/curl", popen_factory=factory
    )


def test_capability_check_is_local_and_requires_https_and_sigv4(monkeypatch):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="curl 8.0 Protocols: http https", stderr="")
        return SimpleNamespace(returncode=0, stdout="  --aws-sigv4 <provider1[:provider2[:region[:service]]]>", stderr="")

    monkeypatch.setattr(curl_target.shutil, "which", lambda name: "/usr/bin/curl")
    assert curl_target.verify_curl_sigv4_capability(runner=runner) == "/usr/bin/curl"
    assert [call[0] for call in calls] == [
        ["/usr/bin/curl", "--version"],
        ["/usr/bin/curl", "--help", "all"],
    ]
    assert all(call[1]["env"].get("PATH") is not None for call in calls)


def test_curl_capability_guard_precedes_configuration_db_and_storage(monkeypatch):
    def blocked():
        raise RuntimeError("NO_CURL")

    def forbidden(*args, **kwargs):
        pytest.fail("Database/config access occurred before curl capability gate")

    monkeypatch.setattr(curl_target, "verify_curl_sigv4_capability", blocked)
    monkeypatch.setattr(migration, "target_config", forbidden)
    monkeypatch.setattr(migration.sa, "create_engine", forbidden)
    result = migration.run(
        database_env="UNREAD",
        roots=["UNREAD"],
        target_envs={},
        dry_run=True,
        target_transport="curl",
    )
    assert not result["success"]
    assert result["terminal_stage"] == "TARGET_TRANSPORT"
    assert result["database_transactions"] == 0
    assert result["target_put_attempts"] == 0


def test_cli_defaults_to_boto3_and_accepts_explicit_curl(monkeypatch, capsys):
    captured = []

    def fake_run(**kwargs):
        captured.append(kwargs["target_transport"])
        return {"success": True}

    monkeypatch.setattr(migration, "run", fake_run)
    args = [
        "--database-env", "SOURCE_DB",
        "--source-root", "/tmp/source",
        "--target-endpoint-env", "ENDPOINT",
        "--target-bucket-env", "BUCKET",
        "--target-access-key-env", "ACCESS",
        "--target-secret-key-env", "SECRET",
        "--target-region-env", "REGION",
        "--target-path-style-env", "STYLE",
        "--dry-run",
    ]
    assert cli.main(args) == 0
    assert cli.main(args + ["--target-transport", "curl"]) == 0
    capsys.readouterr()
    assert captured == ["boto3", "curl"]


def test_virtual_host_and_path_style_urls_are_exact_and_https_only():
    virtual = make_target(CaptureFactory())
    assert virtual._url(KEY) == "https://fixture-bucket.objects.example.test/" + KEY

    path_config = S3Config(
        "https://objects.example.test/base",
        "fixture-bucket",
        ACCESS,
        SECRET,
        region="auto",
        path_style=True,
    )
    path = make_target(CaptureFactory(), path_config)
    assert path._url(KEY) == "https://objects.example.test/base/fixture-bucket/" + KEY

    with pytest.raises(Exception):
        virtual._url("creative/unapproved/key.png")

    with pytest.raises(Exception):
        curl_target.CurlS3ExactTarget(
            S3Config("http://objects.example.test", "fixture-bucket", ACCESS, SECRET),
            BINDINGS,
            curl_path="/usr/bin/curl",
            popen_factory=CaptureFactory(),
        )


def test_get_uses_private_config_without_secret_argv_and_cleans_it():
    factory = CaptureFactory(body=PNG, status=206)
    target = make_target(factory)
    assert target.get(KEY, len(PNG)) == PNG

    argv, kwargs = factory.calls[0]
    rendered = "\0".join(argv) + json.dumps(kwargs, default=str)
    assert ACCESS not in rendered and SECRET not in rendered
    assert factory.config_modes == [0o600]
    assert ACCESS.encode() in factory.config_contents[0]
    assert SECRET.encode() in factory.config_contents[0]
    assert "--max-redirs" in argv and argv[argv.index("--max-redirs") + 1] == "0"
    assert "--retry" in argv and argv[argv.index("--retry") + 1] == "0"
    assert "--proto" in argv and argv[argv.index("--proto") + 1] == "=https"
    assert kwargs["env"].get("AWS_ACCESS_KEY_ID") is None
    assert kwargs["env"].get("HTTPS_PROXY") is None
    assert all(not os.path.exists(path) for path in factory.config_paths)


def test_get_404_is_missing_and_failure_text_never_leaks_secrets():
    factory = CaptureFactory(body=b"", status=404)
    target = make_target(factory)
    with pytest.raises(StorageMissing) as exc:
        target.get(KEY, len(PNG))
    assert ACCESS not in str(exc.value) and SECRET not in str(exc.value)
    assert all(not os.path.exists(path) for path in factory.config_paths)


def test_bounded_get_reads_expected_plus_one_and_stops_oversize():
    body = PNG + b"oversize"
    factory = CaptureFactory(body=body, status=206)
    target = make_target(factory)
    result = target.get(KEY, len(PNG))
    assert len(result) == len(PNG) + 1
    assert factory.processes[0].killed
    assert all(not os.path.exists(path) for path in factory.config_paths)


def test_put_is_conditional_exact_key_and_passes_bytes_only_on_stdin():
    factory = CaptureFactory(body=b"", status=200)
    target = make_target(factory)
    target.put_missing(KEY, PNG)

    argv, kwargs = factory.calls[0]
    rendered = "\0".join(argv) + json.dumps(kwargs, default=str)
    assert ACCESS not in rendered and SECRET not in rendered
    assert argv[argv.index("--request") + 1] == "PUT"
    assert "If-None-Match: *" in argv
    assert "Content-Type: image/png" in argv
    assert argv[-1] == "https://fixture-bucket.objects.example.test/" + KEY
    assert factory.processes[0].input == PNG
    assert all(not os.path.exists(path) for path in factory.config_paths)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 409, 412, 500])
def test_put_redirect_auth_conflict_and_precondition_fail_closed(status):
    factory = CaptureFactory(body=b"", status=status)
    target = make_target(factory)
    with pytest.raises(StorageUnavailable) as exc:
        target.put_missing(KEY, PNG)
    assert ACCESS not in str(exc.value) and SECRET not in str(exc.value)
    assert all(not os.path.exists(path) for path in factory.config_paths)


def test_subprocess_failure_cleans_private_config_and_redacts_exception():
    factory = CaptureFactory(fail=True)
    target = make_target(factory)
    with pytest.raises(StorageUnavailable) as exc:
        target.get(KEY, len(PNG))
    assert "PRIVATE_SUBPROCESS_FAILURE" not in str(exc.value)
    assert ACCESS not in str(exc.value) and SECRET not in str(exc.value)
    assert factory.config_modes == [0o600]
    assert all(not os.path.exists(path) for path in factory.config_paths)


def test_close_prevents_further_operations():
    target = make_target(CaptureFactory())
    target.close()
    with pytest.raises(Exception):
        target.get(KEY, len(PNG))
