import ipaddress
import socket

import pytest

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def _is_loopback_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip().strip("[]")
    if host == "" or host.lower() in _LOOPBACK_NAMES:
        return True
    host = host.split("%", 1)[0]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_loopback_address(address) -> bool:
    if isinstance(address, (str, bytes)):
        return True  # AF_UNIX path
    try:
        return _is_loopback_host(address[0])
    except (TypeError, IndexError):
        return False


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch):
    """No test may reach a non-loopback host: a connect or name lookup to anything else fails the test.

    The violation is recorded as well as raised, so code that swallows exceptions still fails the test.
    """
    violations: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self, address):
        if not _is_loopback_address(address):
            violations.append(f"connect {address!r}")
            raise AssertionError(f"test attempted a network connect to {address!r}")
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if not _is_loopback_address(address):
            violations.append(f"connect_ex {address!r}")
            raise AssertionError(f"test attempted a network connect to {address!r}")
        return real_connect_ex(self, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_loopback_host(host):
            violations.append(f"getaddrinfo {host!r}")
            raise AssertionError(f"test attempted a name lookup of {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def refuse_lookup(name):
        def guarded(host, *args, **kwargs):
            if not _is_loopback_host(host):
                violations.append(f"{name} {host!r}")
                raise AssertionError(f"test attempted a name lookup of {host!r}")
            return real[name](host, *args, **kwargs)

        return guarded

    real = {
        name: getattr(socket, name)
        for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getfqdn")
    }
    for name in real:
        monkeypatch.setattr(socket, name, refuse_lookup(name))

    real_create_connection = socket.create_connection

    def guarded_create_connection(address, *args, **kwargs):
        if not _is_loopback_address(address):
            violations.append(f"create_connection {address!r}")
            raise AssertionError(f"test attempted a network connect to {address!r}")
        return real_create_connection(address, *args, **kwargs)

    real_sendto = socket.socket.sendto
    real_sendmsg = socket.socket.sendmsg

    def guarded_sendto(self, data, *args):
        address = args[-1] if args else None
        if address is not None and not _is_loopback_address(address):
            violations.append(f"sendto {address!r}")
            raise AssertionError(f"test attempted a network send to {address!r}")
        return real_sendto(self, data, *args)

    def guarded_sendmsg(self, buffers, *args):
        address = args[2] if len(args) > 2 else None
        if address is not None and not _is_loopback_address(address):
            violations.append(f"sendmsg {address!r}")
            raise AssertionError(f"test attempted a network send to {address!r}")
        return real_sendmsg(self, buffers, *args)

    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)
    monkeypatch.setattr(socket.socket, "sendmsg", guarded_sendmsg)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield violations
    assert not violations, f"network access attempted: {violations}"


@pytest.fixture
def tmp_env(monkeypatch, tmp_path):
    """Isolate every env var the CLI reads and point caches/dumps at tmp_path."""
    for name in [
        "BGW_HOST", "ROUTER_IP", "BGW_ACCESS_CODE", "BGW_TIMEOUT_MS", "BGW_INSECURE_TLS", "BGW_WAIT_FOR_SESSION",
        "BGW_SESSION_WAIT_TIMEOUT_MS", "BGW_SESSION_WAIT_INTERVAL_MS", "BGW_SESSION_CACHE_TTL_MS",
        "BGW_SESSION_POOL_COOLDOWN_MS", "BGW_SESSION_LOCK_TIMEOUT_MS", "BGW_DUMP_DIR", "XDG_STATE_HOME",
        "BGW_SESSION_CACHE_DIR", "XDG_CACHE_HOME", "BGW_SESSION_LOCK_STALE_MS", "BGW_FALLBACK_ACCESS_CODE",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("BGW_DUMP_DIR", str(tmp_path / "dumps"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture(autouse=True)
def verify_sleeps(monkeypatch):
    """Never sleep for real between verification re-read attempts; record the requested delays."""
    from bgwcli import cli

    delays: list[float] = []
    monkeypatch.setattr(cli, "sleep", delays.append, raising=False)
    return delays


@pytest.fixture
def backend(monkeypatch):
    """Fake sweep seams (tests/sweep_helpers.py); returns the recorded sleeps."""
    from sweep_helpers import install_backend

    return install_backend(monkeypatch)


@pytest.fixture
def clock(monkeypatch):
    """Fake save-confirmation clock (tests/save_helpers.py); modules with their own clock fixture override it."""
    from save_helpers import install_clock

    return install_clock(monkeypatch)
