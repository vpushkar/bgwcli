"""A generic set/submit whose Save was acknowledged but whose known state is not visible is a
committed write decided by the live re-read, like the LAN path."""

from __future__ import annotations

import json

from integration_html import allocation_saved_html, entry_page_html
from save_helpers import client_with, html

from bgwcli import cli

MAC = "02:0a:0b:0c:0d:02"


def _with_hex_nonce(page: str) -> str:
    """The gateway's nonces are hex; the shared fixture pages carry a placeholder the client cannot use."""
    return page.replace('name="nonce" value="n"', 'name="nonce" value="ab12"')


def _run(monkeypatch, capsys, argv):
    state = {"posted": False}

    def handler(request, _n):
        if request.method == "POST":
            state["posted"] = True
            return html(allocation_saved_html(MAC, "192.168.1.99"))
        if state["posted"]:
            # After a save the gateway renders the IP Allocation table, not the Entry form: the
            # re-read shows the device's Fixed Allocation row (with another address), never the
            # `alloc_<mac>` select the write was built from. A re-read that still showed the Entry
            # form without the table would be an unreadable verification page (exit 2), not a mismatch.
            return html(allocation_saved_html(MAC, "192.168.1.99"))
        return html(_with_hex_nonce(entry_page_html(MAC, ["192.168.1.64"])))

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--commit", "--confirm", "IPALLOC", "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    return code, out, posts


def test_set_acknowledged_with_another_allocation_is_committed_and_reread(tmp_env, clock, monkeypatch, capsys):
    code, out, posts = _run(monkeypatch, capsys, ["set", "ipalloc", f"alloc_{MAC}=192.168.1.64"])
    assert len(posts) == 1
    assert out["committed"] is True and out["outcome"] == "applied"
    assert out["acknowledgementObserved"] is True and out["writePerformed"] is True
    assert out["verified"] is False
    assert code == 1


def test_submit_acknowledged_with_another_allocation_is_committed(tmp_env, clock, monkeypatch, capsys):
    code, out, posts = _run(
        monkeypatch, capsys, ["submit", "ipalloc", "Save", f"alloc_{MAC}=192.168.1.64"]
    )
    assert len(posts) == 1
    assert out["committed"] is True and out["acknowledgementObserved"] is True
    assert out["verified"] is False and out["mismatches"]
    assert code == 1


def test_submit_acknowledged_with_unreadable_reread_is_committed_exit_two(tmp_env, clock, monkeypatch, capsys):
    state = {"posted": False}

    def handler(request, _n):
        if request.method == "POST":
            state["posted"] = True
            return html(allocation_saved_html(MAC, "192.168.1.99"))
        if state["posted"]:
            return html("unavailable", 503)
        return html(_with_hex_nonce(entry_page_html(MAC, ["192.168.1.64"])))

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["submit", "ipalloc", "Save", f"alloc_{MAC}=192.168.1.64",
                     "--commit", "--confirm", "IPALLOC", "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    assert len(posts) == 1
    assert out["committed"] is True and out.get("verified") is None and out["warning"]
    assert code == 2
