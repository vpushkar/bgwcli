"""Seventh review round, channel scan: the radio-dropping Continue's own answer is still read.

`find-best-channel-5` reads nothing after an accepted Continue (the radio is re-tuning), but the
Continue POST's inline answer is already in hand: a 200 carrying a rejection banner is a rejection
(exit 1), a "No changes detected" banner is unchanged (exit 0, not committed), and the result reports
Continue's own HTTP status, not the Save's.
"""

import json
from urllib.parse import urlsplit

import pytest
from save_helpers import client_with, html

from bgwcli import cli

SCAN_FORM = (
    '<form action="/cgi-bin/wconfig.ha"><input name="nonce" value="abc123">'
    '<input type="text" name="setting" value="old">'
    '<input type="submit" name="chanscan5" value="Find Best Channel"></form>'
)
WARNING_PAGE = (
    '<form method="post" action="/cgi-bin/wconfig.ha"><input type="hidden" name="nonce" value="aa01">'
    '<input type="submit" name="Continue" value="Continue"></form>'
    '<form method="post" action="/cgi-bin/wifiwarn_advanced.ha"><input type="hidden" name="nonce" value="aa02">'
    '<input type="submit" name="Cancel" value="Cancel"></form>'
)
REJECTED = (
    '<img id="error-message-icon" src="/images/icon_error.png">'
    '<div id="error-message-text">A required setting is empty.</div>' + SCAN_FORM
)
NO_CHANGES = '<div id="error-message-text">No changes detected. Save not performed.</div>' + SCAN_FORM
SCAN = ["action", "find-best-channel-5", "--commit", "--confirm", "CHANSCAN", "--json"]


def _scan(monkeypatch, capsys, continue_answer):
    state = {"continued": False}

    def handler(request, n):
        path = urlsplit(request.url).path
        if request.method == "POST":
            if "Continue" in request.body.decode():
                state["continued"] = True
                return continue_answer()
            return html("", 302, {"location": "/cgi-bin/wifiwarn_advanced.ha"})
        if state["continued"]:
            raise ConnectionResetError("the 5 GHz radio is re-tuning")
        if "wifiwarn" in path:
            return html(WARNING_PAGE)
        return html(SCAN_FORM)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(SCAN)
    captured = capsys.readouterr().out
    out = json.loads(captured) if captured.strip() else {}
    posts = [r for r in wire.requests if r.method == "POST" and "/login.ha" not in r.url]
    after_continue = wire.requests[wire.requests.index(posts[-1]) + 1 :] if posts else []
    return code, out, posts, after_continue


def test_a_continue_answered_with_a_rejection_banner_is_a_rejection(monkeypatch, capsys, tmp_env, clock):
    code, out, posts, after_continue = _scan(monkeypatch, capsys, lambda: html(REJECTED))
    assert len(posts) == 2 and "Continue" in posts[1].body.decode()
    assert after_continue == []
    assert code == 1, out
    assert out["committed"] is False and out["statusCode"] == 200
    assert "A required setting is empty." in out["warning"]


def test_a_continue_answered_no_changes_is_unchanged(monkeypatch, capsys, tmp_env, clock):
    code, out, posts, after_continue = _scan(monkeypatch, capsys, lambda: html(NO_CHANGES))
    assert len(posts) == 2 and after_continue == []
    assert code == 0, out
    assert out["committed"] is False and out["outcome"] == "unchanged"


@pytest.mark.parametrize("status", [302, 303])
def test_an_accepted_continue_reports_its_own_status(monkeypatch, capsys, tmp_env, clock, status):
    code, out, _, after_continue = _scan(
        monkeypatch, capsys, lambda: html("", status, {"location": "/cgi-bin/wconfig.ha"})
    )
    assert after_continue == []
    assert code == 0, out
    assert out["committed"] is True and out["answerRead"] is False
    assert out["statusCode"] == status
