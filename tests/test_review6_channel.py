"""Sixth review round, channel scan and restore protocol:

- `find-best-channel-5` drops the 5 GHz radio (and the client's, on 5 GHz): once its Wi-Fi Warning
  Continue is accepted, nothing more is read; the action reports a requested scan whose answer page
  was not read (exit 0, committed, answerRead false), whether or not the radio is already gone;
- the restore client protocol carries the redirect's query on page reads (`get_cgi_page(page,
  query=...)`), so a Save redirected to `<page>.ha?view=1` is acknowledged on that exact URL.
"""

import json
from urllib.parse import urlsplit

import pytest
from save_helpers import SAVED_RED, client_with, html
from test_integration_restore import CHANGES_SAVED_HTML, FakeRouter, Get, Post

from bgwcli import cli
from bgwcli.restore import RestoreStep, execute_restore

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
SCAN = ["action", "find-best-channel-5", "--commit", "--confirm", "CHANSCAN", "--json"]


def _scan(monkeypatch, capsys, *, radio_gone_after_continue: bool):
    state = {"continued": False}

    def handler(request, n):
        path = urlsplit(request.url).path
        if request.method == "POST":
            if "Continue" in request.body.decode():
                state["continued"] = True
                return html("", 302, {"location": "/cgi-bin/wconfig.ha"})
            return html("", 302, {"location": "/cgi-bin/wifiwarn_advanced.ha"})
        if state["continued"] and radio_gone_after_continue:
            raise ConnectionResetError("the 5 GHz radio is re-tuning")
        if "wifiwarn" in path:
            return html(WARNING_PAGE)
        return html((SAVED_RED if state["continued"] else "") + SCAN_FORM)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(SCAN)
    captured = capsys.readouterr().out
    out = json.loads(captured) if captured.strip() else {}
    posts = [r for r in wire.requests if r.method == "POST" and "/login.ha" not in r.url]
    after_continue = wire.requests[wire.requests.index(posts[-1]) + 1 :] if posts else []
    return code, out, posts, after_continue


@pytest.mark.parametrize("radio_gone", [True, False], ids=["radio-drops", "page-readable"])
def test_the_channel_scan_stops_after_its_accepted_continue(monkeypatch, capsys, tmp_env, clock, radio_gone):
    code, out, posts, after_continue = _scan(monkeypatch, capsys, radio_gone_after_continue=radio_gone)
    assert [urlsplit(p.url).path for p in posts] == ["/cgi-bin/wconfig.ha", "/cgi-bin/wconfig.ha"]
    assert "Continue" in posts[1].body.decode()
    assert after_continue == [], "nothing is read once the radio-dropping Continue is accepted"
    assert code == 0, out
    assert out["committed"] is True and out["answerRead"] is False
    assert out["writeAttempted"] is True and out["writeResponseReceived"] is True
    assert out.get("warning") is None


# --- RestorePoster: page reads carry the redirect's query ------------------------------------------------


def test_a_save_redirected_with_a_query_is_acknowledged_on_that_url():
    # The repo's conforming restore double: page reads carry the redirect's query, as BGW320Client does.
    router = FakeRouter(
        lambda page: Get(200, CHANGES_SAVED_HTML),
        answer=lambda page, fields: Post(302, {"location": f"/cgi-bin/{page}.ha?view=1"}),
    )
    step = RestoreStep(order=1, kind="form", page="etherlan", description="save etherlan", button="Save",
                       raw_payload={"x": "1", "Save": "Save"})
    execution = execute_restore(router, [step])
    assert [page for page, _ in router.posted] == ["etherlan"]
    assert router.gets == ["etherlan?view=1"]
    assert [s.status for s in execution.steps] == ["applied"], execution.steps
