"""Disposable PostgreSQL: curl error refusal leaves every business row intact."""
import pytest
import sqlalchemy as sa

from app.state_transfer import curl_migration_storage as c, media_migration as m, transfer
from tests.test_media_continuity_postgres import prepared, source, pytestmark
from tests.test_media_migration_postgres import configured
from tests.test_media_migration import NAMES
from tests.test_curl_migration_storage import CaptureFactory


@pytest.mark.parametrize("body,success", [
    (b"<Error><Code>NoSuchKey</Code></Error>", True),
    (b"<Error><Code>NoSuchBucket</Code></Error>", False),
    (b"<Error><Code>AccessDenied</Code></Error>", False),
    (b"", False), (b"<html>missing</html>", False),
    (b"x" * (c.MAX_ERROR_BYTES + 1), False),
])
def test_postgres_curl_error_dry_run_readonly_and_business_state_unchanged(configured, monkeypatch, body, success):
    engine, root, rows, _ = configured
    before = transfer.export_source(engine)
    factory = CaptureFactory(body=body, status=404)
    monkeypatch.setattr(c, "verify_curl_sigv4_capability", lambda: "/fixture/curl")
    begins, statements = [], []
    def begin(connection):
        begins.append(True)
    def observe(connection, cursor, statement, parameters, context, many):
        statements.append(statement)
    sa.event.listen(engine, "begin", begin)
    sa.event.listen(engine, "before_cursor_execute", observe)
    try:
        result = m.run(database_env="MEDIA_SOURCE_FIXTURE", roots=[root], target_envs=NAMES,
                       dry_run=True, target_transport="curl",
                       target_factory=lambda cfg, bindings: c.CurlS3ExactTarget(
                           cfg, bindings, curl_path="/fixture/curl", popen_factory=factory))
    finally:
        sa.event.remove(engine, "begin", begin)
        sa.event.remove(engine, "before_cursor_execute", observe)
    after = transfer.export_source(engine)
    assert before["rows"] == after["rows"]
    assert before["manifest"]["tables"] == after["manifest"]["tables"]
    assert begins == [True] and "SET TRANSACTION READ ONLY" in statements
    assert all(s.split()[0] in {"SELECT", "SET", "SHOW"} for s in statements)
    assert result["success"] is success
    assert result["database_transactions"] == 1 and result["database_writes"] == 0
    assert result["target_reads"] == (17 if success else 1)
    assert result["target_put_attempts"] == 0
    assert result["publishing_admission"] == "NOT_GRANTED"
