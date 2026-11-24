"""Fifth review round, restart and login checks:

- a deadline cut on the restart hand-off is a timeout (exit 2, "restart may be in progress, check
  uptime") even when the cut surfaced as a reset;
- a 5xx or a read timeout on the restart POST itself carries the same guidance;
- the hand-off counts as served only for a 2xx answer with a page (204/205, an empty body or a 1xx
  are unconfirmed);
- a full session pool on the hand-off reports the POST's attempts and status in JSON;
- a login redirect is recognised by the resolved same-origin path, never by a query mentioning it.
"""

import http.client
import json
from urllib.parse import urlsplit

import pytest
from save_helpers import client_with, html

from bgwcli import cli
from bgwcli.client import HttpResponse, ResponseReadTimeout, _is_login_url, _redirects_to_login

RESTART_FORM = (
    '<form method="post" action="/cgi-bin/restart.ha"><input type="hidden" name="nonce" value="abc123" />'
    '<input type="submit" name="Restart" value="Restart" /><input type="submit" name="Cancel" value="Cancel" /></form>'
)
RESTART = ["action", "restart", "--commit", "--confirm", "RESTART"]
UNCERTAIN = "restart may be in progress, check uptime"


def _cli(monkeypatch, capsys, handler):
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*RESTART, "--json"])
    captured = capsys.readouterr()
    out = json.loads(captured.out) if captured.out.strip() else {}
    posts = [r for r in wire.requests if r.method == "POST" and "/login.ha" not in r.url]
    first = wire.requests.index(posts[0]) if posts else len(wire.requests)
    gets_after = [urlsplit(r.url).path for r in wire.requests[first + 1 :] if r.method == "GET"]
    return code, out, posts, gets_after, client


def _handler(post_answer, after=lambda r: html("<title>Restarting</title>")):
    state = {"posted": False}

    def handler(request, n):
        if request.method == "POST":
            state["posted"] = True
            return post_answer(request)
        return after(request) if state["posted"] else html(RESTART_FORM)

    return handler


def _redirect(location="/cgi-bin/restarting.ha", status=302):
    return lambda r: html("", status, {"location": location})


def _raise(make):
    def answer(request):
        raise make()

    return answer


def _deadline_cut_reset():
    # What urllib_transport raises when its deadline watchdog shuts the socket down while the
    # status line is awaited: the read fails as a reset, wrapped as a response-read timeout.
    try:
        raise http.client.RemoteDisconnected("Remote end closed connection without response")
    except http.client.RemoteDisconnected as reset:
        timeout = ResponseReadTimeout("response exceeded the 1000 ms deadline")
        timeout.__cause__ = reset
        return timeout


# --- 1a: timeout beats reset; the POST's own 5xx/timeout ------------------------------------------------


def test_a_deadline_cut_that_surfaced_as_a_reset_is_a_timeout(tmp_env, clock, monkeypatch, capsys):
    code, out, posts, _, _ = _cli(monkeypatch, capsys, _handler(_redirect(), _raise(_deadline_cut_reset)))
    assert code == 2 and len(posts) == 1 and out["committed"] is False
    assert UNCERTAIN in out["warning"]


@pytest.mark.parametrize(
    "post_answer",
    [
        lambda r: html("<title>Error</title>", 500),
        lambda r: html("", 503),
        _raise(lambda: ResponseReadTimeout("timed out waiting for response headers")),
        _raise(_deadline_cut_reset),
    ],
    ids=["post-500", "post-503", "post-read-timeout", "post-deadline-cut"],
)
def test_an_ambiguous_restart_post_says_the_restart_may_be_in_progress(
    tmp_env, clock, monkeypatch, capsys, post_answer
):
    code, out, posts, gets_after, _ = _cli(monkeypatch, capsys, _handler(post_answer))
    assert code == 2 and len(posts) == 1 and gets_after == []
    assert out["committed"] is False and out["writeAttempted"] is True
    assert UNCERTAIN in out["warning"]


@pytest.mark.parametrize(
    "post_answer",
    [lambda r: html("<title>Not Found</title>", 404), _raise(lambda: ConnectionRefusedError("refused"))],
    ids=["post-404", "post-refused"],
)
def test_a_clear_restart_post_failure_is_not_called_in_progress(tmp_env, clock, monkeypatch, capsys, post_answer):
    code, out, _, _, _ = _cli(monkeypatch, capsys, _handler(post_answer))
    assert code == 2 and out["committed"] is False
    assert UNCERTAIN not in out["warning"]


def test_the_restart_post_timeout_is_recognised_by_type_not_wording(tmp_env, clock, monkeypatch, capsys):
    # An error that only reads like a timeout (no ResponseReadTimeout behind it) is not one...
    lookalike = _raise(lambda: cli.RouterConnectionError("Timed out reading response from somewhere"))
    code, out, _, _, _ = _cli(monkeypatch, capsys, _handler(lookalike))
    assert code == 2 and UNCERTAIN not in out["warning"]

    # ...and a real one is, whatever its message says.
    reworded = _raise(lambda: ResponseReadTimeout("the gateway went quiet"))
    code, out, _, _, _ = _cli(monkeypatch, capsys, _handler(reworded))
    assert code == 2 and UNCERTAIN in out["warning"]


