"""Enable custom integrations and restrict simulator sockets to loopback."""

import pytest
from pytest_socket import socket_allow_hosts


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations, socket_enabled):
    socket_allow_hosts(["127.0.0.1", "::1"], allow_unix_socket=True)
