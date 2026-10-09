"""An explicitly empty --host is a usage error, and Ctrl-C is a reported outcome (exit 130) with write evidence."""

from __future__ import annotations

import json

import pytest
from save_helpers import client_with, form, html

from bgwcli import cli


@pytest.fixture
def factory(monkeypatch, tmp_env):
    seen = {}

    def install(client):
        monkeypatch.setattr(cli, "_client_factory", lambda options, *a, **kw: seen.setdefault("o", options) and client)

    monkeypatch.setenv("BGW_ACCESS_CODE", "unused")
    seen["install"] = install
    return seen


@pytest.mark.parametrize("value", ["", " ", "\t  "])
def test_an_empty_host_is_a_usage_error_never_a_fallback(capsys, factory, monkeypatch, value):
    monkeypatch.setenv("BGW_HOST", "http://env.local")
    client, wire = client_with(lambda r, n: html(""))
    factory["install"](client)
    code = cli.main(["check", "--host", value])
    captured = capsys.readouterr()
    assert code == 1 and captured.out == ""
    assert "--host" in captured.err and "empty" in captured.err
    assert wire.calls == [], "nothing was contacted"
    assert "o" not in factory, "no client was built for another origin"


def test_an_empty_host_is_a_json_usage_error(capsys, factory):
    client, wire = client_with(lambda r, n: html(""))
    factory["install"](client)
    code = cli.main(["check", "--host", "", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["errorType"] == "UsageError" and wire.calls == []


def test_an_absent_host_still_falls_back_to_the_environment(capsys, factory, monkeypatch):
    monkeypatch.setenv("BGW_HOST", "http://env.local")
    client, _ = client_with(lambda r, n: html("<title>Ok</title>"))
    factory["install"](client)
    cli.main(["check", "--json"])
    capsys.readouterr()
    assert factory["o"].host == "http://env.local"


@pytest.mark.parametrize("value", ["", "  \t"])
def test_an_empty_bgw_host_in_the_environment_is_a_usage_error(capsys, factory, monkeypatch, value):
    monkeypatch.setenv("BGW_HOST", value)
    client, wire = client_with(lambda r, n: html(""))
    factory["install"](client)
    code = cli.main(["check"])
    captured = capsys.readouterr()
    assert code == 1 and captured.out == ""
    assert "BGW_HOST is empty" in captured.err
    assert wire.calls == [] and "o" not in factory


def test_an_empty_bgw_host_is_a_json_usage_error(capsys, factory, monkeypatch):
    monkeypatch.setenv("BGW_HOST", "")
    client, wire = client_with(lambda r, n: html(""))
    factory["install"](client)
    code = cli.main(["check", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["errorType"] == "UsageError" and "BGW_HOST" in out["error"] and wire.calls == []


def test_an_empty_router_ip_alone_is_a_usage_error(capsys, factory, monkeypatch):
    monkeypatch.setenv("ROUTER_IP", " ")
    client, wire = client_with(lambda r, n: html(""))
    factory["install"](client)
    code = cli.main(["check"])
    captured = capsys.readouterr()
    assert code == 1 and "ROUTER_IP is empty" in captured.err and wire.calls == []


def test_an_empty_bgw_host_is_never_skipped_for_router_ip(capsys, factory, monkeypatch):
    monkeypatch.setenv("BGW_HOST", "")
    monkeypatch.setenv("ROUTER_IP", "10.0.0.1")
    client, wire = client_with(lambda r, n: html(""))
    factory["install"](client)
    code = cli.main(["check"])
    captured = capsys.readouterr()
    assert code == 1 and "BGW_HOST is empty" in captured.err and wire.calls == []


def test_a_valid_bgw_host_never_consults_an_empty_router_ip(capsys, factory, monkeypatch):
    monkeypatch.setenv("BGW_HOST", "192.0.2.9")
    monkeypatch.setenv("ROUTER_IP", "")
    client, _ = client_with(lambda r, n: html("<title>Ok</title>"))
    factory["install"](client)
    cli.main(["check", "--json"])
    capsys.readouterr()
    assert factory["o"].host == "192.0.2.9"


@pytest.mark.parametrize("value", ["", "  \t"])
def test_an_explicit_host_wins_over_an_empty_bgw_host(capsys, factory, monkeypatch, value):
    monkeypatch.setenv("BGW_HOST", value)
    client, _ = client_with(lambda r, n: html("<title>Ok</title>"))
    factory["install"](client)
    code = cli.main(["check", "--host", "127.0.0.1", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    assert "BGW_HOST" not in captured.err + captured.out
    assert factory["o"].host == "127.0.0.1"


@pytest.mark.parametrize("argv", [["help"], ["--help"], []])
def test_help_never_consults_an_empty_host_environment(capsys, factory, monkeypatch, argv):
    monkeypatch.setenv("BGW_HOST", "")
    monkeypatch.setenv("ROUTER_IP", "")
    client, wire = client_with(lambda r, n: html(""))
    factory["install"](client)
    code = cli.main(argv)
    captured = capsys.readouterr()
    assert code == 0 and "empty" not in captured.err and captured.out.strip()
    assert wire.calls == []


def test_an_empty_explicit_host_is_its_own_error_not_the_environment_one(capsys, factory, monkeypatch):
    monkeypatch.setenv("BGW_HOST", "")
    client, wire = client_with(lambda r, n: html(""))
    factory["install"](client)
    code = cli.main(["check", "--host", ""])
    captured = capsys.readouterr()
    assert code == 1 and "--host is empty" in captured.err and "BGW_HOST is empty" not in captured.err
    assert wire.calls == []


@pytest.mark.parametrize(
    ("router_ip", "expected"),
    [
        (None, "unset BGW_HOST to use the default 192.168.1.254"),
        ("", "unset BGW_HOST to use the default 192.168.1.254"),
        ("  ", "unset BGW_HOST to use the default 192.168.1.254"),
        ("10.0.0.1", "unset BGW_HOST to use ROUTER_IP"),
    ],
)
def test_the_empty_bgw_host_message_names_the_real_fallback(capsys, factory, monkeypatch, router_ip, expected):
    monkeypatch.setenv("BGW_HOST", "")
    if router_ip is not None:
        monkeypatch.setenv("ROUTER_IP", router_ip)
    client, wire = client_with(lambda r, n: html(""))
    factory["install"](client)
    code = cli.main(["check"])
    err = capsys.readouterr().err
    assert code == 1 and "BGW_HOST is empty" in err and expected in err and wire.calls == []


def test_an_empty_router_ip_message_names_the_default(monkeypatch, tmp_env):
    from bgwcli.config import env_host
    from bgwcli.errors import UsageError

    monkeypatch.setenv("ROUTER_IP", "")
    with pytest.raises(UsageError, match="unset ROUTER_IP to use the default 192.168.1.254"):
        env_host()


def test_env_default_options_takes_the_host_without_consulting_the_environment(monkeypatch, tmp_env):
    from bgwcli.config import env_default_options

    monkeypatch.setenv("BGW_HOST", "")
    assert env_default_options(host="127.0.0.1").host == "127.0.0.1"
    monkeypatch.delenv("BGW_HOST")
    monkeypatch.setenv("ROUTER_IP", "10.0.0.1")
    assert env_default_options(host="127.0.0.1").host == "127.0.0.1"
    assert env_default_options().host == "10.0.0.1"


def test_env_default_options_still_validates_other_variables_with_a_host(monkeypatch, tmp_env):
    from bgwcli.config import env_default_options
    from bgwcli.errors import UsageError

    monkeypatch.setenv("BGW_TIMEOUT_MS", "0")
    with pytest.raises(UsageError, match="BGW_TIMEOUT_MS"):
        env_default_options(host="127.0.0.1")


def test_env_host_matrix(monkeypatch, tmp_env):
    from bgwcli.config import DEFAULT_HOST, env_host
    from bgwcli.errors import UsageError

    assert env_host() == DEFAULT_HOST
    monkeypatch.setenv("ROUTER_IP", "10.0.0.1")
    assert env_host() == "10.0.0.1"
    monkeypatch.setenv("BGW_HOST", "192.0.2.9")
    assert env_host() == "192.0.2.9"
    monkeypatch.setenv("ROUTER_IP", "")
    assert env_host() == "192.0.2.9"
    monkeypatch.setenv("BGW_HOST", " \t")
    with pytest.raises(UsageError, match="BGW_HOST is empty.*to use the default 192.168.1.254"):
        env_host()
    monkeypatch.setenv("ROUTER_IP", "10.0.0.1")
    with pytest.raises(UsageError, match="BGW_HOST is empty.*to use ROUTER_IP"):
        env_host()
    monkeypatch.setenv("ROUTER_IP", "")
    monkeypatch.delenv("BGW_HOST")
    with pytest.raises(UsageError, match="ROUTER_IP is empty.*the default"):
        env_host()


def test_ctrl_c_before_any_write_is_exit_130_and_interrupted(capsys, factory):
    def handler(request, number):
        raise KeyboardInterrupt

    client, wire = client_with(handler)
    factory["install"](client)
    code = cli.main(["check"])
    captured = capsys.readouterr()
    assert code == 130 and captured.out == ""
    assert "interrupted" in captured.err.lower()
    assert "Traceback" not in captured.err


def test_ctrl_c_json_is_a_structured_interrupted_object(capsys, factory):
    client, _ = client_with(lambda r, n: (_ for _ in ()).throw(KeyboardInterrupt()))
    factory["install"](client)
    code = cli.main(["check", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 130 and out["errorType"] == "Interrupted" and out["ok"] is False and out["exitCode"] == 130
    assert out["writeAttempted"] is False


def test_ctrl_c_during_a_write_reports_that_the_write_state_is_unknown(capsys, factory):
    def handler(request, number):
        if request.method == "POST":
            raise KeyboardInterrupt
        return html(form("dosprotect", "old"))

    client, wire = client_with(handler)
    factory["install"](client)
    code = cli.main(
        ["set", "dosprotect", "setting=new", "--commit", "--confirm", "DOSPROTECT", "--json"]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 130 and out["errorType"] == "Interrupted"
    assert out["writeAttempted"] is True and out["writeResponseReceived"] is False
    assert "unknown" in out["error"].lower() and "write" in out["error"].lower()
    assert sum(r.method == "POST" for r in wire.requests) == 1
