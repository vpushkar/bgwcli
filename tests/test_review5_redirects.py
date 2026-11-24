"""Fifth review round: where a write's redirect leads, and which Wi-Fi Warning may be confirmed.

- the Wi-Fi Warning's Continue is posted only after a Wi-Fi page's Save (wconfig, wconfig_unified),
  only to that same page, and only while the Continue button is still on the page its nonce is read
  from;
- the warning page's query (``wifiwarn_advanced.ha?tx=42``) is kept on both warning reads;
- a malformed redirect is reported (with the POST's evidence) before any poll or follow-up read;
- a write's answer page is the redirect's resolved same-origin /cgi-bin/ page, path and query
  exactly: a relative ``devices.ha`` is read, ``/?next=/cgi-bin/restarting.ha`` is never read, and
  another origin is never contacted.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest
from save_helpers import ERROR, SAVED_RED, client_with, form, html

from bgwcli import autorestore, cli
from bgwcli.dumpfile import write_dump_file
from bgwcli.restore import RestoreFollowUp, RestoreStep, execute_restore
from bgwcli.snapshot import Snapshot, SnapshotMeta

WARNING_URL = "/cgi-bin/wifiwarn_advanced.ha"
CLEAR_FORM = (
    '<form method="post" action="/cgi-bin/devices.ha"><input type="hidden" name="nonce" value="abc123" />'
    '<input type="submit" name="Clear" value="Clear and Rescan for Devices" /></form>'
)


def _warning(action: str = "/cgi-bin/wconfig.ha", *, with_continue: bool = True) -> str:
    button = '<input type="submit" name="Continue" value="Continue">' if with_continue else ""
    return (
        f'<html><body><h1>Wi-Fi Warning</h1><form method="post" action="{action}">'
        f'<input type="hidden" name="nonce" value="aa01"><input type="hidden" name="x" value="1">{button}</form>'
        f'<form method="post" action="{WARNING_URL}"><input type="hidden" name="nonce" value="aa02">'
        '<input type="submit" name="Cancel" value="Cancel"></form></body></html>'
    )


def _dump(tmp_env, page: str) -> str:
    path = tmp_env / "dump.json"
    write_dump_file(path, Snapshot(SnapshotMeta("", "", "router.local"), forms={page: {"setting": "new"}}))
    return str(path)


def _argv(kind: str, tmp_env, page: str) -> list[str]:
    token = page.upper().replace("_", "-")
    if kind == "set":
        return ["set", page, "setting=new", "--commit", "--confirm", token]
    if kind == "submit":
        return ["submit", page, "Save", "--commit", "--confirm", token]
    return ["restore", _dump(tmp_env, page), "--include", page, "--commit", "--confirm", "RESTORE"]


def _run(monkeypatch, capsys, handler, argv):
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    monkeypatch.setattr(autorestore, "_sleep", lambda _s: None)
    code = cli.main([*argv, "--json"])
    captured = capsys.readouterr().out
    out = json.loads(captured) if captured.strip() else {}
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    return code, out, posts, wire


def _save_handler(save_location: str, warning_html, *, continue_location: str | None = None):
    """Save answered `save_location`; the warning page is `warning_html(n)` (n = warning reads so far);
    Continue is answered `continue_location` (default: the saved page), after which the page says saved."""
    state = {"saved": False, "warnings": 0}

    def handle(request, n):
        path = urlsplit(request.url).path
        body = request.body.decode() if isinstance(request.body, bytes) else str(request.body or "")
        page = path.rsplit("/", 1)[-1].removesuffix(".ha")
        if request.method == "POST":
            if "Continue" in body:
                state["saved"] = True
                return html("", 302, {"location": continue_location or f"/cgi-bin/{page}.ha"})
            return html("", 302, {"location": save_location})
        if page.startswith("wifiwarn"):
            state["warnings"] += 1
            return html(warning_html(state["warnings"]))
        return html(form(page, "new", "setting", SAVED_RED) if state["saved"] else form(page, "old", "setting"))

    return handle


def _gets(wire, *, after_first_post: bool = True) -> list[str]:
    requests = wire.requests
    if after_first_post:
        first = next(i for i, r in enumerate(requests) if r.method == "POST" and not r.url.endswith("login.ha"))
        requests = requests[first + 1 :]
    return [urlsplit(r.url).path + (f"?{urlsplit(r.url).query}" if urlsplit(r.url).query else "")
            for r in requests if r.method == "GET"]


# --- 2a: Continue only after a Wi-Fi page's Save, only to that page -----------------------------------


@pytest.mark.parametrize("kind", ["set", "submit", "restore"])
def test_a_wifi_warning_after_a_non_wifi_save_is_never_confirmed(clock, tmp_env, monkeypatch, capsys, kind):
    handler = _save_handler(WARNING_URL, lambda n: _warning("/cgi-bin/wconfig.ha"))
    code, out, posts, wire = _run(monkeypatch, capsys, handler, _argv(kind, tmp_env, "etherlan"))
    assert [r.url.rsplit("/", 1)[-1] for r in posts] == ["etherlan.ha"], "only the Save was posted"
    assert code == 2
    text = json.dumps(out)
    assert "Continue was not posted" in text and "etherlan" in text
    assert not any(p.startswith(WARNING_URL) for p in _gets(wire)), "the warning page is not even read"


@pytest.mark.parametrize("kind", ["set", "restore"])
@pytest.mark.parametrize("action", ["/cgi-bin/reset.ha", "/cgi-bin/wconfig_unified.ha"])
def test_continue_is_posted_only_to_the_page_whose_save_was_redirected(
    clock, tmp_env, monkeypatch, capsys, kind, action
):
    handler = _save_handler(WARNING_URL, lambda n: _warning(action))
    code, out, posts, _ = _run(monkeypatch, capsys, handler, _argv(kind, tmp_env, "wconfig"))
    assert [r.url.rsplit("/", 1)[-1] for r in posts] == ["wconfig.ha"], "only the Save was posted"
    assert code == 2 and "Continue was not posted" in json.dumps(out)


@pytest.mark.parametrize("kind", ["set", "restore"])
def test_continue_must_still_be_on_the_warning_page_read_for_its_nonce(clock, tmp_env, monkeypatch, capsys, kind):
    # The first read shows Continue; the nonce read for the Continue POST no longer does.
    handler = _save_handler(WARNING_URL, lambda n: _warning(with_continue=n == 1))
    code, out, posts, _ = _run(monkeypatch, capsys, handler, _argv(kind, tmp_env, "wconfig"))
    assert [r.url.rsplit("/", 1)[-1] for r in posts] == ["wconfig.ha"], "Continue was not posted"
    assert code == 2 and "Continue was not posted" in json.dumps(out)


@pytest.mark.parametrize("kind", ["set", "submit"])
def test_a_wconfig_unified_save_follows_the_live_warnings_continue_to_wconfig(
    clock, tmp_env, monkeypatch, capsys, kind
):
    # The live wifiwarn_advanced.ha (fw 6.35.8, read 2026-11-24) posts Continue to /cgi-bin/wconfig.ha
    # whichever Wi-Fi page was saved.
    handler = _save_handler(WARNING_URL, lambda n: _warning("/cgi-bin/wconfig.ha"))
    code, out, posts, _ = _run(monkeypatch, capsys, handler, _argv(kind, tmp_env, "wconfig_unified"))
    assert [r.url.rsplit("/", 1)[-1] for r in posts] == ["wconfig_unified.ha", "wconfig.ha"]
    assert "Continue" in posts[1].body.decode() and code == 0, out


@pytest.mark.parametrize(("kind", "page"), [("set", "wconfig"), ("restore", "wconfig"), ("set", "wconfig_unified")])
def test_a_wifi_pages_own_continue_is_still_posted(clock, tmp_env, monkeypatch, capsys, kind, page):
    handler = _save_handler(WARNING_URL, lambda n: _warning(f"/cgi-bin/{page}.ha"))
    code, _, posts, _ = _run(monkeypatch, capsys, handler, _argv(kind, tmp_env, page))
    assert [r.url.rsplit("/", 1)[-1] for r in posts] == [f"{page}.ha", f"{page}.ha"]
    assert "Continue" in posts[1].body.decode() and code == 0


# --- 2e: the warning's query is kept on both reads -----------------------------------------------------


@pytest.mark.parametrize("kind", ["set", "restore"])
def test_the_warning_query_is_kept_on_both_warning_reads(clock, tmp_env, monkeypatch, capsys, kind):
    handler = _save_handler(f"{WARNING_URL}?tx=42", lambda n: _warning())
    code, _, posts, wire = _run(monkeypatch, capsys, handler, _argv(kind, tmp_env, "wconfig"))
    warning_reads = [p for p in _gets(wire) if p.startswith(WARNING_URL)]
    assert warning_reads == [f"{WARNING_URL}?tx=42", f"{WARNING_URL}?tx=42"]
    assert len(posts) == 2 and "Continue" in posts[1].body.decode() and code == 0


# --- 2c: a malformed redirect is reported before any poll or follow-up ---------------------------------


@pytest.mark.parametrize("kind", ["set", "restore"])
@pytest.mark.parametrize(
    "location",
    ["http://router.local:bad/cgi-bin/wifiwarn_advanced.ha", "http://router.local:bad/cgi-bin/wconfig.ha",
     "http://[bad/cgi-bin/wconfig.ha"],
)
def test_a_malformed_save_redirect_is_unknown_before_any_poll(clock, tmp_env, monkeypatch, capsys, kind, location):
    handler = _save_handler(location, lambda n: html(SAVED_RED).body.decode())
    code, out, posts, wire = _run(monkeypatch, capsys, handler, _argv(kind, tmp_env, "wconfig"))
    assert code == 2 and len(posts) == 1
    if kind == "set":
        assert _gets(wire) == [], "nothing is read after the malformed answer"
    else:
        # restore's own whole-snapshot verification re-reads the saved page; nothing the answer named is read
        assert out["execution"]["steps"][0]["status"] == "failed"
        assert not any("wifiwarn" in p for p in _gets(wire))
    text = json.dumps(out)
    assert "malformed redirect" in text and '"writeAttempted": true' in text


class _Poster:
    def __init__(self, location: str) -> None:
        self.location = location
        self.gets: list[str] = []
        self.posts: list[str] = []

    def post_cgi_page(self, page, payload):
        self.posts.append(page)
        from bgwcli.types import HttpResponse

        return HttpResponse(302, "Found", {"location": self.location}, "", f"http://router.local/cgi-bin/{page}.ha")

    def post_form(self, nonce_page, post_path, fields, **_kwargs):
        raise AssertionError("no second POST")

    def get_cgi_page(self, page, **_kwargs):
        self.gets.append(page)
        raise AssertionError(f"nothing is read after a malformed redirect ({page})")

    def session_identity(self) -> str:
        return "http://router.local"


def test_a_malformed_redirect_stops_before_the_follow_up_read():
    poster = _Poster("http://router.local:bad/cgi-bin/ipalloc.ha")
    step = RestoreStep(
        1, "form", "ipalloc", "allocate", raw_payload={"Allocate": "Allocate"},
        follow_up=RestoreFollowUp("ipalloc", "alloc_02:0a:0b:0c:0d:01", "192.168.1.70", "Save"),
    )
    execution = execute_restore(poster, [step])
    result = execution.steps[0]
    assert result.status == "failed" and poster.gets == [] and poster.posts == ["ipalloc"]
    assert "malformed redirect" in (result.error or "")


# --- 2d: the answer page is the resolved same-origin /cgi-bin/ page, path and query exactly ------------


def _clear_handler(location: str, answer: str):
    state = {"posted": False}

    def handle(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html("", 302, {"location": location})
        if state["posted"]:
            return html(answer)
        return html(CLEAR_FORM)

    return handle


CLEAR = ["action", "clear-device-list", "--commit"]


def test_a_relative_redirect_is_read_and_its_rejection_counts(clock, tmp_env, monkeypatch, capsys):
    code, out, posts, wire = _run(monkeypatch, capsys, _clear_handler("devices.ha", ERROR + CLEAR_FORM), CLEAR)
    assert len(posts) == 1 and _gets(wire) == ["/cgi-bin/devices.ha"]
    assert code == 1 and "A required setting is empty" in json.dumps(out)


def test_the_answer_read_keeps_the_redirects_query(clock, tmp_env, monkeypatch, capsys):
    code, _, _, wire = _run(monkeypatch, capsys, _clear_handler("/cgi-bin/devices.ha?view=1", CLEAR_FORM), CLEAR)
    assert _gets(wire) == ["/cgi-bin/devices.ha?view=1"] and code == 0


def test_a_query_that_names_a_cgi_page_is_never_read(clock, tmp_env, monkeypatch, capsys):
    _, _, posts, wire = _run(
        monkeypatch, capsys, _clear_handler("/?next=/cgi-bin/restarting.ha", CLEAR_FORM), CLEAR
    )
    assert len(posts) == 1 and not any("restarting" in p for p in _gets(wire))


def test_an_answer_on_another_origin_is_never_read_and_is_unknown(clock, tmp_env, monkeypatch, capsys):
    code, out, posts, wire = _run(
        monkeypatch, capsys, _clear_handler("http://other.example/cgi-bin/devices.ha", CLEAR_FORM), CLEAR
    )
    assert len(posts) == 1 and _gets(wire) == []
    assert all(urlsplit(r.url).hostname == "router.local" for r in wire.requests)
    assert code == 2 and out["committed"] is False and out["writeAttempted"] is True


def test_a_save_poll_reads_the_redirects_page_and_query_not_a_page_named_in_its_query(
    clock, tmp_env, monkeypatch, capsys
):
    handler = _save_handler("/?next=/cgi-bin/restarting.ha", lambda n: _warning())
    _, _, posts, wire = _run(monkeypatch, capsys, handler, _argv("set", tmp_env, "etherlan"))
    assert len(posts) == 1 and not any("restarting" in p for p in _gets(wire))
    handler = _save_handler("status.ha?x=1", lambda n: _warning())
    _, _, _, wire = _run(monkeypatch, capsys, handler, _argv("set", tmp_env, "etherlan"))
    assert _gets(wire)[0] == "/cgi-bin/status.ha?x=1"
