"""Plain and post_path actions read their source page first; a page with no form control is refused
before any POST (exit 2, structural)."""

from __future__ import annotations

import json

import pytest
from save_helpers import FakeTransport

from bgwcli import cli
from bgwcli.client import BGW320Client, RawResponse

WAIT = "<html><head><title>Please wait</title></head><body>Please wait...</body></html>"
DEVICES = (
    "<html><head><title>Devices</title></head><body>"
    '<form method="post" action="/cgi-bin/devices.ha"><input type="hidden" name="nonce" value="abcd01">'
    '<input type="submit" name="Clear" value="Clear Device List"></form></body></html>'
)
HOME = (
    "<html><head><title>Status</title></head><body>"
    '<form method="post" action="/cgi-bin/wrestart.ha?1"><input type="hidden" name="nonce" value="bbbb01">'
    '<input type="submit" name="WRestart1" value="Restart"></form></body></html>'
)


def _response(body: str, status: int = 200, headers: dict[str, str] | None = None) -> RawResponse:
    return RawResponse(
        status=status, reason="OK",
        headers=[("content-type", "text/html")] + list((headers or {}).items()), body=body.encode(),
    )


def _router(monkeypatch, body: str):
    monkeypatch.setenv("BGW_ACCESS_CODE", "12345")
    posts: list[str] = []

    def handler(req, n):
        if req.method == "POST":
            posts.append(req.body.decode())
            return _response("", 302, {"location": "/cgi-bin/home.ha"})
        return _response(body)

    client = BGW320Client(
        "http://router.local", access_code="12345", timeout_ms=1000, insecure_tls=True,
        user_agent="test", transport=FakeTransport(handler),
    )
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "t"}})
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    return posts


def test_plain_action_on_a_please_wait_page_posts_nothing(monkeypatch, tmp_env, capsys):
    posts = _router(monkeypatch, WAIT)
    code = cli.main(["action", "clear-device-list", "--commit", "--confirm", "CLEAR-DEVICES", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert posts == []
    assert out["ok"] is False
    assert "without any form controls" in out["error"]


def test_post_path_action_on_a_please_wait_page_posts_nothing(monkeypatch, tmp_env, capsys):
    posts = _router(monkeypatch, WAIT)
    code = cli.main(["action", "restart-wifi-2.4", "--commit", "--confirm", "RESTART-WIFI", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert posts == []
    assert out["ok"] is False
    assert "without any form controls" in out["error"]


@pytest.mark.parametrize(
    ("args", "token", "body", "needle"),
    [
        (["clear-device-list"], "CLEAR-DEVICES", DEVICES, "Clear=Clear+Device+List"),
        (["restart-wifi-2.4"], "RESTART-WIFI", HOME, "nonce=bbbb01"),
    ],
)
def test_a_readable_source_page_still_posts_once(monkeypatch, tmp_env, capsys, args, token, body, needle):
    posts = _router(monkeypatch, body)
    code = cli.main(["action", *args, "--commit", "--confirm", token, "--json"])
    capsys.readouterr()
    assert code == 0
    assert len(posts) == 1
    assert needle in posts[0]
