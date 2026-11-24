"""Review follow-ups for the restart / clear-device-list live-button changes.

- The restart confirmation never authorizes a second POST (the Wi-Fi Warning follow-up belongs to
  the Wi-Fi pages that raise it).
- The restarting-page hand-off requests exactly the URL it validated: resolved against the POST URL
  like a browser, dot segments removed, origins compared normalized, only after a GET redirect.
- A readable refusal on the hand-off page is reported as one, never as the reboot starting.
- The posted button value is the one the live page renders, whatever the catalog says.
"""

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from save_helpers import ERROR, client_with, html

from bgwcli import cli
from bgwcli.actions import get_action

RESTART_FORM = (
    '<form method="post" action="/cgi-bin/restart.ha"><input type="hidden" name="nonce" value="abc123" />'
    '<input type="submit" name="Restart" value="{value}" /><input type="submit" name="Cancel" value="Cancel" />'
    "</form>"
)
DEVICES_FORM = (
    '<form method="post" action="/cgi-bin/devices.ha"><input type="hidden" name="nonce" value="abc123" />'
    '<input type="submit" name="Clear" value="{value}" /></form>'
)
WARNING_PAGE = (
    '<form method="post" action="/cgi-bin/wconfig.ha"><input type="hidden" name="nonce" value="w1">'
    '<input type="submit" name="Continue" value="Continue"></form>'
    '<form method="post" action="/cgi-bin/wifiwarn_advanced.ha"><input type="hidden" name="nonce" value="w2">'
    '<input type="submit" name="Cancel" value="Cancel"></form>'
)
POOL_FULL = "<title>Login</title><p>All web server sessions are in use</p>"
LOGIN_PAGE = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'
SOURCE = {"restart": RESTART_FORM.format(value="Restart"), "clear-device-list": DEVICES_FORM.format(value="Clear")}


def _run(monkeypatch, capsys, name, *, source=None, post_status=302, location="/cgi-bin/restarting.ha",
         after_post=lambda request: html("<title>Restarting</title>")):
    state = {"posted": False}

    def handler(request, n):
        path = urlsplit(request.url).path
        if request.method == "POST":
            state["posted"] = True
            return html("", post_status, {"location": location} if location is not None else {})
        if "wifiwarn" in path:
            return html(WARNING_PAGE)
        if state["posted"]:
            return after_post(request)
        return html(source or SOURCE[name])

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["action", name, "--commit", "--confirm", get_action(name).confirm_token, "--json"])
    captured = capsys.readouterr()
    posts = [r for r in wire.requests if r.method == "POST" and "/login.ha" not in r.url]
    first_post = wire.requests.index(posts[0]) if posts else len(wire.requests)
    gets_after = [urlsplit(r.url) for r in wire.requests[first_post + 1 :] if r.method == "GET"]
    out = json.loads(captured.out) if captured.out.strip() else {}
    return code, out, posts, gets_after


# --- the confirmation authorizes one POST ------------------------------------------------------------


@pytest.mark.parametrize("name", ["restart", "clear-device-list"])
@pytest.mark.parametrize(
    "location", ["/cgi-bin/wifiwarn_advanced.ha", "https://external.example/cgi-bin/wifiwarn_advanced.ha"]
)
def test_a_wifi_warning_redirect_never_adds_a_second_post(tmp_env, clock, monkeypatch, capsys, name, location):
    code, out, posts, gets_after = _run(monkeypatch, capsys, name, location=location)

    assert len(posts) == 1
    assert all("wconfig" not in post.url for post in posts)
    # The Wi-Fi Warning flow (read the warning twice, post Continue) belongs to the Wi-Fi pages. restart
    # requests only the gateway's restarting page (never this one); clear reads its answer page once.
    expected_reads = 0 if name == "restart" else 1
    assert len([g for g in gets_after if "wifiwarn" in g.path]) == expected_reads


# --- the hand-off requests what it validated -----------------------------------------------------------


