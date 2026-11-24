"""Sixth review round, generic submit: the button a `submit` was planned with is checked again on the
page the write nonce is read from, as the named actions do. A page that changed between the two reads
(the packet-filter toggle flipped, the button's value changed, the button vanished) posts nothing.
"""

import json

import pytest
from save_helpers import client_with, html

from bgwcli import cli


def _page(toggle: str) -> str:
    return (
        '<form method="post" action="/cgi-bin/packetfilter.ha">'
        '<input type="hidden" name="nonce" value="abc123" />'
        f'{toggle}<input type="text" name="rulename" value="r1" /></form>'
    )


ENABLED = _page('<input type="submit" name="Disable" value="Disable Packet Filters" />')
DISABLED = _page('<input type="submit" name="Enable" value="Enable Packet Filters" />')
RENAMED = _page('<input type="submit" name="Disable" value="Disable All Filters" />')
NO_BUTTON = _page("")


def _run(monkeypatch, capsys, first, later):
    reads = {"n": 0}

    def handler(request, n):
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/packetfilter.ha"})
        reads["n"] += 1
        return html(first if reads["n"] == 1 else later)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["submit", "packetfilter", "Disable", "--commit", "--confirm", "PACKETFILTER", "--json"])
    captured = capsys.readouterr().out
    out = json.loads(captured) if captured.strip() else {}
    posts = [r for r in wire.requests if r.method == "POST" and "/login.ha" not in r.url]
    return code, out, posts


@pytest.mark.parametrize("later", [DISABLED, RENAMED, NO_BUTTON], ids=["flipped", "value-changed", "vanished"])
def test_a_submit_button_that_changed_before_the_nonce_read_posts_nothing(tmp_env, clock, monkeypatch, capsys, later):
    code, out, posts = _run(monkeypatch, capsys, ENABLED, later)
    assert posts == [], "nothing is sent when the planned button is not on the nonce page"
    assert code == 2 and out["committed"] is False and out["writeAttempted"] is False
    assert "'Disable' button" in json.dumps(out) and "nothing was sent" in json.dumps(out)


def test_an_unchanged_submit_button_is_still_posted(tmp_env, clock, monkeypatch, capsys):
    _code, _out, posts = _run(monkeypatch, capsys, ENABLED, ENABLED)
    assert len(posts) == 1
    assert "Disable=Disable+Packet+Filters" in posts[0].body.decode()