def test_a_timeout_on_the_relogin_after_an_answered_post_is_not_called_in_progress(
    tmp_env, clock, monkeypatch, capsys
):
    # The POST is answered with the Login page (the gateway did not act on it); the re-login then
    # times out. The restart POST itself was answered, so this is a plain failure, not "in progress".
    login_page = (
        '<html><head><title>Login</title></head><body><form method="post" action="/cgi-bin/login.ha">'
        '<input type="hidden" name="nonce" value="abc123" /><input type="password" name="password" />'
        '<input type="submit" name="Continue" value="Continue" /></form></body></html>'
    )
    state = {"posted": False}

    def handler(request, n):
        if request.method == "POST" and "/login.ha" not in request.url:
            state["posted"] = True
            return html(login_page)
        if state["posted"] and "/login.ha" in request.url:
            raise ResponseReadTimeout("timed out waiting for response headers")
        return html(RESTART_FORM)

    code, out, posts, _, _ = _cli(monkeypatch, capsys, handler)
    assert len(posts) == 1 and code == 2
    assert UNCERTAIN not in json.dumps(out)


# --- 1b: the hand-off is served only as a 2xx page ------------------------------------------------------


@pytest.mark.parametrize(
    "after",
    [
        lambda r: html("", 204),
        lambda r: html("", 205),
        lambda r: html(""),
        lambda r: html("   \n"),
        lambda r: html("<title>Restarting</title>", 103),
    ],
    ids=["204", "205", "200-empty", "200-blank", "103"],
)
def test_a_hand_off_without_a_served_page_is_unconfirmed(tmp_env, clock, monkeypatch, capsys, after):
    code, out, posts, gets_after, _ = _cli(monkeypatch, capsys, _handler(_redirect(), after))
    assert code == 2 and len(posts) == 1 and gets_after == ["/cgi-bin/restarting.ha"]
    assert out["committed"] is False and out["writeAttempted"] is True


def test_a_served_restarting_page_is_still_a_requested_restart(tmp_env, clock, monkeypatch, capsys):
    code, out, _, _, _ = _cli(monkeypatch, capsys, _handler(_redirect()))
    assert code == 0 and out["committed"] is True


# --- 1f: pool-full JSON carries the POST's attempts and status -------------------------------------------


def test_pool_full_on_the_hand_off_reports_attempts_and_status_in_json(tmp_env, clock, monkeypatch, capsys):
    handler = _handler(
        _redirect(status=303), lambda r: html("<title>Login</title><p>All web server sessions are in use</p>")
    )
    code, out, _, _, _ = _cli(monkeypatch, capsys, handler)
    assert code == 2 and out["sessionPoolFull"] is True
    assert out["writeAttempted"] is True and out["writeResponseReceived"] is True
    assert out["writeAttempts"] == 1 and out["statusCode"] == 303


# --- 2f: login redirects by resolved same-origin path ----------------------------------------------------


def _answer(location: str, status: int = 302) -> HttpResponse:
    return HttpResponse(
        status_code=status, status_message="", headers={"location": location}, body="",
        url="http://router.local/cgi-bin/restart.ha",
    )


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("/cgi-bin/login.ha", True),
        ("login.ha", True),
        ("/CGI-BIN/LOGIN.HA?x=1", True),
        ("http://router.local:80/cgi-bin/login.ha", True),
        ("/cgi-bin/restarting.ha?next=/cgi-bin/login.ha", False),
        ("/cgi-bin/home.ha#/cgi-bin/login.ha", False),
        ("http://other.example/cgi-bin/login.ha", False),
        ("http://router.local:bad/cgi-bin/login.ha", False),
        ("/cgi-bin/login.hax", False),
    ],
)
def test_a_post_redirect_is_a_login_redirect_only_by_its_resolved_path(location, expected):
    assert _redirects_to_login(_answer(location)) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://router.local/cgi-bin/login.ha", True),
        ("http://router.local/cgi-bin/login.ha?next=x", True),
        ("http://router.local/cgi-bin/home.ha?next=/cgi-bin/login.ha", False),
        ("http://router.local/cgi-bin/restarting.ha#/cgi-bin/login.ha", False),
    ],
)
def test_a_final_url_is_the_login_page_only_by_its_path(url, expected):
    assert _is_login_url(url) is expected


def test_a_restart_redirect_whose_query_names_login_keeps_the_session_and_hands_off(
    tmp_env, clock, monkeypatch, capsys
):
    handler = _handler(_redirect("/cgi-bin/restarting.ha?next=/cgi-bin/login.ha"))
    code, out, posts, gets_after, client = _cli(monkeypatch, capsys, handler)
    assert code == 0 and out["committed"] is True and len(posts) == 1
    assert gets_after == ["/cgi-bin/restarting.ha"]
    assert client.has_authenticated_session() is True


def test_a_restart_redirected_to_the_login_page_still_loses_the_session(tmp_env, clock, monkeypatch, capsys):
    code, out, _, gets_after, client = _cli(monkeypatch, capsys, _handler(_redirect("/cgi-bin/login.ha?x=1")))
    assert code != 0 and gets_after == []
    assert client.has_authenticated_session() is False
