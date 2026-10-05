"""Manual migration-only exact S3 target using curl with AWS SigV4."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import re
from xml.parsers import expat
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from app.services.media_storage import StorageMissing, StorageUnavailable
from .one_shot_migration import _require


CONNECT_TIMEOUT = 10
TOTAL_TIMEOUT = 30
MAX_ERROR_BYTES = 4096
MAX_HEADER_BYTES = 65536


def _no_such_key(body, key):
    """Parse only a small S3 Error envelope; never retain or report diagnostics."""
    if not body or len(body) > MAX_ERROR_BYTES:
        return False
    parser = expat.ParserCreate(namespace_separator="}")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    stack, seen, code, returned_key = [], set(), [], []

    def forbidden(*args):
        raise ValueError("Refused")

    def start(name, attributes):
        namespace, _, local = name.rpartition("}")
        if namespace not in {"", "http://s3.amazonaws.com/doc/2006-03-01/"}:
            forbidden()
        local = local if namespace else name
        if attributes or len(stack) >= 2:
            forbidden()
        if not stack:
            if local != "Error" or "Error" in seen:
                forbidden()
        elif local not in {"Code", "Message", "Key", "Resource", "RequestId", "HostId"}:
            forbidden()
        if local in seen:
            forbidden()
        seen.add(local)
        stack.append(local)

    def text(value):
        if stack == ["Error", "Code"]:
            code.append(value)
        elif stack == ["Error", "Key"]:
            returned_key.append(value)
        elif len(stack) <= 1 and value.strip():
            forbidden()

    parser.StartElementHandler = start
    parser.EndElementHandler = lambda name: stack.pop()
    parser.CharacterDataHandler = text
    parser.StartDoctypeDeclHandler = forbidden
    parser.EntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    parser.ProcessingInstructionHandler = forbidden
    try:
        parser.Parse(body, True)
        return (not stack and "".join(code) == "NoSuchKey"
                and ("Key" not in seen or "".join(returned_key) == key))
    except Exception:
        return False


def _header_status(path):
    """Headers are private and bounded; they are never returned to callers."""
    with open(path, "rb") as stream:
        raw = stream.read(MAX_HEADER_BYTES + 1)
    if not raw or len(raw) > MAX_HEADER_BYTES or not raw.endswith(b"\r\n\r\n"):
        raise StorageUnavailable("Exact target read refused.")
    blocks = raw.rstrip(b"\r\n").split(b"\r\n\r\n")
    statuses = []
    for block in blocks:
        line = block.split(b"\r\n", 1)[0]
        match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: [^\r\n]*)?", line)
        if match is None:
            raise StorageUnavailable("Exact target read refused.")
        statuses.append(int(match[1]))
    if any(status >= 200 for status in statuses[:-1]):
        raise StorageUnavailable("Exact target read refused.")
    return statuses[-1]


def _stop(proc):
    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


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


def _status(raw):
    try:
        value = raw.decode("ascii").strip()
    except Exception:
        return None
    return int(value) if len(value) == 3 and value.isdigit() else None


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
            "-q",
            "--config",
            config_path,
            "--silent",
            "--no-location",
            "--http1.1",
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
            "--write-out",
            "%{stderr}%{http_code}",
        ]
        if method == "GET":
            argv += ["--range", f"0-{size}"]
        else:
            argv += [
                "--header",
                "Content-Type: image/png",
                "--header",
                "If-None-Match: *",
                "--header",
                "Expect:",
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

    def get(self, key, size):
        _require(key in self.keys and type(size) is int and size >= 0 and not self.closed)
        path = _credential_config(self.config)
        proc, headers = None, None
        try:
            fd, headers = tempfile.mkstemp(
                prefix="sm-poster-s3-", suffix=".headers", dir=str(Path(path).parent))
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            argv = self._argv(path, "GET", key, size)
            argv[2:2] = ["--dump-header", headers]
            proc = self.popen_factory(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_curl_env(),
            )
            _require(proc.stdout is not None and proc.stderr is not None)
            # The first bounded read ensures curl has emitted headers, without
            # allocating an expected-image-size buffer for an error response.
            payload = proc.stdout.read(min(size + 1, MAX_ERROR_BYTES + 1))
            status = _header_status(headers)
            if status == 404:
                if len(payload) <= MAX_ERROR_BYTES:
                    payload += proc.stdout.read(MAX_ERROR_BYTES + 1 - len(payload))
                if len(payload) > MAX_ERROR_BYTES:
                    raise StorageUnavailable("Exact target read refused.")
                returncode = proc.wait(timeout=TOTAL_TIMEOUT + 5)
                if (returncode == 0 and _status(proc.stderr.read(16)) == status
                        and _no_such_key(payload, key)):
                    raise StorageMissing("Exact object absent.")
                raise StorageUnavailable("Exact target read refused.")
            if status not in {200, 206}:
                raise StorageUnavailable("Exact target read refused.")
            if len(payload) < size + 1:
                payload += proc.stdout.read(size + 1 - len(payload))

            # If the response already exceeds the expected object size, stop reading
            # immediately. The existing migration verifier will classify it CONFLICT.
            if len(payload) == size + 1:
                _stop(proc)
                return payload

            returncode = proc.wait(timeout=TOTAL_TIMEOUT + 5)
            if returncode != 0 or _status(proc.stderr.read(16)) != status:
                raise StorageUnavailable("Exact target read refused.")
            return payload
        except StorageMissing:
            raise
        except Exception:
            _stop(proc)
            raise StorageUnavailable("Exact target read refused.") from None
        finally:
            for pipe in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass
            for temporary in (path, headers):
                if temporary is not None:
                    try:
                        os.unlink(temporary)
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
            _, raw_status = proc.communicate(input=data, timeout=TOTAL_TIMEOUT + 5)
            status = _status(raw_status[:16])
            if proc.returncode != 0 or status not in {200, 201, 204}:
                raise StorageUnavailable("Conditional target create refused.")
        except Exception:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            raise StorageUnavailable("Conditional target create refused.") from None
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def close(self):
        self.closed = True
