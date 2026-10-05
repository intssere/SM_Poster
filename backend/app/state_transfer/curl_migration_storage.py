"""Manual migration-only exact S3 target using curl with AWS SigV4."""
from __future__ import annotations

import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import quote, unquote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from app.services.media_storage import StorageMissing, StorageUnavailable
from .one_shot_migration import _require


CONNECT_TIMEOUT = 10
TOTAL_TIMEOUT = 30
ERROR_BODY_LIMIT = 4096
HEADER_LIMIT = 16384
HEADER_LINE_LIMIT = 4096


def _curl_env():
    """Minimal curl environment: no ambient cloud credentials or proxy routing."""
    env = {"PATH": os.environ.get("PATH", ""), "LC_ALL": "C", "LANG": "C"}
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def verify_curl_sigv4_capability(*, runner=subprocess.run):
    """Local capability check only. This never performs an HTTP request."""
    curl = shutil.which("curl")
    _require(type(curl) is str and bool(curl))
    common = {
        "capture_output": True,
        "text": True,
        "timeout": 5,
        "check": False,
        "env": _curl_env(),
    }
    version = runner([curl, "--version"], **common)
    help_all = runner([curl, "--help", "all"], **common)
    _require(
        version.returncode == 0
        and help_all.returncode == 0
        and "https" in version.stdout.lower()
        and "--aws-sigv4" in help_all.stdout
    )
    return curl


def _config_quote(value):
    _require(
        type(value) is str
        and "\x00" not in value
        and "\n" not in value
        and "\r" not in value
    )
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t")


def _credential_config(config):
    """Create one private curl config outside the checkout and remove after the call."""
    _require(":" not in config.access_key)
    directory = Path(tempfile.gettempdir()).resolve()
    cwd = Path.cwd().resolve()
    _require(directory != cwd and cwd not in directory.parents)
    fd, path = tempfile.mkstemp(prefix="sm-poster-s3-", suffix=".conf", dir=str(directory))
    try:
        os.fchmod(fd, 0o600)
        content = (
            f'aws-sigv4 = "{_config_quote("aws:amz:" + config.region + ":s3")}"\n'
            f'user = "{_config_quote(config.access_key + ":" + config.secret_key)}"\n'
        ).encode("utf-8")
        os.write(fd, content)
        os.fsync(fd)
    except Exception:
        try:
            os.close(fd)
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        raise
    else:
        os.close(fd)
    return path


_STATUS = re.compile(rb"^HTTP/(?:1\.[01]|2|3) ([0-9]{3})(?: [^\r\n]*)?\r?\n$")


def _read_http_status(stream):
    """Read exactly one bounded response-header block and return its status."""
    total = 0
    first = True
    status = None
    while True:
        remaining = HEADER_LIMIT - total
        if remaining <= 0:
            return None
        line = stream.readline(min(HEADER_LINE_LIMIT + 1, remaining + 1))
        if not line:
            return None
        total += len(line)
        if len(line) > HEADER_LINE_LIMIT or total > HEADER_LIMIT:
            return None
        if first:
            match = _STATUS.fullmatch(line)
            if match is None:
                return None
            status = int(match.group(1))
            first = False
        if line in {b"\r\n", b"\n"}:
            return status


def _s3_error_code(raw):
    """Accept only the exact unnamespaced S3 NoSuchKey error envelope."""
    if type(raw) is not bytes or not raw or len(raw) > ERROR_BODY_LIMIT:
        return None
    upper = raw.upper()
    # ElementTree has no external resolver here; reject declarations that could
    # introduce entity expansion before parsing the already-small body.
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        return None
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError):
        return None

    # No namespace normalization: namespace-qualified/default-namespace tags are
    # deliberately ambiguous and therefore cannot prove exact object absence.
    if root.tag != "Error" or root.attrib:
        return None
    codes = [child for child in list(root) if child.tag == "Code"]
    if len(codes) != 1:
        return None
    code = codes[0]
    if code.attrib or list(code) or code.text != "NoSuchKey":
        return None
    for element in root.iter():
        if element is root or element is code or type(element.tag) is not str:
            continue
        if element.tag.rsplit("}", 1)[-1] == "Code":
            return None
    return "NoSuchKey"


