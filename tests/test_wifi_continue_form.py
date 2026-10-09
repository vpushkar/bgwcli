"""The Wi-Fi Warning's Continue is posted only to the form that owns the button, resolved exactly.

The owning form's action must reduce to `<page>.ha` (rooted under /cgi-bin/ or relative, no query).
Anything else is refused before Continue is posted: Continue posted to the warning page itself is
answered 302 by the gateway and silently discards the Wi-Fi change, which would also hide that the
change was never applied. The Save's evidence stays on the refusal. The real client runs over a
scripted wire and every test counts the POSTs.
"""

from __future__ import annotations

import json

import pytest
from save_helpers import SAVED_RED, client_with, form, html

from bgwcli import autorestore, cli
from bgwcli.dumpfile import write_dump_file
from bgwcli.snapshot import Snapshot, SnapshotMeta

NONCE = "abc123"
WARNING_URL = "/cgi-bin/wifiwarn_advanced.ha"


def _warning(action: str | None) -> str:
    """A warning page whose Continue button sits in a form with `action` (None: in no form at all)."""
    button = '<input type="submit" name="Continue" value="Continue">'
    cancel = (
        f'<form method="post" action="{WARNING_URL}"><input type="hidden" name="nonce" value="cancel-nonce">'
        '<input type="submit" name="Cancel" value="Cancel"></form>'
    )
    if action is None:
        return f"<html><body><h1>Wi-Fi Warning</h1>{button}{cancel}</body></html>"
    return (
        f'<html><body><h1>Wi-Fi Warning</h1><form method="post" action="{action}">'
        f'<input type="hidden" name="nonce" value="{NONCE}">{button}</form>{cancel}</body></html>'
    )


def _handler(warning_html: str, continue_page: str = "wconfig"):
    state = {"saved": False}

    def handle(request, n):
        path = request.url.rsplit("/", 1)[-1]
        body = request.body.decode() if isinstance(request.body, bytes) else str(request.body or "")
        if request.method == "POST":
            if path == "login.ha":
                return html("", 302, {"location": "/cgi-bin/home.ha"})
            if "Continue" in body:
                state["saved"] = True
                return html("", 302, {"location": f"/cgi-bin/{continue_page}.ha"})
            return html("", 302, {"location": WARNING_URL})
        if path.startswith("wifiwarn"):
            return html(warning_html)
        page = path.removesuffix(".ha")
        return html(form(page, "new", "setting", SAVED_RED) if state["saved"] else form(page, "old", "setting"))

    return handle


def _dump(tmp_env, page: str = "wconfig"):
    path = tmp_env / "dump.json"
    write_dump_file(path, Snapshot(SnapshotMeta("", "", "router.local"), forms={page: {"setting": "new"}}))
    return str(path)


def _argv(kind: str, tmp_env, page: str = "wconfig") -> list[str]:
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
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    return code, out, posts


UNRESOLVED = [
    pytest.param(None, id="bare-button-in-no-form"),
    pytest.param("", id="empty-action"),
    pytest.param("/cgi-bin/wconfig.ha?1", id="query-string"),
    pytest.param("/x/wconfig.ha", id="not-under-cgi-bin"),
    pytest.param("http://other.example/wconfig.ha", id="other-host"),
    pytest.param("http://other.example/cgi-bin/wconfig.ha", id="other-host-under-cgi-bin"),
    pytest.param("/foo/cgi-bin/wconfig.ha", id="nested-cgi-bin"),
]


@pytest.mark.parametrize("kind", ["set", "submit", "restore"])
@pytest.mark.parametrize("action", UNRESOLVED)
def test_an_unresolvable_continue_form_is_refused_and_keeps_the_saves_evidence(
    clock, tmp_env, monkeypatch, capsys, kind, action
):
    code, out, posts = _run(monkeypatch, capsys, _handler(_warning(action)), _argv(kind, tmp_env))
    assert [r.url.rsplit("/", 1)[-1] for r in posts] == ["wconfig.ha"], "only the Save was posted"
    assert all("Continue" not in r.body.decode() for r in posts)
    assert code == 2
    text = json.dumps(out)
    assert "Continue was not posted" in text
    assert "discards an unconfirmed Wi-Fi change" in text
    if kind == "restore":
        step = out["execution"]["steps"][0]
        assert step["status"] == "failed"
        assert step["writeAttempted"] is True and step["writeResponseReceived"] is True
        assert step["statusCode"] == 302 and step["location"] == WARNING_URL
    else:
        assert out["committed"] is False and out["outcome"] == "failed"
        assert out["writeAttempted"] is True and out["writeResponseReceived"] is True
        assert out["statusCode"] == 302 and out["location"] == WARNING_URL


def test_the_refusal_names_the_unusable_form_action(clock, tmp_env, monkeypatch, capsys):
    _, out, _ = _run(monkeypatch, capsys, _handler(_warning("/cgi-bin/wconfig.ha?1")), _argv("set", tmp_env))
    assert "posts to /cgi-bin/wconfig.ha?1, not to a page this tool can confirm" in out["warning"]
    _, out, _ = _run(monkeypatch, capsys, _handler(_warning(None)), _argv("set", tmp_env))
    assert "posts to no action, not to a page this tool can confirm" in out["warning"]


@pytest.mark.parametrize(
    ("kind", "action", "page", "target"),
    [
        ("set", "/cgi-bin/wconfig.ha", "wconfig", "wconfig.ha"),
        ("set", "wconfig.ha", "wconfig", "wconfig.ha"),
        ("set", "/cgi-bin/wconfig_unified.ha", "wconfig_unified", "wconfig_unified.ha"),
        ("restore", "/cgi-bin/wconfig.ha", "wconfig", "wconfig.ha"),
        ("restore", "wconfig.ha", "wconfig", "wconfig.ha"),
    ],
)
def test_a_live_shaped_continue_form_is_posted_with_the_warning_pages_nonce(
    clock, tmp_env, monkeypatch, capsys, kind, action, page, target
):
    code, out, posts = _run(
        monkeypatch, capsys, _handler(_warning(action), page), _argv(kind, tmp_env, page)
    )
    assert [r.url.rsplit("/", 1)[-1] for r in posts] == [f"{page}.ha", target], "the Save, then Continue"
    assert "Continue" in posts[1].body.decode() and NONCE in posts[1].body.decode()
    assert "cancel-nonce" not in posts[1].body.decode()
    assert code == 0


@pytest.mark.parametrize("action", [None, "/cgi-bin/wconfig.ha?1"])
def test_autorestore_counts_an_unresolved_continue_like_a_refused_one(
    clock, tmp_env, monkeypatch, capsys, action
):
    code, out, posts = _run(
        monkeypatch, capsys, _handler(_warning(action)),
        ["autorestore", _dump(tmp_env), "--include", "wconfig", "--host", "router.local", "--commit",
         "--confirm", "RESTORE", "--max-passes", "3", "--wait", "120"],
    )
    assert [r.url.rsplit("/", 1)[-1] for r in posts] == ["wconfig.ha"], "the Save was never re-posted"
    assert out["status"] == "error" and code == 2 and out["writeUnanswered"] is True
    assert "the Save was answered and the Continue was not posted" in out["reason"]
    assert "got no answer" not in out["reason"]
