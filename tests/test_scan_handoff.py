"""find-best-channel-5 loads the gateway's wrestart.ha hand-off, as a browser does.

Live on fw 6.35.8 (2026-11-24): the chanscan5 POST answers 302 -> /cgi-bin/wrestart.ha?22 and the
scan only ran when that page was then requested (it answered 302 -> /cgi-bin/wconfig.ha after about
20 s). Three POST-only runs left the 5 GHz channel unchanged; after POST + GET it moved 36 -> 64.
"""

import json
from urllib.parse import urlsplit

import pytest
from save_helpers import client_with, html

from bgwcli import cli
from bgwcli.client import SLOW_PAGE_TIMEOUT_MS, ResponseReadTimeout

SCAN_FORM = (
    '<form method="post" action="/cgi-bin/wconfig.ha"><input type="hidden" name="nonce" value="abc123">'
    '<input type="text" name="power_5" value="100">'
    '<input type="submit" name="chanscan5" value="Find Best Channel"></form>'
)
SCAN = ["action", "find-best-channel-5", "--commit", "--confirm", "CHANSCAN", "--json"]
UNCERTAIN = "scan may be in progress"


def _redirect(location, status=302):
    return lambda request: html("", status, {"location": location})


def _raise(make):
    def answer(request):
        raise make()

    return answer


BACK_TO_WCONFIG = _redirect("/cgi-bin/wconfig.ha")


def _scan(monkeypatch, capsys, post_answer, handoff_answer=BACK_TO_WCONFIG):
    state = {"posted": False}

    def handler(request, n):
        path = urlsplit(request.url).path
        if request.method == "POST":
            state["posted"] = True
            return post_answer(request)
        if state["posted"] and path == "/cgi-bin/wrestart.ha":
            return handoff_answer(request)
        return html(SCAN_FORM)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(SCAN)
    captured = capsys.readouterr().out
    out = json.loads(captured) if captured.strip() else {}
    posts = [r for r in wire.requests if r.method == "POST" and "/login.ha" not in r.url]
    after = wire.requests[wire.requests.index(posts[-1]) + 1 :] if posts else []
    gets_after = [urlsplit(r.url).path + (f"?{urlsplit(r.url).query}" if urlsplit(r.url).query else "")
                  for r in after if r.method == "GET"]
    return code, out, posts, gets_after


def test_the_scan_loads_the_wrestart_hand_off_like_a_browser(monkeypatch, capsys, tmp_env, clock):
    code, out, posts, gets_after = _scan(monkeypatch, capsys, _redirect("/cgi-bin/wrestart.ha?22"))
    assert len(posts) == 1 and "chanscan5" in posts[0].body.decode()
    assert gets_after == ["/cgi-bin/wrestart.ha?22"], "exactly the hand-off, query kept, nothing else"
    assert code == 0, out
    assert out["committed"] is True and out["answerRead"] is True
    assert "scan" in out["result"].lower()


@pytest.mark.parametrize(
    "handoff",
    [
        _raise(lambda: ResponseReadTimeout("timed out waiting for response headers")),
        _raise(lambda: ConnectionResetError("the 5 GHz radio is re-tuning")),
    ],
    ids=["timeout", "reset"],
)
def test_a_hand_off_without_an_answer_may_still_be_scanning(monkeypatch, capsys, tmp_env, clock, handoff):
    code, out, posts, gets_after = _scan(monkeypatch, capsys, _redirect("/cgi-bin/wrestart.ha?22"), handoff)
    assert len(posts) == 1 and gets_after == ["/cgi-bin/wrestart.ha?22"]
    assert code == 2, out
    assert out["committed"] is False and UNCERTAIN in out["warning"]


@pytest.mark.parametrize(
    "handoff",
    [
        lambda r: html("<title>Wi-Fi</title>" + SCAN_FORM),  # 200 instead of the redirect back
        _redirect("/cgi-bin/login.ha"),
        lambda r: html("", 500),
    ],
    ids=["200-page", "login-redirect", "500"],
)
def test_a_hand_off_that_does_not_return_to_wconfig_is_unconfirmed(monkeypatch, capsys, tmp_env, clock, handoff):
    code, out, posts, gets_after = _scan(monkeypatch, capsys, _redirect("/cgi-bin/wrestart.ha?22"), handoff)
    assert len(posts) == 1 and gets_after == ["/cgi-bin/wrestart.ha?22"]
    assert code == 2, out
    assert out["committed"] is False


def test_a_hand_off_on_another_origin_is_never_requested(monkeypatch, capsys, tmp_env, clock):
    elsewhere = _redirect("https://elsewhere.example/cgi-bin/wrestart.ha?22")
    code, out, posts, gets_after = _scan(monkeypatch, capsys, elsewhere)
    assert len(posts) == 1 and gets_after == []
    assert code == 2, out
    assert out["committed"] is False


def test_the_wrestart_hand_off_gets_the_slow_page_timeout_floor():
    # The hand-off took 20-21 s live on every run: the 15 s default would cut a working scan.
    assert SLOW_PAGE_TIMEOUT_MS["wrestart"] == 45000
