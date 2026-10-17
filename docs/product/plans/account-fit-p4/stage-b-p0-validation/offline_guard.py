"""Temporary validation guard; never installed in the application."""
import ipaddress
import socket

import pytest


@pytest.fixture(autouse=True, scope='session')
def p0_loopback_connections_only():
    original = socket.socket.connect
    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0]
            if host != 'localhost':
                try:
                    permitted = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    permitted = False
                if not permitted:
                    raise AssertionError('08-P0 offline validation blocked a non-loopback connection')
        return original(sock, address)
    socket.socket.connect = connect
    try:
        yield
    finally:
        socket.socket.connect = original
