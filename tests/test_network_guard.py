import socket

import pytest


def test_connect_to_a_routable_address_is_refused(_network_guard):
    sock = socket.socket()
    try:
        with pytest.raises(AssertionError, match="network connect"):
            sock.connect(("192.0.2.1", 80))
    finally:
        sock.close()
    assert _network_guard == ["connect ('192.0.2.1', 80)"]
    _network_guard.clear()


def test_name_lookup_of_a_remote_host_is_refused(_network_guard):
    with pytest.raises(AssertionError, match="name lookup"):
        socket.getaddrinfo("router.invalid", 80)
    assert _network_guard == ["getaddrinfo 'router.invalid'"]
    _network_guard.clear()


def test_loopback_connections_still_work():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.create_connection(server.getsockname(), timeout=2)
    client.close()
    server.close()
    assert socket.getaddrinfo("localhost", 80)


@pytest.mark.parametrize(
    "call",
    [
        lambda: socket.gethostbyname("router.invalid"),
        lambda: socket.gethostbyname_ex("router.invalid"),
        lambda: socket.gethostbyaddr("192.0.2.1"),
        lambda: socket.getfqdn("192.0.2.1"),
        lambda: socket.create_connection(("192.0.2.1", 80), timeout=1),
        lambda: _sendto(),
        lambda: _sendmsg(),
    ],
    ids=["gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getfqdn", "create_connection", "sendto", "sendmsg"],
)
def test_every_other_network_entry_point_is_refused(_network_guard, call):
    with pytest.raises(AssertionError, match="network|name lookup"):
        call()
    assert len(_network_guard) == 1
    _network_guard.clear()


def _sendto():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(b"x", ("192.0.2.1", 9))
    finally:
        sock.close()


def _sendmsg():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendmsg([b"x"], [], 0, ("192.0.2.1", 9))
    finally:
        sock.close()


def test_loopback_datagrams_and_lookups_still_work():
    assert socket.gethostbyname("localhost")
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.sendto(b"x", receiver.getsockname())
    sender.close()
    receiver.close()
