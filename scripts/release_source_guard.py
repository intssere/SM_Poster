"""Offline verification of build-visible source; not a platform snapshot attestation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

REQUIRED_EXPECTATIONS = {
    "EXPECTED_RELEASE_COMMIT": 40,
    "EXPECTED_RELEASE_TREE": 40,
    "EXPECTED_CANONICAL_COMMIT": 40,
    "EXPECTED_CANONICAL_TREE": 40,
    "EXPECTED_REPLIT_OVERLAY_SHA256": 64,
}
# Exact roots only. Nothing is excluded merely because .gitignore ignores it.
# Dependencies are outside this source-integrity contract, not attested source.
DEPENDENCY_DIRS = frozenset({"node_modules", "frontend/node_modules", ".pythonlibs"})
OUTPUT_DIRS = frozenset({"frontend/dist"})
OUTPUT_FILES = frozenset({
    "frontend/tsconfig.tsbuildinfo",
    "backend/.build-provenance.json",
    "backend/.build-provenance.json.tmp",
})
RECEIPT = "backend/.build-provenance.json"
TOPOLOGY = "canonical_parent_with_checkpoint_overlay"


class SourceGuardError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SourceGuardError(message)


def expectations(values=None) -> dict[str, str]:
    values = os.environ if values is None else values
    result = {}
    for name, length in REQUIRED_EXPECTATIONS.items():
        value = values.get(name, "")
        require(isinstance(value, str) and
                re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None,
                f"independently pinned {name} required ({length} lowercase hex)")
        result[name] = value
    return result


def git(root: Path, *args: str) -> bytes:
    # Do not inherit Git overrides, credentials, fsmonitor commands or global
    # config. In particular, replacement objects must never redefine identity.
    env = {
        "PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
    }
    try:
        return subprocess.check_output([
            "git", "--no-replace-objects", "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=/dev/null", "-c", "core.untrackedCache=false",
            *args,
        ], cwd=root, env=env, stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError) as error:
        raise SourceGuardError("missing/unusable Git metadata or Git objects") from error


def git_text(root: Path, *args: str) -> str:
    return git(root, *args).decode("utf-8").strip()


def git_object(root: Path, kind: str, oid: str) -> bytes:
    data = git(root, "cat-file", kind, oid)
    framed = f"{kind} {len(data)}\0".encode("ascii") + data
    require(hashlib.sha1(framed, usedforsecurity=False).hexdigest() == oid,
            "Git object identity mismatch")
    return data


def verify_identity(root: Path, pins: dict[str, str]) -> None:
    require((root / ".git").exists() and not (root / ".git").is_symlink(),
            "missing/unusable root Git metadata")
    require(Path(git_text(root, "rev-parse", "--show-toplevel")).resolve() == root.resolve(),
            "Git worktree root mismatch")
    release = pins["EXPECTED_RELEASE_COMMIT"]
    require(git_text(root, "rev-parse", "--verify", "HEAD") == release,
            "release commit mismatch")
    require(git_text(root, "rev-parse", "--verify", "HEAD^{tree}") ==
            pins["EXPECTED_RELEASE_TREE"], "release tree mismatch")
    # Read verified commit bytes, not a history walk that could honor grafts.
    headers = git_object(root, "commit", release).split(b"\n\n", 1)[0].splitlines()
    parents = [line[7:].decode("ascii") for line in headers if line.startswith(b"parent ")]
    require(parents == [pins["EXPECTED_CANONICAL_COMMIT"]],
            "release must have exactly one canonical parent")
    require([line[5:].decode("ascii") for line in headers if line.startswith(b"tree ")] ==
            [pins["EXPECTED_RELEASE_TREE"]], "release commit/tree binding mismatch")
    canonical_headers = git_object(root, "commit", pins["EXPECTED_CANONICAL_COMMIT"]).split(
        b"\n\n", 1)[0].splitlines()
    require([line[5:].decode("ascii") for line in canonical_headers if line.startswith(b"tree ")] ==
            [pins["EXPECTED_CANONICAL_TREE"]], "canonical commit/tree binding mismatch")
    require(git_text(root, "rev-parse", "--verify",
                     pins["EXPECTED_CANONICAL_COMMIT"] + "^{tree}") ==
            pins["EXPECTED_CANONICAL_TREE"], "canonical tree mismatch")
    git_object(root, "tree", pins["EXPECTED_RELEASE_TREE"])
    git_object(root, "tree", pins["EXPECTED_CANONICAL_TREE"])
    changed = git(root, "diff-tree", "--no-commit-id", "--name-only", "-r",
                  "-z", "--no-renames", pins["EXPECTED_CANONICAL_COMMIT"], release)
    require(changed == b".replit\0", "checkpoint tracked delta must be exactly .replit")
    # An alternate index cannot silently certify a different staged tree.
    require(not git(root, "diff", "--cached", "--raw", "--no-ext-diff",
                    "--no-textconv", release, "--"), "index differs from release tree")


def tree_entries(root: Path, tree: str) -> dict[str, tuple[str, str]]:
    entries = {}
    for item in git(root, "ls-tree", "-rtz", "--full-tree", tree).split(b"\0"):
        if not item:
            continue
        header, name = item.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split()
        path = name.decode("utf-8")
        require(not path.startswith("/") and
                all(part not in {"", ".", "..", ".git"} for part in path.split("/")),
                "unsafe Git source path")
        if kind == "tree":
            require(mode == "040000", "unsupported tree mode")
            git_object(root, "tree", oid)
            continue
        require(kind == "blob" and mode in {"100644", "100755", "120000"},
                "unsupported source mode (including submodules)")
        require(not any(path == directory or path.startswith(directory + "/")
                        for directory in DEPENDENCY_DIRS | OUTPUT_DIRS)
                and path not in OUTPUT_FILES, "generated/dependency path is tracked")
        entries[path] = (mode, oid)
    require(bool(entries), "empty source tree")
    return entries


def parent_fd(root: Path, path: str) -> tuple[int, str]:
    """Anchor every ancestor; never follow a swapped directory symlink."""
    parts = PurePosixPath(path).parts
    require(bool(parts) and not path.startswith("/") and
            all(part not in {".", ".."} for part in parts), "unsafe filesystem path")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def physical_entry(root: Path, path: str) -> tuple[str, bytes]:
    def identity(info):
        # Reads/readlink may legitimately update atime; it is not source identity.
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)
    parent, name = parent_fd(root, path)
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            data = os.fsencode(os.readlink(name, dir_fd=parent))
            after = os.stat(name, dir_fd=parent, follow_symlinks=False)
            require(identity(before) == identity(after), "symlink changed while verifying")
            return "120000", data
        require(stat.S_ISREG(before.st_mode), "source/output is not a regular file or symlink")
        require(not before.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX),
                "unexpected special permission bits")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            require(identity(before) == identity(opened), "file changed while opening")
            data = stream.read()
            require(identity(opened) == identity(os.fstat(stream.fileno())), "file changed while reading")
        require(identity(before) == identity(os.stat(name, dir_fd=parent, follow_symlinks=False)),
                "file replaced while verifying")
        executable = bool(before.st_mode & stat.S_IXUSR)
        require(executable or not before.st_mode & 0o111, "unexpected executable mode")
        return ("100755" if executable else "100644"), data
    finally:
        os.close(parent)


def files(root: Path, *, source: bool) -> list[str]:
    """Enumerate physical leaves, including ignored files; don't follow links."""
    result = []
    excluded_dirs = DEPENDENCY_DIRS | OUTPUT_DIRS if source else frozenset()
    def walk(directory: Path, prefix: str) -> None:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                path = prefix + entry.name
                if source and path == ".git":
                    continue
                if path in excluded_dirs:
                    require(entry.is_dir(follow_symlinks=False),
                            "excluded cache/output must be a real directory")
                    continue
                if source and path in OUTPUT_FILES:
                    require(entry.is_file(follow_symlinks=False),
                            "excluded generated output must be a regular file")
                    continue
                if entry.is_dir(follow_symlinks=False):
                    walk(Path(entry.path), path + "/")
                else:
                    result.append(path)
    walk(root, "")
    return sorted(result)


