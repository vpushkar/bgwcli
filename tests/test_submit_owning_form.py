"""`submit <page> <button>` only posts the page's own form; a button of another form is refused."""

from __future__ import annotations

import json

import pytest
from save_helpers import FakeTransport

from bgwcli import cli
from bgwcli.client import BGW320Client, RawResponse

HOME = (
    "<html><head><title>Status</title></head><body>"
    '<form method="post" action="/cgi-bin/crestart.ha?1"><input type="hidden" name="nonce" value="aaaa01">'
    '<input type="submit" name="Broadband" value="Restart"></form>'
    '<form method="post" action="/cgi-bin/wrestart.ha?1"><input type="hidden" name="nonce" value="bbbb01">'
    '<input type="submit" name="WRestart1" value="Restart"></form>'
    '<form method="post" action="/cgi-bin/other.ha"><input type="hidden" name="nonce" value="dddd01">'
    '<input type="submit" name="Other" value="Other"></form>'
    '<form method="post" action="/cgi-bin/home.ha"><input type="hidden" name="nonce" value="cccc01">'
    '<input type="text" name="note" value="x">'
    '<input type="submit" name="Save" value="Save"></form></body></html>'
)


def _response(body: str, status: int = 200, headers: dict[str, str] | None = None) -> RawResponse:
    return RawResponse(
        status=status, reason="OK",
        headers=[("content-type", "text/html")] + list((headers or {}).items()), body=body.encode(),
    )


@pytest.fixture
def router(monkeypatch, tmp_env):
    monkeypatch.setenv("BGW_ACCESS_CODE", "12345")
    posts: list[tuple[str, str]] = []

    def handler(req, n):
        if req.method == "POST":
            posts.append((req.url, req.body.decode()))
            return _response("", 302, {"location": "/cgi-bin/home.ha"})
        return _response(HOME)

    transport = FakeTransport(handler)
    client = BGW320Client(
        "http://router.local", access_code="12345", timeout_ms=1000, insecure_tls=True,
        user_agent="test", transport=transport,
    )
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "t"}})
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    return posts


def test_button_of_another_form_with_an_action_is_refused_naming_it(router, capsys):
    code = cli.main(["submit", "home", "WRestart1", "--commit", "--confirm", "HOME"])
    err = capsys.readouterr().err
    assert code == 1
    assert router == []
    assert "action restart-wifi-2.4" in err


def test_button_of_another_form_without_an_action_says_so(router, capsys):
    code = cli.main(["submit", "home", "Other", "--commit", "--confirm", "HOME"])
    err = capsys.readouterr().err
    assert code == 1
    assert router == []
    assert "another form" in err
    assert "action " not in err


def test_dry_run_shows_the_same_refusal(router, capsys):
    code = cli.main(["submit", "home", "WRestart1"])
    err = capsys.readouterr().err
    assert code == 1
    assert router == []
    assert "action restart-wifi-2.4" in err


def test_button_of_the_pages_own_form_still_posts(router, clock, capsys):
    code = cli.main(["submit", "home", "Save", "--commit", "--confirm", "HOME", "--json"])
    capsys.readouterr()
    assert code in (0, 1, 2)
    assert len(router) == 1
    assert router[0][0].endswith("/cgi-bin/home.ha")


def test_restart_wifi_action_still_posts_to_its_own_path(router, capsys):
    code = cli.main(["action", "restart-wifi-2.4", "--commit", "--confirm", "RESTART-WIFI", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert len(router) == 1
    assert "wrestart.ha?1" in router[0][0]
    assert "bbbb01" in router[0][1]
    assert out["committed"] is True
