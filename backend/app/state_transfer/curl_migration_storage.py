"""Opt-in manual-migration transport. No SDK, settings, listing or fallback."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
from urllib.parse import quote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from app.services.media_storage import StorageCorrupt, StorageMissing, StorageUnavailable, media_key
from .media_continuity import IDENTITY, SHA256, MAX_FILE_BYTES
from .one_shot_migration import _require

HEADER_LIMIT = 65536
PROCESS_TIMEOUT = 40


@dataclass(frozen=True)
class CurlBinary:
    path: str
    identity: tuple

    def verify(self):
        info = Path(self.path).stat()
        _require((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) == self.identity)


def _environment(home="/nonexistent"):
    # Never forward sealed values, proxies, curl settings or tracing variables.
    return {"PATH": os.defpath, "HOME": home, "LANG": "C", "LC_ALL": "C"}


def check_curl_capability():
    """Local version/help only, before configuration, DB or storage access."""
    try:
        path = shutil.which("curl")
        _require(path is not None)
        path = str(Path(path).resolve(strict=True))
        version = subprocess.run([path, "-q", "--version"], env=_environment(),
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=5, check=True).stdout
        match = re.match(rb"curl (\d+)\.(\d+)\.(\d+)", version)
        # 8.4 enforces max-filesize during transfers without Content-Length too.
        _require(match is not None and tuple(map(int, match.groups())) >= (8, 4, 0)
                 and b" https " in b" " + version.lower().replace(b"\n", b" ") + b" ")
        help_text = subprocess.run([path, "-q", "--help", "all"], env=_environment(),
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   timeout=5, check=True).stdout
        _require(all(option in help_text for option in (
            b"--aws-sigv4", b"--max-filesize", b"--proto", b"--retry", b"--max-time")))
        info = Path(path).stat()
        return CurlBinary(path, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
    except Exception:
        raise StorageUnavailable("Local curl capability refused.") from None


def _quoted(value):
    _require(type(value) is str and bool(value)
             and all(32 <= ord(c) < 127 for c in value))
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _private_file(path, content=b""):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)


def _bounded_process(argv, *, payload, limit, environment):
    """Independently cap pipe reads, even if curl/server violates its size cap."""
    process = subprocess.Popen(argv, stdin=subprocess.PIPE if payload is not None
                               else subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=environment, shell=False)
    bodies, errors, overflow = {}, [], set()
    threads = []

    def read(name, pipe, maximum):
        buffer = bytearray()
        try:
            while True:
                chunk = pipe.read(min(65536, maximum - len(buffer)))
                if not chunk:
                    break
                buffer.extend(chunk)
                # expected size + 1 already proves corruption; never read more.
                if len(buffer) == maximum:
                    overflow.add(name)
                    process.kill()
                    break
            bodies[name] = bytes(buffer)
        except Exception:
            errors.append(name)
            process.kill()

    def write():
        try:
            process.stdin.write(payload)
            process.stdin.close()
        except Exception:
            errors.append("input")

    try:
        for name, pipe, maximum in (("body", process.stdout, limit),
                                    ("status", process.stderr, 16)):
            thread = threading.Thread(target=read, args=(name, pipe, maximum), daemon=True)
            threads.append(thread)
            thread.start()
        if payload is not None:
            thread = threading.Thread(target=write, daemon=True)
            threads.append(thread)
            thread.start()
        code = process.wait(timeout=PROCESS_TIMEOUT)
        for thread in threads:
            thread.join(timeout=2)
        _require(not errors and all(not t.is_alive() for t in threads)
                 and "status" not in overflow)
        return code, bodies.get("body", b""), bodies.get("status", b""), "body" in overflow
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except Exception:
                    # A broken upload pipe must not prevent closing other pipes
                    # or reaping readers; the request has already been refused.
                    pass
        for thread in threads:
            thread.join(timeout=2)


def _headers(data):
    _require(len(data) <= HEADER_LIMIT)
    blocks = re.split(rb"\r?\n\r?\n", data.strip())
    last = blocks[-1].splitlines()
    match = re.fullmatch(rb"HTTP/(?:1\.[01]|2|3) ([0-9]{3})(?: .*)?", last[0])
    _require(match is not None)
    values = {}
    for line in last[1:]:
        name, separator, value = line.partition(b":")
        _require(bool(separator) and name.lower() not in values)
        values[name.lower()] = value.strip()
    return int(match.group(1)), values


class CurlExactTarget:
    """Same byte protocol as S3ExactTarget, using only explicitly bound keys."""
    def __init__(self, config, bindings, *, binary=None):
        self.binary = binary or check_curl_capability()
        self.binary.verify()
        self.config = config
        self.bindings = {}
        for binding in bindings:
            identity, sha, size = (binding[k] for k in ("creative_id", "sha256", "size_bytes"))
            _require(IDENTITY.fullmatch(identity) and SHA256.fullmatch(sha)
                     and type(size) is int and 8 <= size <= MAX_FILE_BYTES
                     and binding["key"] == media_key("creative", identity, sha)
                     and binding["key"] not in self.bindings)
            self.bindings[binding["key"]] = size
        self.endpoint = urlsplit(config.endpoint)
        _require(self.endpoint.scheme == "https" and bool(self.endpoint.hostname)
                 and not self.endpoint.username and not self.endpoint.password
                 and not self.endpoint.query and not self.endpoint.fragment
                 and ":" not in config.access_key)
        for segment in self.endpoint.path.split("/"):
            _require(segment not in {".", ".."} and
                     (not segment or re.fullmatch(r"[A-Za-z0-9._~-]+", segment)))
        # Validate quoting before any request or private file is created.
        _quoted(config.access_key)
        _quoted(config.secret_key)

    def _url(self, key):
        _require(key in self.bindings)
        host = self.endpoint.hostname
        if self.config.path_style:
            prefix = self.endpoint.path.rstrip("/") + "/" + self.config.bucket
        else:
            _require(":" not in host)
            host = self.config.bucket + "." + host
            prefix = self.endpoint.path.rstrip("/")
        if ":" in host:
            host = "[" + host + "]"
        if self.endpoint.port is not None:
            host += ":" + str(self.endpoint.port)
        path = prefix + "/" + quote(key, safe="/")
        return urlunsplit(("https", host, path, "", ""))

    def _request(self, key, *, payload=None):
        self.binary.verify()
        _require(key in self.bindings)
        size = self.bindings[key]
        try:
            with tempfile.TemporaryDirectory(prefix="closed-state-curl-", dir="/tmp") as folder:
                root = Path(folder)
                config_path, headers_path = root / "request.conf", root / "headers"
                _private_file(headers_path)
                method = "GET" if payload is None else "PUT"
                lines = [
                    "url = " + _quoted(self._url(key)),
                    "user = " + _quoted(self.config.access_key + ":" + self.config.secret_key),
                    "aws-sigv4 = " + _quoted("aws:amz:" + self.config.region + ":s3"),
                    "request = " + _quoted(method),
                    "dump-header = " + _quoted(str(headers_path)),
                ]
                if payload is None:
                    lines.append("range = " + _quoted("0-" + str(size)))
                else:
                    lines.extend(['data-binary = "@-"', 'header = "Content-Type: image/png"',
                                  'header = "If-None-Match: *"', 'header = "Expect:"'])
                _private_file(config_path, ("\n".join(lines) + "\n").encode("ascii"))
                argv = [
                    self.binary.path, "-q", "--config", str(config_path),
                    "--silent", "--globoff", "--path-as-is", "--no-location", "--http1.1",
                    "--max-redirs", "0", "--retry", "0", "--proto", "=https",
                    "--proto-redir", "=https", "--noproxy", "*",
                    "--connect-timeout", "10", "--max-time", "30",
                    "--max-filesize", str(size + 1), "--output", "-",
                    "--write-out", "%{stderr}%{http_code}",
                ]
                code, body, status_text, overflow = _bounded_process(
                    argv, payload=payload, limit=size + 1, environment=_environment(folder))
                with headers_path.open("rb") as stream:
                    status, headers = _headers(stream.read(HEADER_LIMIT + 1))
                _require(status_text == str(status).encode("ascii") or
                         (overflow and not status_text))
                return code, status, headers, body, overflow
        except Exception:
            raise StorageUnavailable("Exact curl request refused.") from None

    def get(self, key, size):
        _require(key in self.bindings and size == self.bindings[key])
        code, status, headers, data, overflow = self._request(key)
        if status in {200, 206, 416} and (code == 63 or overflow or status == 416):
            raise StorageCorrupt("Exact object size conflict.") from None
        _require(code == 0)
        if status == 404:
            if data:
                try:
                    _require(b"<!DOCTYPE" not in data and b"<!ENTITY" not in data)
                    error = ET.fromstring(data)
                    codes = [e.text for e in error.iter() if e.tag.rsplit("}", 1)[-1] == "Code"]
                    _require(codes in [["NoSuchKey"], ["NotFound"], ["404"]])
                except Exception:
                    raise StorageUnavailable("Exact object absence unconfirmed.") from None
            raise StorageMissing("Exact object absent.") from None
        _require(status in {200, 206})
        if status == 206:
            match = re.fullmatch(rb"bytes 0-(\d+)/(\d+)", headers.get(b"content-range", b""))
            if match is None or int(match[2]) != size or int(match[1]) + 1 != len(data):
                raise StorageCorrupt("Exact object range conflict.") from None
        return data

    def put_missing(self, key, data):
        _require(key in self.bindings and type(data) is bytes and len(data) == self.bindings[key])
        code, status, _, _, overflow = self._request(key, payload=data)
        _require(code == 0 and not overflow and status in {200, 201, 204})

    def close(self):
        # Each request owns and immediately removes its private temporary files.
        pass
