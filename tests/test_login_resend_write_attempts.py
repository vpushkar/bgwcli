"""A write re-sent once after a Login-page answer reports writeAttempts 2 on success too."""

import json
from urllib.parse import urlsplit

import pytest
from integration_html import EMPTY_SECTION_TABLES
from save_helpers import SAVED_RED, client_with, form, html

from bgwcli import cli
from bgwcli.dumpfile import write_dump_file
from bgwcli.snapshot import Snapshot, SnapshotMeta

LOGIN = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'


def _handler(page, *, resend):
    state = {"posts": 0}

    def handler(request, _n):
        if request.url.endswith("login.ha"):
            if request.method == "POST":
                return html("", 302, {"location": "/cgi-bin/home.ha"})
            return html(LOGIN)
        if request.method == "POST":
            state["posts"] += 1
            if resend and state["posts"] == 1:
                return html(LOGIN)
            return html("", 302, {"location": f"/cgi-bin/{page}.ha"})
        if state["posts"] >= 1:
            return html(form(page, "new", banner=SAVED_RED))
        return html(form(page, "old"))

    return handler


def _run(monkeypatch, capsys, page, token, *, resend):
    client, wire = client_with(_handler(page, resend=resend))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    monkeypatch.setattr(cli, "sleep", lambda s: None)
    code = cli.main(["set", page, "setting=new", "--commit", "--confirm", token, "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = sum(1 for r in wire.requests if r.method == "POST" and not urlsplit(r.url).path.endswith("login.ha"))
    return code, out, posts


@pytest.mark.parametrize("page,token", [("etherlan", "ETHERLAN"), ("wconfig", "WCONFIG")])
def test_a_successful_resend_reports_two_write_attempts(tmp_env, clock, monkeypatch, capsys, page, token):
    code, out, posts = _run(monkeypatch, capsys, page, token, resend=True)
    assert code == 0 and out["committed"] is True
    assert out["writeAttempts"] == 2
    assert posts == 2


ACTION_CASES = [
    ("plain", ["action", "run-speed-test", "--commit", "--confirm", "SPEED"]),
    ("post_path", ["action", "restart-wifi-2.4", "--commit", "--confirm", "RESTART-WIFI"]),
]


@pytest.mark.parametrize("resend", [True, False])
@pytest.mark.parametrize("name,argv", ACTION_CASES)
def test_an_action_reports_write_attempts_only_when_it_was_resent(
    tmp_env, clock, monkeypatch, capsys, name, argv, resend
):
    state = {"posts": 0}
    nonce_page = '<input name="nonce" value="abc123"><input type="submit" name="Go" value="Go">'

    def handler(request, _n):
        if request.url.endswith("login.ha"):
            if request.method == "POST":
                return html("", 302, {"location": "/cgi-bin/home.ha"})
            return html(LOGIN)
        if request.method == "POST":
            state["posts"] += 1
            if resend and state["posts"] == 1:
                return html(LOGIN)
            return html("", 302, {"location": "/cgi-bin/home.ha"})
        return html(nonce_page)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    monkeypatch.setattr(cli, "sleep", lambda s: None)
    code = cli.main([*argv, "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = sum(1 for r in wire.requests if r.method == "POST" and not urlsplit(r.url).path.endswith("login.ha"))
    assert code == 0 and out["committed"] is True
    assert posts == (2 if resend else 1)
    if resend:
        assert out["writeAttempts"] == 2
    else:
        assert "writeAttempts" not in out


@pytest.mark.parametrize("page,token", [("etherlan", "ETHERLAN"), ("wconfig", "WCONFIG")])
def test_a_single_post_success_has_no_write_attempts(tmp_env, clock, monkeypatch, capsys, page, token):
    code, out, posts = _run(monkeypatch, capsys, page, token, resend=False)
    assert code == 0 and out["committed"] is True
    assert "writeAttempts" not in out
    assert posts == 1


def _count_config_posts(wire):
    return sum(1 for r in wire.requests if r.method == "POST" and not urlsplit(r.url).path.endswith("login.ha"))


@pytest.mark.parametrize("resend", [True, False])
def test_the_lan_dhcp_save_reports_write_attempts_only_after_a_resend(tmp_env, clock, monkeypatch, capsys, resend):
    state = {"posts": 0, "wrote": False}

    def handler(request, _n):
        if request.url.endswith("login.ha"):
            if request.method == "POST":
                return html("", 302, {"location": "/cgi-bin/home.ha"})
            return html(LOGIN)
        if request.method == "POST":
            state["posts"] += 1
            if resend and state["posts"] == 1:
                return html(LOGIN)
            state["wrote"] = True
            return html("", 302, {"location": "/cgi-bin/dhcpserver.ha"})
        if state["wrote"]:
            return html(form("dhcpserver", "off", name="dhcp", banner=SAVED_RED))
        return html(form("dhcpserver", "on", name="dhcp"))

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    monkeypatch.setattr(cli, "sleep", lambda s: None)
    code = cli.main(["set", "dhcpserver", "dhcp=off", "--commit", "--confirm", "DHCPSERVER", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["committed"] is True and out["acknowledgementObserved"] is True
    assert _count_config_posts(wire) == (2 if resend else 1)
    if resend:
        assert out["writeAttempts"] == 2
    else:
        assert "writeAttempts" not in out


@pytest.mark.parametrize("resend", [True, False])
def test_a_restore_commit_step_reports_write_attempts_only_after_a_resend(
    tmp_env, clock, monkeypatch, capsys, resend
):
    path = tmp_env / "dump.json"
    write_dump_file(path, Snapshot(SnapshotMeta("", "", "router.local"), forms={"dosprotect": {"setting": "new"}}))
    state = {"posts": 0, "wrote": False}

    def handler(request, _n):
        if request.url.endswith("login.ha"):
            if request.method == "POST":
                return html("", 302, {"location": "/cgi-bin/home.ha"})
            return html(LOGIN)
        if request.method == "POST":
            state["posts"] += 1
            if resend and state["posts"] == 1:
                return html(LOGIN)
            state["wrote"] = True
            return html("", 302, {"location": "/cgi-bin/dosprotect.ha"})
        if state["wrote"]:
            return html(form("dosprotect", "new", banner=SAVED_RED) + EMPTY_SECTION_TABLES)
        return html(form("dosprotect", "old") + EMPTY_SECTION_TABLES)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    monkeypatch.setattr(cli, "sleep", lambda s: None)
    code = cli.main(["restore", str(path), "--include", "dosprotect", "--commit", "--confirm", "RESTORE", "--json"])
    out = json.loads(capsys.readouterr().out)
    step = out["execution"]["steps"][0]
    assert code == 0 and step["status"] == "applied"
    assert _count_config_posts(wire) == (2 if resend else 1)
    if resend:
        assert step["writeAttempts"] == 2
    else:
        assert "writeAttempts" not in step
