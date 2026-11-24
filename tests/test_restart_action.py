"""`action restart` behaves like the gateway's own Restart button (fw 6.35.8, captured 2026-11-23).

The live restart.ha form renders `<input type="submit" name="Restart" value="Restart">`; posting any
other value is answered 302 -> /cgi-bin/restarting.ha and the gateway does not reboot (observed:
"Time Since Last Reboot" kept counting). A browser also loads the restarting page the POST redirects
to, so the action requests it once, and a dropped connection there is the reboot starting.
"""

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from save_helpers import client_with, html

from bgwcli import cli
from bgwcli.actions import get_action

# Trimmed from the live page: the form, its nonce and both submit buttons, markup as served.
RESTART_FORM = (
    '<form method="post" action="/cgi-bin/restart.ha">'
    '<input type="hidden" name="nonce" value="abc123" />'
    '<p class="col1"><br/>Restarting your (AT&amp;T BGW320-500) device will temporarily prevent access '
    "to the Internet and any associated services (e.g., VoIP)</p>"
    '<input type="submit" name="Restart" class="cssbtn smallbtn btnspacer" value="Restart" />'
    '<input type="submit" name="Cancel" class="cssbtn smallbtn btnspacer" value="Cancel" />'
    "</form>"
)
RESET_FORM = (
    '<form method="post" action="/cgi-bin/reset.ha"><input type="hidden" name="nonce" value="abc123" />'
    '<input type="submit" name="ResetIP" class="cssbtn" value="Reset IP"/>'
    '<input type="submit" name="Restart" class="cssbtn" value="Restart"/>'
    '<input type="submit" name="Reset" class="cssbtn" value="Reset Device..."/></form>'
)
PAGES = {"restart": RESTART_FORM, "restart-from-resets": RESET_FORM}


def _run(monkeypatch, capsys, name, after_post, location="/cgi-bin/restarting.ha"):
    action = get_action(name)
    state = {"posted": False}

    def handler(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html("", 302, {"location": location})
        if state["posted"]:
            return after_post()
        return html(PAGES[name])

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["action", name, "--commit", "--confirm", action.confirm_token, "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and "/login.ha" not in r.url]
    after = wire.requests[wire.requests.index(posts[-1]) + 1 :] if posts else []
    gets_after_post = [urlsplit(r.url).path for r in after if r.method == "GET"]
    return code, out, posts, gets_after_post


def _restarting_page():
    return html("<title>Restarting</title>")


def test_restart_posts_the_button_value_the_live_restart_form_renders(tmp_env, clock, monkeypatch, capsys):
    code, out, posts, _ = _run(monkeypatch, capsys, "restart", _restarting_page)

    assert code == 0 and len(posts) == 1
    sent = parse_qs(posts[0].body.decode())
    assert sent["Restart"] == ["Restart"]
    assert sent["nonce"] == ["abc123"]
    assert "Cancel" not in sent


@pytest.mark.parametrize("name", ["restart", "restart-from-resets"])
def test_restart_requests_the_restarting_page_the_gateway_redirects_to(tmp_env, clock, monkeypatch, capsys, name):
    code, out, posts, gets_after_post = _run(monkeypatch, capsys, name, _restarting_page)

    assert code == 0 and len(posts) == 1 and out["committed"] is True
    assert gets_after_post == ["/cgi-bin/restarting.ha"]


@pytest.mark.parametrize("name", ["restart", "restart-from-resets"])
def test_a_dropped_connection_on_the_restarting_page_is_the_restart_starting(
    tmp_env, clock, monkeypatch, capsys, name
):
    def dropped():
        raise ConnectionResetError("gateway going down")

    code, out, posts, gets_after_post = _run(monkeypatch, capsys, name, dropped)

    assert code == 0 and len(posts) == 1 and out["committed"] is True
    assert gets_after_post[:1] == ["/cgi-bin/restarting.ha"]
    assert "restarting.ha" in out["result"]


def test_restart_never_follows_a_redirect_that_leaves_the_gateway(tmp_env, clock, monkeypatch, capsys):
    code, out, posts, gets_after_post = _run(
        monkeypatch, capsys, "restart", _restarting_page, location="http://example.com/cgi-bin/restarting.ha"
    )

    assert code == 0 and len(posts) == 1
    assert gets_after_post == []


def test_restart_without_a_redirect_requests_nothing_more(tmp_env, clock, monkeypatch, capsys):
    code, out, posts, gets_after_post = _run(monkeypatch, capsys, "restart", _restarting_page, location="")

    assert len(posts) == 1
    assert gets_after_post == []