class CurlS3ExactTarget:
    """Exact-key GET/conditional-PUT transport for the manual media migrator only."""

    def __init__(
        self,
        config,
        bindings,
        *,
        curl_path=None,
        popen_factory=subprocess.Popen,
    ):
        self.config = config
        self.keys = {binding["key"] for binding in bindings}
        _require(
            config.endpoint.startswith("https://")
            and len(self.keys) == len(bindings)
            and all(type(key) is str and bool(key) for key in self.keys)
        )
        self.curl = curl_path or verify_curl_sigv4_capability()
        self.popen_factory = popen_factory
        self.closed = False

    def _url(self, key):
        _require(key in self.keys and not self.closed)
        endpoint = urlsplit(self.config.endpoint)
        _require(
            endpoint.scheme == "https"
            and endpoint.hostname
            and not endpoint.username
            and not endpoint.password
            and not endpoint.query
            and not endpoint.fragment
        )
        encoded_key = quote(key, safe="/")
        base = endpoint.path.rstrip("/")
        if self.config.path_style:
            path = f"{base}/{quote(self.config.bucket, safe='')}/{encoded_key}"
            netloc = endpoint.netloc
        else:
            netloc = f"{self.config.bucket}.{endpoint.hostname}"
            if endpoint.port is not None:
                netloc += f":{endpoint.port}"
            path = f"{base}/{encoded_key}"
        url = urlunsplit(("https", netloc, path, "", ""))
        parsed = urlsplit(url)
        _require(
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and unquote(parsed.path).endswith("/" + key)
        )
        return url

    def _argv(self, config_path, method, key, size):
        _require(
            method in {"GET", "PUT"}
            and key in self.keys
            and type(size) is int
            and size >= 0
            and not self.closed
        )
        argv = [
            self.curl,
            "--config",
            config_path,
            "--silent",
            "--max-redirs",
            "0",
            "--retry",
            "0",
            "--connect-timeout",
            str(CONNECT_TIMEOUT),
            "--max-time",
            str(TOTAL_TIMEOUT),
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--noproxy",
            "*",
            "--request",
            method,
            "--dump-header",
            "/dev/stderr",
        ]
        if method == "GET":
            argv += ["--range", f"0-{size}"]
        else:
            argv += [
                "--header",
                "Content-Type: image/png",
                "--header",
                "If-None-Match: *",
                "--data-binary",
                "@-",
            ]
        argv.append(self._url(key))
        rendered = "\0".join(argv)
        _require(
            self.config.access_key not in rendered
            and self.config.secret_key not in rendered
        )
        return argv

    @staticmethod
    def _stop(proc):
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    def get(self, key, size):
        _require(key in self.keys and type(size) is int and size >= 0 and not self.closed)
        path = _credential_config(self.config)
        proc = None
        try:
            proc = self.popen_factory(
                self._argv(path, "GET", key, size),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_curl_env(),
            )
            _require(proc.stdout is not None and proc.stderr is not None)
            status = _read_http_status(proc.stderr)
            if status in {200, 206}:
                payload = proc.stdout.read(size + 1)
                if len(payload) == size + 1:
                    self._stop(proc)
                    return payload
                _require(proc.wait(timeout=TOTAL_TIMEOUT + 5) == 0)
                return payload

            if status == 404:
                body = proc.stdout.read(ERROR_BODY_LIMIT + 1)
                if len(body) > ERROR_BODY_LIMIT:
                    self._stop(proc)
                    raise StorageUnavailable("Exact target read refused.")
                _require(proc.wait(timeout=TOTAL_TIMEOUT + 5) == 0)
                if _s3_error_code(body) == "NoSuchKey":
                    raise StorageMissing("Exact object absent.")
                raise StorageUnavailable("Exact target read refused.")

            self._stop(proc)
            raise StorageUnavailable("Exact target read refused.")
        except StorageMissing:
            raise
        except StorageUnavailable:
            raise
        except Exception:
            if proc is not None:
                self._stop(proc)
            raise StorageUnavailable("Exact target read refused.") from None
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def put_missing(self, key, data):
        _require(key in self.keys and type(data) is bytes and not self.closed)
        path = _credential_config(self.config)
        proc = None
        try:
            proc = self.popen_factory(
                self._argv(path, "PUT", key, len(data)),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=_curl_env(),
            )
            _require(proc.stderr is not None)
            _, raw_headers = proc.communicate(input=data, timeout=TOTAL_TIMEOUT + 5)
            status = _read_http_status(io.BytesIO(raw_headers))
            if proc.returncode != 0 or status not in {200, 201, 204}:
                raise StorageUnavailable("Conditional target create refused.")
        except Exception:
            if proc is not None:
                self._stop(proc)
            raise StorageUnavailable("Conditional target create refused.") from None
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def close(self):
        self.closed = True