def inventory_digest(records: list[dict]) -> str:
    encoded = json.dumps({"inventory_version": 1, "entries": records},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def verify_source(root: Path, pins: dict[str, str]) -> dict:
    verify_identity(root, pins)
    expected = tree_entries(root, pins["EXPECTED_RELEASE_TREE"])
    require(files(root, source=True) == sorted(expected),
            "unexpected untracked/ignored source or missing tracked source")
    records = []
    for path, (mode, oid) in sorted(expected.items()):
        actual_mode, data = physical_entry(root, path)
        require(actual_mode == mode, f"source mode drift: {path}")
        require(data == git_object(root, "blob", oid), f"source content drift: {path}")
        if mode == "120000":
            target = (root / path).resolve(strict=True)
            require(target.is_relative_to(root.resolve()), "source symlink escapes worktree")
            relative = target.relative_to(root.resolve()).as_posix()
            require(relative in expected or any(p.startswith(relative + "/") for p in expected),
                    "source symlink target is outside verified source")
        records.append({"path": path, "mode": mode,
                        "content_sha256": hashlib.sha256(data).hexdigest()})
    require(hashlib.sha256(physical_entry(root, ".replit")[1]).hexdigest() ==
            pins["EXPECTED_REPLIT_OVERLAY_SHA256"], "reviewed .replit hash mismatch")
    verify_identity(root, pins)
    require(files(root, source=True) == sorted(expected), "source inventory changed while verifying")
    return {
        "schema_version": 4, "topology": TOPOLOGY,
        "canonical_commit_sha": pins["EXPECTED_CANONICAL_COMMIT"],
        "canonical_tree_sha": pins["EXPECTED_CANONICAL_TREE"],
        "release_commit_sha": pins["EXPECTED_RELEASE_COMMIT"],
        "release_tree_sha": pins["EXPECTED_RELEASE_TREE"],
        "release_overlay": {"path": ".replit", "sha256": pins["EXPECTED_REPLIT_OVERLAY_SHA256"]},
        "source_inventory_sha256": inventory_digest(records),
        "source_file_count": len(records),
    }


def source_witness(root: Path, pins: dict[str, str]) -> tuple:
    """Ephemeral change evidence, never part of the deterministic receipt.

    ctime/inode evidence also rejects a tracked file or HEAD changed and then
    restored during compilation. This is not a filesystem lock or a platform
    snapshot witness; dependencies and artifacts have a separate contract.
    """
    paths = [root / path for path in tree_entries(root, pins["EXPECTED_RELEASE_TREE"])]
    ref = git_text(root, "rev-parse", "--symbolic-full-name", "HEAD")
    metadata = ["HEAD", "index", "packed-refs"]
    if ref != "HEAD":
        metadata.append(ref)
    for name in metadata:
        path = Path(git_text(root, "rev-parse", "--git-path", name))
        paths.append(path if path.is_absolute() else root / path)
    result = []
    for path in paths:
        try:
            info = path.lstat()
            result.append((str(path), info.st_dev, info.st_ino, info.st_mode,
                           info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        except FileNotFoundError:
            result.append((str(path), None))
    return tuple(result)


def artifact_inventory(root: Path) -> dict:
    directory = root / "frontend/dist"
    require(directory.is_dir() and not directory.is_symlink(), "frontend/dist missing/unusable")
    paths = files(directory, source=False)
    require(bool(paths), "compiled artifact inventory is empty")
    records = []
    for path in paths:
        mode, data = physical_entry(root, "frontend/dist/" + path)
        require(mode != "120000", "compiled artifacts must not contain symlinks")
        records.append({"path": path, "mode": mode,
                        "content_sha256": hashlib.sha256(data).hexdigest()})
    artifacts = {"frontend/dist": {"inventory_sha256": inventory_digest(records),
                                    "file_count": len(records)}}
    info = root / "frontend/tsconfig.tsbuildinfo"
    if info.exists():
        mode, data = physical_entry(root, "frontend/tsconfig.tsbuildinfo")
        require(mode != "120000", "compiler metadata must not be a symlink")
        artifacts["frontend/tsconfig.tsbuildinfo"] = {
            "sha256": hashlib.sha256(data).hexdigest(), "mode": mode,
        }
    return artifacts