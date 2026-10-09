"""set ipalloc alloc_<mac>=<ip> is verified by the reservation state the save wait already observed:
after the save the gateway renders a Fixed Allocation row, so the editor select is gone from the
re-read and comparing the requested field against it would read <absent>."""

from __future__ import annotations

import json

import pytest
from integration_html import allocation_saved_html, entry_page_html
from save_helpers import client_with, html

from bgwcli import cli

MAC = "02:0a:0b:0c:0d:02"
IP = "192.168.1.64"


def _run(monkeypatch, capsys, argv, *, saved_ip):
    state = {"posted": False}

    def handler(request, _n):
        if request.method == "POST":
            state["posted"] = True
            return html(allocation_saved_html(MAC, saved_ip))
        if state["posted"]:
            return html(allocation_saved_html(MAC, saved_ip))
        return html(entry_page_html(MAC, [IP]).replace('name="nonce" value="n"', 'name="nonce" value="ab12"'))

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--commit", "--confirm", "IPALLOC", "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    return code, out, posts


def test_set_after_a_state_verified_reservation_is_verified_and_exits_zero(tmp_env, clock, monkeypatch, capsys):
    code, out, posts = _run(monkeypatch, capsys, ["set", "ipalloc", f"alloc_{MAC}={IP}"], saved_ip=IP)
    assert len(posts) == 1
    assert code == 0 and out["committed"] is True and out["outcome"] == "applied"
    assert out["verified"] is True and not out.get("mismatches")
    assert out["acknowledgementObserved"] is True and out["writePerformed"] is True


def test_set_whose_reservation_state_is_not_visible_stays_a_mismatch(tmp_env, clock, monkeypatch, capsys):
    code, out, posts = _run(monkeypatch, capsys, ["set", "ipalloc", f"alloc_{MAC}={IP}"], saved_ip="192.168.1.99")
    assert len(posts) == 1
    assert code == 1 and out["committed"] is True and out["verified"] is False and out["mismatches"]


@pytest.mark.parametrize("operation", [["submit", "ipalloc", "Save"]])
def test_submit_of_the_same_exchange_is_unchanged(tmp_env, clock, monkeypatch, capsys, operation):
    code, out, posts = _run(monkeypatch, capsys, [*operation, f"alloc_{MAC}={IP}"], saved_ip=IP)
    assert len(posts) == 1
    assert code == 0 and out["committed"] is True
