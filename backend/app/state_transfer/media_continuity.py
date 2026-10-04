"""Read-only closed-state local-byte certification; never a storage operation."""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import ExitStack
import hashlib
import logging
import os
from pathlib import Path
import re
import stat

import sqlalchemy as sa

from app.services.media_storage import PNG_SIGNATURE, media_key
from .catalog import Refused, digest
from .one_shot_migration import _env, _url, _require
from .transfer import closed_state, transaction


EXECUTION_ACK = "INVENTORY_MEDIA_CONTINUITY_ONCE"
STATUSES = ("MATCHED", "MISSING", "DIGEST_MISMATCH", "DUPLICATE", "UNSUPPORTED")
IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,36}")
SHA256 = re.compile(r"[a-f0-9]{64}")
MAX_FILES = MAX_ROWS = 100000
MAX_FILE_BYTES = 100 * 1024 * 1024


def _open_source(root, path):
    # Anchor reads to the supplied directory and reject symlinks in *every*
    # descendant component, including replacements during traversal. NONBLOCK
    # prevents a concurrent FIFO replacement from hanging before fstat.
    with ExitStack() as stack:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory)
        parts = path.relative_to(root).parts
        for part in parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
            stack.callback(os.close, directory)
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                       dir_fd=directory)


def _files(roots):
    paths = [Path(p).resolve(strict=True) for p in roots]
    _require(bool(paths) and len(set(paths)) == len(paths))
    _require(all(p.is_dir() for p in paths))
    _require(not any(a in b.parents for a in paths for b in paths if a != b))
    index = defaultdict(list)
    unsupported = count = total = 0
    for root in paths:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                unsupported += 1
                continue
            if not path.is_file():
                continue
            if path.suffix.lower() != ".png":
                continue
            count += 1
            _require(count <= MAX_FILES)
            # Legacy caches use <id>.png; LocalStorage uses the existing
            # digest-addressed creative/<id>/<sha256>.png layout.
            if (SHA256.fullmatch(path.stem) and path.parent.parent.name == "creative"):
                identity = path.parent.name
                named_digest = path.stem
            else:
                identity, named_digest = path.stem, None
            if not IDENTITY.fullmatch(identity):
                unsupported += 1
                continue
            value = None
            try:
                fd = _open_source(root, path)
                with os.fdopen(fd, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                            or not 8 <= before.st_size <= MAX_FILE_BYTES):
                        raise Refused()
                    hasher = hashlib.sha256()
                    signature = stream.read(8)
                    hasher.update(signature)
                    read_size = 8
                    while block := stream.read(1024 * 1024):
                        read_size += len(block)
                        if read_size > MAX_FILE_BYTES:
                            raise Refused()
                        hasher.update(block)
                    after = os.fstat(stream.fileno())
                    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                        raise Refused()
                    total += after.st_size
                    value = (hasher.hexdigest(), after.st_size, signature == PNG_SIGNATURE,
                             named_digest)
            except (OSError, Refused):
                pass
            index[identity].append(value)
    return index, count, total, unsupported


