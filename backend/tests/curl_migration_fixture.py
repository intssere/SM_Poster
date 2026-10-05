"""Memory subprocess double. Never executes curl or opens a network connection."""
import io
from pathlib import Path
import re
import stat
import subprocess
import threading
from urllib.parse import urlsplit, unquote


def fields(text):
    result = {}
    for line in text.splitlines():
        name, value = line.split(" = ", 1)
        # Same escaped quote/backslash syntax accepted by curl config files.
        value = re.sub(r'\\(["\\])', r'\1', value[1:-1])
        result.setdefault(name, []).append(value)
    return result


class CurlMemoryProcess:
    def __init__(self, owner, argv, **kwargs):
        assert kwargs["shell"] is False
        assert kwargs["env"].keys() == {"PATH", "HOME", "LANG", "LC_ALL"}
        assert kwargs["stdout"] == subprocess.PIPE and kwargs["stderr"] == subprocess.PIPE
        path = Path(argv[argv.index("--config") + 1])
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert path.is_relative_to("/tmp")
        text = path.read_text()
        config = fields(text)
        url = config["url"][0]
        key = "creative/" + unquote(urlsplit(url).path.split("/creative/", 1)[1])
        method = config["request"][0]
        owner.calls.append((method, key))
        owner.records.append((list(argv), config, text, path, dict(kwargs["env"])))
        self.returncode = None
        self.done = threading.Event()
        self.owner, self.key, self.method = owner, key, method
        code, status, data = 0, 200, b""
        extra = b""
        if method == "GET":
            status = 200 if key in owner.values else 404
            data = owner.values.get(key, b"")
            if owner.range_response and status == 200:
                status = 206
                extra = f"Content-Range: bytes 0-{len(data)-1}/{len(data)}\r\n".encode()
            if owner.corrupt_readback and any(m == "PUT" and k == key for m, k in owner.calls):
                data += b"!"
        else:
            assert config["data-binary"] == ["@-"]
            assert "If-None-Match: *" in config["header"]
            assert "Content-Type: image/png" in config["header"]
            status = 412 if key in owner.values or owner.race else 200
        if owner.response is not None:
            code, status, data, extra = owner.response
        self.code, self.status = code, status
        header = f"HTTP/1.1 {status} fixture\r\n".encode() + extra + b"\r\n"
        Path(config["dump-header"][0]).write_bytes(header)
        self.stdout = io.BytesIO(data)
        self.stderr = io.BytesIO(str(status).encode())
        process = self
        class Input(io.BytesIO):
            def close(self):
                if not self.closed:
                    if process.status == 200 and process.method == "PUT":
                        owner.values[key] = self.getvalue()
                    super().close()
                process.done.set()
        self.stdin = Input() if kwargs["stdin"] == subprocess.PIPE else None
        if self.stdin is None:
            self.done.set()

    def wait(self, timeout=None):
        if self.owner.timeout:
            raise subprocess.TimeoutExpired("redacted", timeout)
        assert self.done.wait(timeout=1)
        if self.returncode is None:
            self.returncode = self.code
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.owner.kills += 1
        self.returncode = -9
        self.done.set()
        # Make the post-kill reap succeed in timeout tests.
        self.owner.timeout = False


class CurlMemory:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls, self.records = [], []
        self.response = None
        self.range_response = self.race = self.corrupt_readback = self.timeout = False
        self.kills = 0

    def __call__(self, argv, **kwargs):
        return CurlMemoryProcess(self, argv, **kwargs)
