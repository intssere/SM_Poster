"""Regression protection against tests touching attached storage or providers."""
import socket

import pytest
import replit.object_storage as sdk


def test_real_attached_storage_client_is_forbidden():
    with pytest.raises(AssertionError, match="Live Object Storage"):
        sdk.Client()


def test_external_dns_is_forbidden():
    with pytest.raises(AssertionError, match="External DNS"):
        socket.getaddrinfo("api.pinterest.com", 443)


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_external_socket_is_forbidden(method):
    with socket.socket() as connection:
        with pytest.raises(AssertionError, match="External connections"):
            getattr(connection, method)(("203.0.113.1", 443))