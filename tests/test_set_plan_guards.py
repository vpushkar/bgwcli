"""set refuses a disabled Save control and keeps its no-Save advisory on a result that did not commit."""

import json

from save_helpers import ERROR, client_with, form, html

from bgwcli import cli


def _run(monkeypatch, capsys, handler, argv):
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--json"])
    captured = capsys.readouterr()
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    return code, captured, posts


def test_set_refuses_a_disabled_save_button(tmp_env, clock, monkeypatch, capsys):
    body = form("etherlan", "old").replace('name="Save" value="Save"', 'name="Save" value="Save" disabled')
    code, captured, posts = _run(monkeypatch, capsys, lambda r, n: html(body),
                                 ["set", "etherlan", "setting=new", "--commit", "--confirm", "ETHERLAN"])
    assert code == 1 and posts == []
    assert "disabled" in json.loads(captured.out)["error"]


def test_the_no_save_advisory_survives_a_rejected_set(tmp_env, clock, monkeypatch, capsys):
    page = form("etherlan", "old").replace('<input type="submit" name="Save" value="Save">', "")

    def handler(request, n):
        if request.method == "POST":
            return html(ERROR + page)
        return html(page)

    code, captured, posts = _run(monkeypatch, capsys, handler,
                                 ["set", "etherlan", "setting=new", "--commit", "--confirm", "ETHERLAN"])
    out = json.loads(captured.out)
    assert code == 1 and len(posts) == 1 and out["committed"] is False
    assert "has no Save button" in out["warning"] and "rejected" in out["warning"]


def test_a_redirect_answer_without_the_error_icon_that_says_no_changes_is_unchanged(
    tmp_env, clock, monkeypatch, capsys
):
    state = {"posted": False}
    base = form("diag", "old").replace('name="Save" value="Save"', 'name="Ping" value="Ping"')

    def handler(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html("", 302, {"location": "/cgi-bin/diag.ha"})
        notice = '<div id="error-message-text">No changes detected. Save not performed.</div>'
        return html(base + (notice if state["posted"] else ""))

    code, captured, posts = _run(monkeypatch, capsys, handler,
                                 ["submit", "diag", "Ping", "--commit", "--confirm", "DIAG"])
    out = json.loads(captured.out)
    assert len(posts) == 1
    assert out["committed"] is False and out["outcome"] == "unchanged" and code == 0
