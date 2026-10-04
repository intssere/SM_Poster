"""Local inventory is byte-only, strictly bound to persisted IDs and hashes."""
import hashlib
import json

import pytest

from app.state_transfer import media_continuity as media
from app.state_transfer import inventory_media_continuity as cli
from app.services.media_storage import media_key


PNG = b"\x89PNG\r\n\x1a\ninventory-source"
SHA = hashlib.sha256(PNG).hexdigest()


def row(identity="creative-1", **kwargs):
    return {"id": identity, "sha256": SHA, "size_bytes": len(PNG),
            "render_status": "RENDERED", **kwargs}


def test_exact_id_sha_binding_and_idempotent_zero_network_plan(tmp_path):
    (tmp_path / "creative-1.png").write_bytes(PNG)
    first = media.compare([row()], [tmp_path], plan=True)
    assert first == media.compare([row()], [tmp_path], plan=True)
    assert first["complete"] is True and first["statuses"]["MATCHED"] == 1
    assert first["objects"] == [{"object_key": media_key("creative", "creative-1", SHA),
                                "sha256": SHA, "size_bytes": len(PNG)}]
    assert first["provider_calls"] == first["storage_writes"] == first["database_writes"] == 0
    assert first["publishing_admission"] == "NOT_GRANTED"
    assert str(tmp_path) not in json.dumps(first)
    assert "objects" not in media.compare([row()], [tmp_path])


@pytest.mark.parametrize("case,status", [
    ("missing", "MISSING"), ("wrong-id", "MISSING"), ("corrupt", "DIGEST_MISMATCH"),
    ("not-png", "DIGEST_MISMATCH"), ("size", "DIGEST_MISMATCH"),
    ("duplicate", "DUPLICATE"), ("symlink", "MISSING"),
    ("bad-sha", "UNSUPPORTED"), ("bad-id", "UNSUPPORTED"),
    ("unreadable", "UNSUPPORTED"),
])
def test_incomplete_inventory_never_certifies(tmp_path, case, status):
    item = row()
    if case not in ("missing", "wrong-id", "symlink"):
        (tmp_path / "creative-1.png").write_bytes(PNG)
    if case == "wrong-id":
        (tmp_path / "another-id.png").write_bytes(PNG)
    if case == "corrupt":
        (tmp_path / "creative-1.png").write_bytes(PNG + b"bad")
    if case == "not-png":
        (tmp_path / "creative-1.png").write_bytes(b"x" * len(PNG))
    if case == "size":
        item["size_bytes"] += 1
    if case == "duplicate":
        nested = tmp_path / "nested"
        nested.mkdir()
        (nested / "creative-1.png").write_bytes(PNG)
    if case == "symlink":
        actual = tmp_path / "actual"
        actual.write_bytes(PNG)
        (tmp_path / "creative-1.png").symlink_to(actual)
    if case == "bad-sha":
        item["sha256"] = "PRIVATE_CANARY_SECRET"
    if case == "bad-id":
        item["id"] = "https://PRIVATE_CANARY_SECRET"
    if case == "unreadable":
        (tmp_path / "creative-1.png").unlink()
        (tmp_path / "creative-1.png").write_bytes(b"")
    result = media.compare([item], [tmp_path], plan=True)
    assert result["success"] is False and result["complete"] is False
    assert result["statuses"][status] == 1
    assert result["objects"] == []
    assert "PRIVATE_CANARY_SECRET" not in json.dumps(result)


def test_digest_addressed_layout_must_also_match_named_digest(tmp_path):
    key = media_key("creative", "creative-1", SHA)
    path = tmp_path / key
    path.parent.mkdir(parents=True)
    path.write_bytes(PNG)
    assert media.compare([row()], [tmp_path])["success"] is True
    path.rename(path.with_name("a" * 64 + ".png"))
    assert media.compare([row()], [tmp_path])["statuses"]["DIGEST_MISMATCH"] == 1


def test_unrequired_rows_unknown_files_and_empty_coverage(tmp_path):
    result = media.compare([row(sha256=None, size_bytes=None, render_status="PENDING")], [tmp_path])
    assert result["not_media_bearing_rows"] == 1 and result["complete"] is False
    (tmp_path / "creative-1.png").write_bytes(PNG)
    (tmp_path / "orphan.png").write_bytes(PNG)
    assert media.compare([row()], [tmp_path])["orphan_files"] == 1
    assert media.compare([row()], [tmp_path])["complete"] is False


def test_multiple_roots_duplicate_and_overlap_fail_closed(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    for root in (left, right):
        (root / "creative-1.png").write_bytes(PNG)
    assert media.compare([row()], [left, right])["statuses"]["DUPLICATE"] == 1
    with pytest.raises(media.Refused):
        media.compare([row()], [left, left])
    with pytest.raises(media.Refused):
        media.compare([row()], [tmp_path, left])


def test_symlink_parent_never_reads_outside_supplied_root(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "creative-1.png").write_bytes(PNG)
    (root / "nested").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        media._open_source(root, root / "nested" / "creative-1.png")
    result = media.compare([row()], [root])
    assert result["success"] is False and result["source_png_bytes"] == 0


def test_gate_before_env_db_filesystem_and_storage(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Gate did not precede access")
    monkeypatch.setattr(media, "_env", forbidden)
    monkeypatch.setattr(media.sa, "create_engine", forbidden)
    monkeypatch.setattr(media, "_files", forbidden)
    result = media.run_inventory(database_env="UNREAD", roots=["/not-read"])
    assert result["diagnostic"]["stage"] == "EXECUTION_GATE"


def test_cli_no_secret_arg_echo_and_disabled_gate(capsys, monkeypatch):
    private = "PRIVATE_CANARY_SECRET"
    assert cli.main(["--unknown", private]) == 2
    output = capsys.readouterr()
    assert output.err == "" and private not in output.out
    monkeypatch.setenv("SOURCE_PRIVATE", private)
    assert cli.main(["--database-env", "SOURCE_PRIVATE", "--source-root", private]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["diagnostic"]["stage"] == "EXECUTION_GATE"
    assert output.err == "" and private not in output.out