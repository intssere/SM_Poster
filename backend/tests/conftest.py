import ipaddress
import socket
import os
from pathlib import Path
import subprocess

import pytest
import replit.object_storage as sdk


def _forbid_live_storage_client(*args, **kwargs):
    raise AssertionError("Live Object Storage clients are forbidden in tests")


# Install before test collection, not just before each test: a test module
# importing application code must never attach a real bucket as a side effect.
sdk.Client = _forbid_live_storage_client

# Native curl bypasses Python socket guards. Fence it before collection even
# for ordinary pytest runs, not only the credential-cleared isolation runner.
_original_popen = subprocess.Popen


def fenced_popen(args, *positional, **kwargs):
    command = [args] if isinstance(args, (str, bytes)) else list(args)
    if command and Path(os.fsdecode(command[0])).name == "curl":
        if command[1:] not in [["--version"], ["--help", "all"]]:
            raise AssertionError("Native curl requests forbidden before collection")
    return _original_popen(args, *positional, **kwargs)


subprocess.Popen = fenced_popen

from app.models.domain import PinApproval


@pytest.fixture(autouse=True)
def _only_local_test_network(monkeypatch):
    """Allow disposable PostgreSQL, but never live provider traffic or DNS."""
    resolve = socket.getaddrinfo
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def is_local(host):
        if host is None:
            return True
        if isinstance(host, bytes):
            host = host.decode("ascii")
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def local_resolve(host, *args, **kwargs):
        if not is_local(host):
            raise AssertionError("External DNS is forbidden in tests")
        return resolve(host, *args, **kwargs)

    def local_connect(sock, address):
        if isinstance(address, tuple) and not is_local(address[0]):
            raise AssertionError("External connections are forbidden in tests")
        return connect(sock, address)

    def local_connect_ex(sock, address):
        if isinstance(address, tuple) and not is_local(address[0]):
            raise AssertionError("External connections are forbidden in tests")
        return connect_ex(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", local_resolve)
    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", local_connect_ex)


@pytest.fixture(autouse=True)
def _never_initialize_live_object_storage(monkeypatch):
    """Tests must inject fake storage, even inside a credentialed workspace.

    Adapter tests may replace this sentinel with their own fake SDK client.
    No test may accidentally construct the real attached-bucket client.
    """
    monkeypatch.setattr(sdk, "Client", _forbid_live_storage_client)


@pytest.fixture(autouse=True)
def _normalize_buffer_phase2_original_provenance(request, monkeypatch):
    """Keep the historical phase-2 fixture aligned with valid approved-original provenance.

    test_buffer_phase2 predates ContentRevision persistence and synthesizes a non-null
    revision identity without creating that revision. Reconciliation now correctly
    rejects that impossible record. Patch only that test module's helper so its
    intended original-draft fixture is explicit and structurally valid.
    """
    module = request.module
    if module.__name__ != "test_buffer_phase2" or not hasattr(module, "_ready_publication"):
        return

    original = module._ready_publication

    def valid_ready_publication(db, *args, **kwargs):
        publication = original(db, *args, **kwargs)
        if kwargs.get("dispatch_provider") == "buffer":
            approval = db.get(PinApproval, publication.approval_id)
            approval.revision_id = None
            approval.approved_version_id = "original"
            publication.revision_id = None
            db.commit()
        return publication

    monkeypatch.setattr(module, "_ready_publication", valid_ready_publication)
