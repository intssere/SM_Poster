"""Manual closed-state byte transfer. No ORM, settings, URLs, or DB mutations."""
from __future__ import annotations

from collections import Counter
import logging
import os
from pathlib import Path
import stat

import sqlalchemy as sa

from app.services.media_storage import PNG_SIGNATURE, StorageMissing, media_key
from app.services.s3_media_storage import S3Config
from .catalog import digest
from .media_continuity import IDENTITY, SHA256, MAX_FILE_BYTES, _open_source
from .one_shot_migration import ENV_NAME, _env, _require, _url
from .transfer import closed_state, transaction

ACK = "MIGRATE_CLOSED_STATE_MEDIA_ONCE"
REQUIRED_COUNT = 17
MAX_TOTAL_BYTES = 256 * 1024 * 1024


def verified(data, binding):
    import hashlib
    _require(type(data) is bytes and len(data) == binding["size_bytes"]
             and data.startswith(PNG_SIGNATURE)
             and hashlib.sha256(data).hexdigest() == binding["sha256"])
    return data


def metadata(engine):
    """Exactly one repeatable-read/read-only transaction, reviewed source only."""
    with transaction(engine, readonly=True) as c:
        _require(c.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "on")
        _require(c.exec_driver_sql("SHOW transaction_isolation").scalar_one() == "repeatable read")
        _require(c.exec_driver_sql("SELECT version_num FROM public.alembic_version").scalars().all()
                 == ["0031"])
        closed_state(c)
        rows = c.exec_driver_sql(
            "SELECT id,sha256,size_bytes,render_status FROM public.pin_creatives "
            "ORDER BY id LIMIT 18").mappings().all()
        return bindings(rows)


def bindings(rows):
    _require(len(rows) == REQUIRED_COUNT)
    result, seen = [], set()
    for row in rows:
        identity, sha, size = row["id"], row["sha256"], row["size_bytes"]
        _require(type(identity) is str and IDENTITY.fullmatch(identity)
                 and identity not in seen and type(sha) is str and SHA256.fullmatch(sha)
                 and type(size) is int and 8 <= size <= MAX_FILE_BYTES
                 and row["render_status"] in {"RENDERED", "STAGED"})
        seen.add(identity)
        result.append({"creative_id": identity, "key": media_key("creative", identity, sha),
                       "sha256": sha, "size_bytes": size})
    _require(sum(v["size_bytes"] for v in result) <= MAX_TOTAL_BYTES)
    return sorted(result, key=lambda v: v["key"])


def local_roots(values):
    _require(bool(values))
    roots = [Path(v).absolute() for v in values]
    _require(len(set(roots)) == len(roots)
             and all(p.resolve(strict=True) == p and p.is_dir() for p in roots)
             and not any(a in b.parents for a in roots for b in roots if a != b))
    return roots


def local_bytes(binding, roots):
    """Only two exact supported layouts; no directory scan or HTTPS fallback."""
    candidates = []
    for root in roots:
        for path in (root / (binding["creative_id"] + ".png"), root / binding["key"]):
            for parent in path.parents:
                if parent == root:
                    break
                _require(not parent.is_symlink())
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            candidates.append((root, path))
    _require(len(candidates) <= 1)
    if not candidates:
        return None
    root, path = candidates[0]
    with os.fdopen(_open_source(root, path), "rb") as stream:
        before = os.fstat(stream.fileno())
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                 and before.st_size == binding["size_bytes"])
        data = stream.read(binding["size_bytes"] + 1)
        after = os.fstat(stream.fileno())
        _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                 == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns))
    return verified(data, binding)


def target_config(names, database_env, execution_env=None):
    values = [database_env, *names.values()]
    if execution_env:
        values.append(execution_env)
    _require(set(names) == {"endpoint", "bucket", "access_key", "secret_key", "region", "path_style"}
             and all(type(n) is str and ENV_NAME.fullmatch(n) for n in values)
             and len(set(values)) == len(values))
    style = _env(names["path_style"])
    _require(style in {"true", "false"})
    config = S3Config(**{k: _env(v) for k, v in names.items() if k != "path_style"},
                      path_style=style == "true")
    # The manual production transfer does not allow plaintext target credentials.
    _require(config.endpoint.startswith("https://"))
    return config