def compare(rows, roots, *, plan=False):
    """No settings, database writes, provider clients, URLs or source paths."""
    _require(len(rows) <= MAX_ROWS)
    index, file_count, file_bytes, unsupported_files = _files(roots)
    counts = Counter({name: 0 for name in STATUSES})
    bindings, objects, seen = [], [], set()
    skipped = 0
    for row in rows:
        identity, sha, size, state = (row[k] for k in ("id", "sha256", "size_bytes", "render_status"))
        if state not in {"RENDERED", "STAGED"} and sha is None and size is None:
            skipped += 1
            continue
        valid = (type(identity) is str and IDENTITY.fullmatch(identity)
                 and identity not in seen and type(sha) is str and SHA256.fullmatch(sha)
                 and state in {"RENDERED", "STAGED"}
                 and (size is None or type(size) is int and size >= 8))
        if type(identity) is str:
            seen.add(identity)
        status = "UNSUPPORTED"
        matched_size = None
        if valid:
            candidates = index.get(identity, [])
            if len(candidates) > 1:
                status = "DUPLICATE"
            elif not candidates:
                status = "MISSING"
            elif candidates[0] is not None:
                actual, length, png, named = candidates[0]
                status = "MATCHED" if (png and sha == actual and (size is None or size == length)
                                      and (named is None or named == sha)) else "DIGEST_MISMATCH"
                if status == "MATCHED":
                    matched_size = length
        counts[status] += 1
        # Only validated bindings may appear in output or its fingerprint.
        if valid:
            key = media_key("creative", identity, sha)
            bindings.append({"creative_id": identity, "sha256": sha, "status": status,
                             "size_bytes": matched_size})
            if plan and status == "MATCHED":
                objects.append({"object_key": key, "sha256": sha, "size_bytes": matched_size})
    orphan_files = sum(len(files) for identity, files in index.items() if identity not in seen)
    complete = (counts["MATCHED"] > 0 and sum(counts.values()) == counts["MATCHED"]
                and unsupported_files == 0 and orphan_files == 0)
    result = {
        "success": complete, "complete": complete,
        "mode": "TARGET_PLAN" if plan else "INVENTORY",
        "scope": "LOCAL_SOURCE_BYTES_ONLY",
        "provider_certification": "NOT_CHECKED", "publishing_admission": "NOT_GRANTED",
        "metadata_rows": len(rows), "not_media_bearing_rows": skipped,
        "required_media_count": sum(counts.values()), "statuses": dict(counts),
        "source_png_count": file_count, "source_png_bytes": file_bytes,
        "unsupported_files": unsupported_files, "orphan_files": orphan_files,
        "inventory_sha256": digest(sorted(bindings, key=lambda v: v["creative_id"])),
        "database_writes": 0, "provider_calls": 0, "storage_writes": 0,
    }
    if plan:
        result["objects"] = sorted(objects, key=lambda v: v["object_key"])
        result["target_configuration"] = "NOT_CHECKED"
    return result


def certify_inventory(engine, roots, *, plan=False):
    with transaction(engine, readonly=True) as connection:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM public.alembic_version").scalars().all()
        _require(revision in (["0031"], ["0034"]))
        closed_state(connection)
        rows = connection.exec_driver_sql(
            "SELECT id,sha256,size_bytes,render_status FROM public.pin_creatives "
            f"ORDER BY id LIMIT {MAX_ROWS + 1}"
        ).mappings().all()
        result = compare(rows, roots, plan=plan)
        result.update({"closed_state": "PASS", "database_revision": revision[0],
                       "read_only": connection.exec_driver_sql("SHOW transaction_read_only").scalar_one(),
                       "isolation": connection.exec_driver_sql("SHOW transaction_isolation").scalar_one()})
        return result


def run_inventory(*, database_env, roots, execute=False, execution_env=None, plan=False):
    stage, engine = "EXECUTION_GATE", None
    result = {}
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        _require(type(execute) is bool and (execute or (
            execution_env is not None and _env(execution_env) == EXECUTION_ACK)))
        stage = "CONFIGURATION"
        _require(type(plan) is bool)
        url = _url(_env(database_env))
        _require(bool(roots))
        engine = sa.create_engine(url, echo=False, hide_parameters=True,
                                  connect_args={"connect_timeout": 10})
        stage = "READ_ONLY_INVENTORY"
        result = certify_inventory(engine, roots, plan=plan)
    except Exception:
        result = {"success": False, "complete": False,
                  "diagnostic": {"stage": stage, "exception_class": "Refused"},
                  "database_writes": 0, "provider_calls": 0, "storage_writes": 0}
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                result = {"success": False, "complete": False,
                          "diagnostic": {"stage": "DATABASE_CLOSE", "exception_class": "Refused"},
                          "database_writes": 0, "provider_calls": 0, "storage_writes": 0}
        logging.disable(previous)
    return result