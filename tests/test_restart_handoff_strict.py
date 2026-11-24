"""The restart hand-off is recognised only as the gateway's own restarting page.

Anything else after the restart POST (another /cgi-bin/ page, a redirect, a 4xx, a malformed
Location, a full session pool) leaves the restart unconfirmed: exit 2 with the POST's write
evidence, never "Restart requested". A 5xx or dropped connection on restarting.ha is the gateway
going down (exit 0, reboot not verified)."""

import json
import time
from urllib.parse import urlsplit

import pytest
from save_helpers import client_with, html

from bgwcli import cli
from bgwcli.actions import get_action
from bgwcli.client import _scan_forms, _select_write_nonces

RESTART_FORM = (
    '<form method="post" action="/cgi-bin/restart.ha"><input type="hidden" name="nonce" value="abc123" />'
    '<input type="submit" name="Restart" value="Restart" /><input type="submit" name="Cancel" value="Cancel" /></form>'
)
RESET_FORM = (
    '<form method="post" action="/cgi-bin/reset.ha"><input type="hidden" name="nonce" value="abc123" />'
    '<input type="submit" name="Restart" value="Restart"/></form>'
)
POOL_FULL = "<title>Login</title><p>All web server sessions are in use</p>"


def _run(monkeypatch, capsys, name, location, after_post):
    state = {"posted": False}
    source = RESTART_FORM if name == "restart" else RESET_FORM

    def handler(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html("", 302, {"location": location})
        if state["posted"]:
            return after_post(request)
        return html(source)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["action", name, "--commit", "--confirm", get_action(name).confirm_token, "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and "/login.ha" not in r.url]
    first = wire.requests.index(posts[0])
    gets_after = [urlsplit(r.url).path for r in wire.requests[first + 1 :] if r.method == "GET"]
    return code, out, posts, gets_after


def _unconfirmed(code, out, posts):
    assert code == 2 and len(posts) == 1
    assert out["committed"] is False and out["writeAttempted"] is True and out["statusCode"] == 302


@pytest.mark.parametrize("name", ["restart", "restart-from-resets"])
@pytest.mark.parametrize(
    "location", ["/cgi-bin/restart.ha", "/cgi-bin/wifiwarn_advanced.ha", "/cgi-bin/home.ha?x=1", "", "http://[::1/cgi-bin/x"]
)
def test_a_redirect_that_is_not_the_restarting_page_is_never_requested_and_leaves_the_restart_unconfirmed(
    tmp_env, clock, monkeypatch, capsys, name, location
):
    code, out, posts, gets_after = _run(monkeypatch, capsys, name, location, lambda r: html("<title>x</title>"))

    assert gets_after == []
    _unconfirmed(code, out, posts)


@pytest.mark.parametrize(
    "answer",
    [
        lambda r: html("<title>Not Found</title>", 404),
        lambda r: html("", 302, {"location": "/cgi-bin/login.ha"}),
        lambda r: html("", 302, {"location": "/cgi-bin/home.ha"}),
    ],
    ids=["404", "redirect-to-login", "redirect-elsewhere"],
)
def test_a_restarting_page_answer_that_is_not_the_gateway_going_down_is_unconfirmed_with_evidence(
    tmp_env, clock, monkeypatch, capsys, answer
):
    code, out, posts, gets_after = _run(monkeypatch, capsys, "restart", "/cgi-bin/restarting.ha", answer)

    assert gets_after == ["/cgi-bin/restarting.ha"]
    _unconfirmed(code, out, posts)


@pytest.mark.parametrize(
    "answer",
    [lambda r: html("<title>Restarting</title>"), lambda r: html("", 503)],
    ids=["200", "503"],
)
def test_the_restarting_page_answered_or_going_down_is_a_requested_unverified_restart(
    tmp_env, clock, monkeypatch, capsys, answer
):
    code, out, posts, gets_after = _run(monkeypatch, capsys, "restart", "/cgi-bin/restarting.ha?boot=1", answer)

    assert code == 0 and out["committed"] is True and gets_after == ["/cgi-bin/restarting.ha"]
    assert "not verified" in out["result"]


# --- form scanner -----------------------------------------------------------------------------


def test_select_and_textarea_controls_belong_to_their_own_form():
    page = (
        '<form action="/cgi-bin/a.ha"><input name="nonce" value="aa"><select name="mode"><option>x</option>'
        '</select><textarea name="t"></textarea><input type="submit" name="Save" value="Save"></form>'
        '<form action="/cgi-bin/b.ha"><input name="nonce" value="bb"><input type="hidden" name="mode" value="y"></form>'
    )
    assert _select_write_nonces(page, "a", "a.ha", {"mode": "x", "t": "z"}) == ["aa"]


@pytest.mark.parametrize("empty", ["<!-->", "<!--->"])
def test_an_empty_comment_does_not_hide_the_forms_after_it(empty):
    page = f'{empty}<form action="/cgi-bin/a.ha"><input name="nonce" value="aa"></form><!-- x -->'
    assert [form[0] for form in _scan_forms(page)] == ["/cgi-bin/a.ha"]


def test_the_page_nonce_fallback_ignores_commented_out_nonces():
    page = '<!-- <input name="nonce" value="aa11"> --><input name="nonce" value="bb22">'
    assert _select_write_nonces(page, "p", "p.ha", {"Go": "Go"}) == ["bb22"]


@pytest.mark.parametrize("hostile", ["<script " * 40000, "<!--" * 40000, "<style x" * 40000])
def test_markup_stripping_stays_linear_on_hostile_input(hostile):
    started = time.monotonic()
    _scan_forms(hostile + '<form action="/cgi-bin/a.ha"></form>')
    assert time.monotonic() - started < 1.0
