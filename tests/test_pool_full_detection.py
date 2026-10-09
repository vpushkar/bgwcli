"""The pool-full phrase is a pool-full answer only on a login-shaped page; inside a content page it is data."""

from __future__ import annotations

import json

import pytest
from save_helpers import FakeTransport, client_with, form, html

from bgwcli import cli
from bgwcli.client import BGW320Client, router_sessions_full
from bgwcli.errors import RouterSessionPoolFullError

PHRASE = "All Web Server Sessions Are In Use"
DEVICES = (
    f"<html><title>Devices</title><body><table><tr><th>Name</th></tr><tr><td>{PHRASE}</td></tr></table></body></html>"
)
POOL_FULL = f"<title>Login</title><p>{PHRASE}</p>"


def test_the_phrase_alone_in_a_content_page_is_not_pool_full():
    assert router_sessions_full(DEVICES) is False
    assert router_sessions_full("<title>Logs</title><pre>all web server sessions are in use</pre>") is False


def test_the_phrase_on_a_login_shaped_page_is_pool_full():
    assert router_sessions_full(POOL_FULL) is True
    assert router_sessions_full(f"<h1>Login</h1><p>{PHRASE}</p>") is True
    assert router_sessions_full("<title>Login</title>nothing else") is False


def test_a_content_page_with_the_phrase_in_a_cell_is_returned_as_data():
    client, wire = client_with(lambda r, n: html(DEVICES))
    response = client.get_cgi_page("devices")
    assert response.status_code == 200 and PHRASE in response.body
    assert len(wire.calls) == 1


def test_a_write_answered_by_a_content_page_holding_the_phrase_is_not_pool_full():
    def handler(request, number):
        if request.method == "POST":
            return html(DEVICES)
        return html(form("dosprotect", "old"))

    client, wire = client_with(handler)
    response = client.post_cgi_page("dosprotect", {"setting": "x"})
    assert response.status_code == 200
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("path", ["get", "post"])
def test_a_login_shaped_answer_is_still_pool_full(path):
    def handler(request, number):
        return html(POOL_FULL) if path == "get" or request.method == "POST" else html(form("dosprotect", "o"))

    client, wire = client_with(handler)
    with pytest.raises(RouterSessionPoolFullError):
        if path == "get":
            client.get_cgi_page("devices")
        else:
            client.post_cgi_page("dosprotect", {"setting": "x"})


def test_a_pool_full_read_answer_with_wait_for_session_and_no_access_code_is_pool_full_not_access_code_required():
    transport = FakeTransport(lambda request, number: html(POOL_FULL))
    client = BGW320Client(
        "http://router.local", timeout_ms=1000, insecure_tls=True, user_agent="test", transport=transport,
        wait_for_session=True,
    )
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "test"}})
    with pytest.raises(RouterSessionPoolFullError) as info:
        client.get_cgi_page("devices")
    assert "Access code required" not in str(info.value)
    assert [r.method for r in transport.requests] == ["GET"]
    assert client.login_attempts == 0
    assert not client.has_authenticated_session()


def test_page_with_wait_for_session_and_no_access_code_reports_pool_full(capsys, monkeypatch, tmp_env):
    transport = FakeTransport(lambda request, number: html(POOL_FULL))

    def factory(options, access_code, **kwargs):
        client = BGW320Client(
            "http://router.local", access_code=access_code, timeout_ms=1000, insecure_tls=True,
            user_agent="test", transport=transport, wait_for_session=options.wait_for_session,
        )
        client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "t"}})
        return client

    monkeypatch.setattr(cli, "_client_factory", factory)
    code = cli.main(["page", "devices", "--wait-for-session", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert out["sessionPoolFull"] is True and out["exitCode"] == 2
    assert "Access code" not in out["error"]
    assert [r.method for r in transport.requests] == ["GET"]


def test_a_pool_full_write_answer_without_an_access_code_is_pool_full_not_access_code_required():
    from bgwcli.client import BGW320Client

    def handler(request, number):
        return html(POOL_FULL) if request.method == "POST" else html(form("dosprotect", "old"))

    from save_helpers import FakeTransport

    transport = FakeTransport(handler)
    client = BGW320Client(
        "http://router.local", timeout_ms=1000, insecure_tls=True, user_agent="test", transport=transport
    )
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "test"}})
    with pytest.raises(RouterSessionPoolFullError):
        client.post_cgi_page("dosprotect", {"setting": "x"})
    assert sum(r.method == "POST" for r in transport.requests) == 1
    assert client.login_attempts == 0
    assert not client.has_authenticated_session()