def test_hand_off_never_leaves_cgi_bin_through_dot_segments(tmp_env, clock, monkeypatch, capsys):
    _, _, posts, gets_after = _run(monkeypatch, capsys, "restart", location="http://router.local/cgi-bin/../outside")

    assert len(posts) == 1
    assert [g.path for g in gets_after] == []


def test_hand_off_resolves_a_relative_location_against_the_post_url(tmp_env, clock, monkeypatch, capsys):
    _, _, _, gets_after = _run(monkeypatch, capsys, "restart", location="restarting.ha?boot=1")

    assert [(g.path, g.query) for g in gets_after] == [("/cgi-bin/restarting.ha", "boot=1")]


def test_hand_off_compares_origins_normalized(tmp_env, clock, monkeypatch, capsys):
    _, _, _, gets_after = _run(
        monkeypatch, capsys, "restart", location="HTTP://ROUTER.LOCAL:80/cgi-bin/restarting.ha"
    )

    assert [g.path for g in gets_after] == ["/cgi-bin/restarting.ha"]


def test_hand_off_follows_only_a_get_redirect(tmp_env, clock, monkeypatch, capsys):
    _, _, _, gets_after = _run(monkeypatch, capsys, "restart", post_status=201)

    assert [g.path for g in gets_after if g.path == "/cgi-bin/restarting.ha"] == []


# --- a readable refusal is reported as one -------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        lambda request: html("<title>Forbidden</title>", 403),
        lambda request: html(LOGIN_PAGE),
    ],
    ids=["http-403", "login-page"],
)
def test_hand_off_refused_by_a_lost_session_is_no_answer(tmp_env, clock, monkeypatch, capsys, answer):
    code, out, posts, _ = _run(monkeypatch, capsys, "restart", after_post=answer)

    assert code == 2 and len(posts) == 1
    assert out["committed"] is False and out["writeAttempted"] is True


def test_hand_off_answered_by_a_rejection_banner_is_a_negative_answer(tmp_env, clock, monkeypatch, capsys):
    code, out, posts, _ = _run(
        monkeypatch, capsys, "restart", after_post=lambda request: html(f"<title>Restart</title>{ERROR}")
    )

    assert code == 1 and len(posts) == 1
    assert out["committed"] is False
    assert "A required setting is empty" in out["warning"]


def test_hand_off_answered_by_a_full_session_pool_is_not_a_reboot(tmp_env, clock, monkeypatch, capsys):
    code, out, posts, _ = _run(monkeypatch, capsys, "restart", after_post=lambda request: html(POOL_FULL))

    # Raised (the coordinator records the pool-full cooldown) with the restart POST's write evidence.
    assert code != 0 and len(posts) == 1
    assert out["sessionPoolFull"] is True and out["writeAttempted"] is True


@pytest.mark.parametrize(
    "answer",
    [
        lambda request: html("<title>Restarting</title>"),
        lambda request: html("<title>Unavailable</title>", 503),
    ],
    ids=["http-200", "http-503"],
)
def test_hand_off_answered_without_a_refusal_is_a_requested_unverified_restart(
    tmp_env, clock, monkeypatch, capsys, answer
):
    code, out, posts, _ = _run(monkeypatch, capsys, "restart", after_post=answer)

    assert code == 0 and len(posts) == 1 and out["committed"] is True
    assert "not verified" in out["result"]


# --- the posted value is the rendered one --------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "source", "body"),
    [
        ("restart", RESTART_FORM.format(value="Restart Now"), {"nonce": ["abc123"], "Restart": ["Restart Now"]}),
        (
            "clear-device-list",
            DEVICES_FORM.format(value="Clear It All"),
            {"nonce": ["abc123"], "Clear": ["Clear It All"]},
        ),
    ],
)
def test_the_posted_body_carries_the_value_the_page_renders_not_the_catalog_value(
    tmp_env, clock, monkeypatch, capsys, name, source, body
):
    _, _, posts, _ = _run(monkeypatch, capsys, name, source=source, location="/cgi-bin/devices.ha"
                          if name == "clear-device-list" else "/cgi-bin/restarting.ha")

    assert len(posts) == 1
    assert parse_qs(posts[0].body.decode()) == body