def transfer_bytes(source, roots, config, *, dry_run, report, source_factory, target_factory):
    """Injectable byte protocol; all source and target preflights precede puts."""
    objects = [{**v, "source_class": "NOT_RUN", "target_status": "NOT_RUN"} for v in source]
    report["objects"] = objects
    report["source_fingerprint"] = digest(source)
    report["target_configuration_fingerprint"] = digest({
        "endpoint": config.endpoint, "bucket": config.bucket,
        "region": config.region, "path_style": config.path_style})
    data, reader, target = {}, None, None
    report["terminal_stage"] = "SOURCE_PREFLIGHT"
    try:
        for item in objects:
            payload = local_bytes(item, roots)
            if payload is None:
                if reader is None:
                    reader = source_factory(source)
                report["source_reads"] += 1
                payload = verified(reader.get(item["key"], item["size_bytes"]), item)
                item["source_class"] = "REPLIT_VERIFIED"
            else:
                item["source_class"] = "LOCAL_VERIFIED"
            data[item["key"]] = payload
        # No target network/client construction until every authoritative source passes.
        report["terminal_stage"] = "TARGET_PREFLIGHT"
        target = target_factory(config, source)
        conflicts = False
        for item in objects:
            report["target_reads"] += 1
            try:
                payload = target.get(item["key"], item["size_bytes"])
            except StorageMissing:
                item["target_status"] = "MISSING"
                continue
            try:
                verified(payload, item)
                item["target_status"] = "VERIFIED_EXISTING"
            except Exception:
                item["target_status"] = "CONFLICT"
                conflicts = True
        _require(not conflicts)
        if not dry_run:
            for item in objects:
                if item["target_status"] != "MISSING":
                    continue
                report["terminal_stage"] = "TARGET_WRITE"
                # Conditional create: a race may refuse, never overwrite or retry.
                report["target_put_attempts"] += 1
                item["target_status"] = "PUT_ATTEMPTED_UNCONFIRMED"
                target.put_missing(item["key"], data[item["key"]])
                item["target_status"] = "UPLOADED_UNVERIFIED"
                report["terminal_stage"] = "TARGET_READBACK"
                report["target_reads"] += 1
                verified(target.get(item["key"], item["size_bytes"]), item)
                item["target_status"] = "UPLOADED_VERIFIED"
        report["success"] = True
        report["terminal_stage"] = "DRY_RUN_COMPLETE" if dry_run else "MEDIA_CERTIFIED"
        report["media_certification"] = (
            "PASS" if all(v["target_status"] in {"VERIFIED_EXISTING", "UPLOADED_VERIFIED"}
                          for v in objects) else "NOT_GRANTED")
    finally:
        data.clear()
        close_failed = False
        for backend in (reader, target):
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    close_failed = True
        if close_failed:
            report["terminal_stage"] = "STORAGE_CLOSE"
            _require(False)


def run(*, database_env, roots, target_envs, execute=False, dry_run=False,
        execution_env=None, source_factory=None, target_factory=None):
    report = {"success": False, "terminal_stage": "EXECUTION_GATE", "objects": [],
              "database_writes": 0, "publishing_admission": "NOT_GRANTED",
              "media_certification": "NOT_GRANTED", "source_reads": 0, "target_reads": 0,
              "target_put_attempts": 0, "database_transactions": 0,
              "automatic_retries": 0, "source_fingerprint": None,
              "target_fingerprint": None, "transfer_fingerprint": None}
    previous, engine = logging.root.manager.disable, None
    logging.disable(logging.CRITICAL)
    try:
        _require(type(execute) is bool and type(dry_run) is bool)
        _require(not (dry_run and (execute or execution_env is not None)))
        _require(dry_run or execute or (execution_env is not None and _env(execution_env) == ACK))
        report["mode"] = "DRY_RUN" if dry_run else "EXECUTE"
        report["terminal_stage"] = "CONFIGURATION"
        config = target_config(target_envs, database_env, execution_env)
        source_roots = local_roots(roots)
        engine = sa.create_engine(_url(_env(database_env)), echo=False, hide_parameters=True,
                                  connect_args={"connect_timeout": 10})
        report["terminal_stage"] = "READ_ONLY_METADATA"
        report["database_transactions"] = 1
        source = metadata(engine)
        engine.dispose()
        engine = None
        from .migration_storage import ReplitExactReader, S3ExactTarget
        transfer_bytes(source, source_roots, config, dry_run=dry_run, report=report,
                       source_factory=source_factory or ReplitExactReader,
                       target_factory=target_factory or S3ExactTarget)
    except Exception:
        report["success"] = False
        report["media_certification"] = "NOT_GRANTED"
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                report.update(success=False, terminal_stage="DATABASE_CLOSE",
                              media_certification="NOT_GRANTED")
        objects = report["objects"]
        report["source_counts"] = dict(Counter(v["source_class"] for v in objects))
        report["target_counts"] = dict(Counter(v["target_status"] for v in objects))
        if objects:
            report["target_fingerprint"] = digest([
                {k: v[k] for k in ("key", "sha256", "size_bytes")} | {
                    "status": "VERIFIED" if v["target_status"] in
                    {"VERIFIED_EXISTING", "UPLOADED_VERIFIED"} else v["target_status"]}
                for v in objects])
            report["transfer_fingerprint"] = digest({
                "source": report["source_fingerprint"], "target": report["target_fingerprint"],
                "configuration": report["target_configuration_fingerprint"]})
        logging.disable(previous)
    return report
