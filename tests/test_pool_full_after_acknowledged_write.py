"""A session pool that fills while the post-save verification re-reads must not erase what the
write already proved: it was sent, answered and acknowledged."""

from __future__ import annotations

import json

from save_helpers import SAVED_RED, client_with, form, html

from bgwcli import cli, session
from bgwcli.client import session_pool_full_error


def test_pool_full_during_verification_keeps_the_acknowledged_write_evidence(tmp_env, clock, monkeypatch, capsys):
    state = {"posted": False}

    def handler(request, _n):
        if request.method == "POST":
            state["posted"] = True
            return html(form("dosprotect", "new", banner=SAVED_RED))
        if state["posted"]:
            raise session_pool_full_error(waited_ms=2300, retry_count=4)
        return html(form("dosprotect", "old"))

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    code = cli.main(["set", "dosprotect", "setting=new", "--commit", "--confirm", "DOSPROTECT",
                     "--host", "http://router.local", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert sum(r.method == "POST" for r in wire.requests) == 1
    assert code == 2 and out["sessionPoolFull"] is True and out["waitedMs"] == 2300
    assert out["writeAttempted"] is True and out["acknowledgementObserved"] is True
    assert out["committed"] is True and out["writePerformed"] is True
    assert session.read_session_state(client.session_identity()).pool_cooldown_until is not None
