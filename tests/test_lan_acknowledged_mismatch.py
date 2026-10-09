"""A LAN Save that is acknowledged but reads back differently is the set mismatch row: exit 1,
committed, verified false - not an unconfirmed (exit 2, uncommitted) save."""

from __future__ import annotations

import json

from save_helpers import SAVED_RED, client_with, form, html

from bgwcli import cli


def test_lan_acknowledged_mismatch_is_committed_unverified_exit_one(tmp_env, clock, monkeypatch, capsys):
    state = {"posted": False}

    def handler(request, _n):
        if request.method == "POST":
            state["posted"] = True
            return html("", 302, {"location": "/cgi-bin/dhcpserver.ha"})
        return html(form("dhcpserver", "on", name="dhcp", banner=SAVED_RED if state["posted"] else ""))

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    code = cli.main(["set", "dhcpserver", "dhcp=off", "--commit", "--confirm", "DHCPSERVER",
                     "--host", "http://router.local", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert sum(r.method == "POST" for r in wire.requests) == 1
    assert code == 1 and out["committed"] is True and out["verified"] is False
    assert out["writePerformed"] is True and out["acknowledgementObserved"] is True
    assert out["mismatches"]
