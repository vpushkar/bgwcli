"""When `submit` is and is not re-read after an acknowledged save, and what the re-read decides (the
contract the README states): a plain applied submit is never re-read; only an acknowledged save whose
known state is not visible is, with or without requested values."""

from __future__ import annotations

import json

from integration_html import allocation_saved_html
from save_helpers import SAVED_RED, client_with, form, html

from bgwcli import cli

MAC = "02:0a:0b:0c:0d:02"
IP = "192.168.1.64"


def _selected_entry_page() -> str:
    return (
        '<html><body><form method="post" action="/cgi-bin/ipalloc.ha">'
        '<input type="hidden" name="nonce" value="ab12">'
        f'<select name="alloc_{MAC}"><option value="normal">Address from DHCP pool</option>'
        f'<option value="{IP}" selected>Private fixed:{IP}</option></select>'
        '<input type="submit" name="Save" value="Save"></form></body></html>'
    )


def _run(monkeypatch, capsys, handler, argv):
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--commit", "--confirm", argv[1].upper(), "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    return code, out, posts


def test_a_plain_applied_submit_is_not_re_read(tmp_env, clock, monkeypatch, capsys):
    state = {"posted": False}

    def handler(request, _n):
        if request.method == "POST":
            state["posted"] = True
            return html("", 302, {"location": "/cgi-bin/etherlan.ha"})
        return html(form("etherlan", "new", banner=SAVED_RED) if state["posted"] else form("etherlan", "old"))

    code, out, posts = _run(monkeypatch, capsys, handler, ["submit", "etherlan", "Save", "setting=new"])
    assert len(posts) == 1
    assert code == 0 and out["committed"] is True and out["outcome"] == "applied"
    assert "verified" not in out and "verifyAttempts" not in out


def test_a_submit_without_requested_values_posts_no_known_state_and_is_not_re_read(
    tmp_env, clock, monkeypatch, capsys
):
    """The generic submit payload carries only the requested controls plus the button, so without
    requested values there is no reservation or LAN state to wait for or re-read."""
    state = {"posted": False}

    def handler(request, _n):
        if request.method == "POST":
            state["posted"] = True
            return html(allocation_saved_html(MAC, "192.168.1.99"))
        return html(allocation_saved_html(MAC, "192.168.1.99") if state["posted"] else _selected_entry_page())

    code, out, posts = _run(monkeypatch, capsys, handler, ["submit", "ipalloc", "Save"])
    assert len(posts) == 1
    assert code == 0 and out["committed"] is True and out["outcome"] == "applied"
    assert "verified" not in out and "verifyAttempts" not in out


def test_an_acknowledged_submit_whose_re_read_is_unreadable_exits_two(tmp_env, clock, monkeypatch, capsys):
    requested = [f"alloc_{MAC}={IP}"]
    state = {"posted": False}

    def handler(request, _n):
        if request.method == "POST":
            state["posted"] = True
            return html(allocation_saved_html(MAC, "192.168.1.99"))
        return html("unavailable", 503) if state["posted"] else html(_selected_entry_page())

    monkeypatch.setattr(cli, "sleep", lambda s: None)
    code, out, posts = _run(monkeypatch, capsys, handler, ["submit", "ipalloc", "Save", *requested])
    assert len(posts) == 1
    assert code == 2 and out["committed"] is True and out["outcome"] == "applied"
    assert out.get("verified") is None and out["warning"] and out["verifyAttempts"] == 3
