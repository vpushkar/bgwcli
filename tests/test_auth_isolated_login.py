"""`auth` and every credentialed login verify the access code with a fresh cookie jar.

The gateway can answer a wrong code with an ordinary 200 page; login() then probes a protected page.
That probe used to carry whatever session cookie the client already held, so a still-valid cached
session made a wrong code pass `bgwcli auth` (Codex review, v0.3.0). The probe must also show a
recognisable page, not just "not the login page".
"""

import json
from urllib.parse import urlsplit

import pytest

from bgwcli import cli
from bgwcli.client import BGW320Client, RawResponse
from bgwcli.errors import RouterAuthError

LOGIN = (
    '<html><head><title>Login</title></head><body><form method="post" action="/cgi-bin/login.ha">'
    '<input type="hidden" name="nonce" value="abc123"><input type="password" id="password" name="password">'
    '<input type="submit" name="Continue" value="Continue"></form></body></html>'
)
SERVICES = (
    "<html><head><title>Custom Services</title></head><body>"
    "<table><tr><th>Service</th></tr></table></body></html>"
)


def html(body, status=200, headers=None):
    pairs = [("content-type", "text/html"), *(headers or {}).items()]
    return RawResponse(status=status, reason="OK", headers=pairs, body=body.encode())


def gateway(*, code_ok: bool, probe_body: str = SERVICES):
    """A gateway holding one valid session (cookie SessionID=good). A login GET without a cookie hands
    out an unauthenticated SessionID=anon; a wrong code is answered 200 with a plain page; protected
    pages render only for the good session (or a session the right code logged in)."""
    state = {"logged_in": set()}

    def handler(request):
        path = urlsplit(request.url).path
        cookie = request.headers.get("Cookie", "")
        if path == "/cgi-bin/login.ha" and request.method == "GET":
            headers = {} if cookie else {"set-cookie": "SessionID=anon; Path=/"}
            return html(LOGIN, headers=headers)
        if path == "/cgi-bin/login.ha" and request.method == "POST":
            if code_ok:
                state["logged_in"].add(cookie)
                return html("", 302, {"location": "/cgi-bin/home.ha"})
            return html("<html><head><title>Please wait</title></head><body>Processing</body></html>")
        if "SessionID=good" in cookie or cookie in state["logged_in"]:
            return html(probe_body)
        return html(LOGIN)

    return handler


def cached_client(handler, code="wrong-code"):
    client = BGW320Client("http://router.local", access_code=code, timeout_ms=1000, transport=handler)
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"SessionID": "good"}})
    return client


def test_a_forced_login_with_a_wrong_code_fails_despite_a_valid_cached_session():
    client = cached_client(gateway(code_ok=False))
    with pytest.raises(RouterAuthError):
        client.login(force=True)
    assert client.has_authenticated_session() is False


def test_a_forced_login_with_the_right_code_still_passes():
    client = cached_client(gateway(code_ok=True), code="12345")
    client.login(force=True)
    assert client.has_authenticated_session() is True


@pytest.mark.parametrize("probe_body", ["", "<html><body></body></html>", "<html><head><title></title></head></html>"])
def test_an_unrecognisable_probe_page_is_no_proof_of_login(probe_body):
    # Not the login page, but not a page either: an empty answer proves nothing about the code.
    def handler(request):
        path = urlsplit(request.url).path
        if path == "/cgi-bin/login.ha" and request.method == "GET":
            return html(LOGIN, headers={"set-cookie": "SessionID=anon; Path=/"})
        if path == "/cgi-bin/login.ha":
            return html("<html><head><title>Please wait</title></head><body>Processing</body></html>")
        return html(probe_body)

    client = BGW320Client("http://router.local", access_code="wrong", timeout_ms=1000, transport=handler)
    with pytest.raises(RouterAuthError):
        client.login()
    assert client.has_authenticated_session() is False


def test_auth_command_rejects_a_wrong_code_behind_a_cached_session(monkeypatch, capsys, tmp_env):
    client = cached_client(gateway(code_ok=False))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["auth", "--json"])
    out = capsys.readouterr().out
    assert code != 0
    data = json.loads(out) if out.strip() else {}
    assert data.get("authenticated") is not True
