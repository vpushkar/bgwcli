"""The gateway's "No changes detected" answer is page-independent: the live re-read decides."""

import json

import pytest
from save_helpers import NO_CHANGE, client_with, form, html

from bgwcli import cli

NO_CHANGE_WITHOUT_ICON = '<div id="error-message-text">No changes detected. Save not performed.</div>'


@pytest.mark.parametrize("page", ["dosprotect", "etherlan", "wconfig", "dhcpserver"])
@pytest.mark.parametrize("banner", [NO_CHANGE, NO_CHANGE_WITHOUT_ICON], ids=["icon", "no-icon"])
@pytest.mark.parametrize("live", ["same", "different"])
def test_no_changes_on_any_form_page_is_decided_by_the_reread(
    tmp_env, clock, monkeypatch, capsys, page, banner, live
):
    state = {"posted": False}
    wanted = "2" if live == "same" else "1"

    def handler(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html("", 302, {"location": f"/cgi-bin/{page}.ha"})
        return html(form(page, wanted if state["posted"] else "1", banner=banner if state["posted"] else ""))

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["set", page, "setting=2", "--commit", "--confirm", page.upper(), "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    assert len(posts) == 1
    assert clock.sleeps == []
    assert out["committed"] is False and out["writePerformed"] is False
    if live == "same":
        assert code == 0 and out["outcome"] == "unchanged" and out["verified"] is True
    else:
        assert code == 1 and out["outcome"] == "failed" and out["verified"] is False
