"""check, sitemap, coverage and the raw page readers never report an unavailable page as data."""

import json

import pytest
from save_helpers import client_with, html

from bgwcli import cli

NOT_FOUND = "<html><title>Page not found</title></html>"
LOGIN = '<html><title>Login</title><form action="/cgi-bin/login.ha"><input name="nonce" value="n">' \
        '<input name="password"></form></html>'


def _run(monkeypatch, capsys, argv, body, status=200):
    client, _wire = client_with(lambda r, n: html(body, status=status))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_check_exits_2_when_the_gateway_is_not_reachable(tmp_env, monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, ["check"], "<html><title>Bad Gateway</title></html>", status=502)
    assert code == 2 and out["reachable"] is False


def test_check_exits_0_when_the_gateway_answers(tmp_env, monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, ["check"], "<html><title>Sitemap</title></html>")
    assert code == 0 and out["reachable"] is True


@pytest.mark.parametrize("argv", [["sitemap"], ["coverage"], ["page", "nosuch", "--raw"], ["logs", "--raw"]])
@pytest.mark.parametrize("body", [NOT_FOUND, LOGIN], ids=["not-found", "login"])
def test_unavailable_page_answers_are_exit_2(tmp_env, clock, monkeypatch, capsys, argv, body):
    code, out = _run(monkeypatch, capsys, argv, body)
    assert code == 2
    assert "raw" not in out and not isinstance(out, list)


def test_raw_page_with_content_is_still_printed(tmp_env, monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, ["page", "sysinfo", "--raw"], "<html><title>System</title>ok</html>")
    assert code == 0 and "ok" in out["raw"]


def test_logs_without_a_readable_log_table_is_exit_2(tmp_env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "parse_logs", lambda body: None)
    code, out = _run(monkeypatch, capsys, ["logs"], "<html><title>Logs</title><body>Please wait</body></html>")
    assert code == 2 and out["ok"] is False


@pytest.mark.parametrize("body", [
    "<html><title>Please wait</title><body>busy</body></html>",
    "",
    "<table><tr><td>a</td><td>b</td><td>c</td><td>d</td><td>e</td><td>f</td></tr></table>",
])
def test_logs_answer_without_a_log_table_is_exit_2_with_the_real_parser(tmp_env, monkeypatch, capsys, body):
    code, out = _run(monkeypatch, capsys, ["logs"], body)
    assert code == 2 and out["ok"] is False and "no log table" in out["error"]


def test_logs_answer_with_an_empty_log_table_is_an_empty_log(tmp_env, monkeypatch, capsys):
    header = "<tr><th>ID</th><th>Time</th><th>Source</th><th>Destination</th><th>Protocol</th><th>Reason</th></tr>"
    code, out = _run(monkeypatch, capsys, ["logs"], f"<table>{header}</table>")
    assert code == 0 and out == []


@pytest.mark.parametrize("argv", [["sitemap"], ["coverage"]])
def test_public_read_pool_full_records_the_cooldown(tmp_env, monkeypatch, capsys, argv):
    from bgwcli import session

    body = "<html><title>Login</title>All web server sessions are in use.</html>"
    client, _wire = client_with(lambda r, n: html(body))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--json"])
    assert code == 2
    assert session.read_session_state(client.session_identity()).pool_cooldown_until is not None


def test_router_text_in_a_snapshot_error_never_reaches_stderr_unsanitised(tmp_env, monkeypatch, capsys):
    hostile = "\x1b]0;pwn\x07‮evil"
    ipalloc = (
        '<form><input name="nonce" value="n"><table><tr><th>IPv4 Address / Name</th><th>MAC Address</th>'
        f"<th>Allocation</th></tr><tr><td>{hostile}</td><td>aa:bb:cc:dd:ee:01</td><td>Fixed</td></tr></table></form>"
    )
    from save_helpers import form

    def handler(request, n):
        if "ipalloc" in request.url:
            return html(ipalloc)
        return html(form("dosprotect", "old"))

    client, _wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["dump", "--out", str(tmp_env / "d.json")])
    err = capsys.readouterr().err
    assert code == 2
    assert "\x1b" not in err and "‮" not in err and "\x07" not in err


def test_sweep_raw_of_a_failed_page_writes_nothing_to_stdout(tmp_env, monkeypatch, capsys):
    client, _wire = client_with(lambda r, n: html("<title>Server Error</title>", status=500))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["sweep", "--pages", "sysinfo", "--raw"])
    captured = capsys.readouterr()
    assert code == 2 and captured.out == "" and "could not be read" in captured.err
