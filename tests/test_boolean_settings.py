"""Every boolean variable takes 1/true/yes/on or 0/false/no/off; anything else is a usage error."""

from __future__ import annotations

import pytest

from bgwcli import cli, config
from bgwcli.config import env_flag
from bgwcli.errors import UsageError

TRUE_SPELLINGS = ["1", "true", "TRUE", "True", "yes", "on", " on "]
FALSE_SPELLINGS = ["0", "false", "FALSE", "no", "off", " No "]


@pytest.mark.parametrize("value", TRUE_SPELLINGS)
def test_true_spellings(tmp_env, monkeypatch, value):
    monkeypatch.setenv("BGW_JSON", value)
    assert env_flag("BGW_JSON", False) is True


@pytest.mark.parametrize("value", FALSE_SPELLINGS)
def test_false_spellings(tmp_env, monkeypatch, value):
    monkeypatch.setenv("BGW_JSON", value)
    assert env_flag("BGW_JSON", True) is False


def test_unset_or_empty_gives_the_default(tmp_env, monkeypatch):
    assert env_flag("BGW_JSON", False) is False and env_flag("BGW_INSECURE_TLS", True) is True
    monkeypatch.setenv("BGW_JSON", "")
    monkeypatch.setenv("BGW_INSECURE_TLS", "  ")
    assert env_flag("BGW_JSON", False) is False and env_flag("BGW_INSECURE_TLS", True) is True


@pytest.mark.parametrize("value", ["maybe", "2", "-1", "ja", "t", "y"])
def test_anything_else_is_a_usage_error_naming_the_spellings(tmp_env, monkeypatch, value):
    monkeypatch.setenv("BGW_WAIT_FOR_SESSION", value)
    with pytest.raises(UsageError) as info:
        config.env_default_options()
    assert str(info.value) == f"BGW_WAIT_FOR_SESSION must be 1, true, yes, on or 0, false, no, off; got {value!r}."


def test_every_boolean_variable_goes_through_the_reader(tmp_env, monkeypatch):
    monkeypatch.setenv("BGW_INSECURE_TLS", "false")
    monkeypatch.setenv("BGW_WAIT_FOR_SESSION", "yes")
    monkeypatch.setenv("BGW_JSON", "on")
    monkeypatch.setenv("BGW_DUMP_ALL_CLIENTS", "True")
    monkeypatch.setenv("BGW_AUTORESTORE_ON_ANY_DIFF", "1")
    command = cli.parse_args(["dump"])
    assert (command.options.insecure_tls, command.options.wait_for_session, command.options.json) == (
        False, True, True,
    )
    assert (command.all_clients, command.on_any_diff) == (True, True)
    monkeypatch.setenv("BGW_INSECURE_TLS", "yes")
    monkeypatch.setenv("BGW_WAIT_FOR_SESSION", "0")
    monkeypatch.setenv("BGW_JSON", "off")
    monkeypatch.setenv("BGW_DUMP_ALL_CLIENTS", "no")
    monkeypatch.setenv("BGW_AUTORESTORE_ON_ANY_DIFF", "False")
    command = cli.parse_args(["dump"])
    assert (command.options.insecure_tls, command.options.wait_for_session, command.options.json) == (
        True, False, False,
    )
    assert (command.all_clients, command.on_any_diff) == (False, False)


@pytest.mark.parametrize(
    "name", ["BGW_JSON", "BGW_DUMP_ALL_CLIENTS", "BGW_AUTORESTORE_ON_ANY_DIFF", "BGW_INSECURE_TLS"]
)
def test_a_bad_boolean_fails_the_command_as_a_usage_error(tmp_env, monkeypatch, capsys, name):
    monkeypatch.setenv(name, "maybe")
    assert cli.main(["dump", "--host", "router.local"]) == 1
    assert f"{name} must be 1, true, yes, on or 0, false, no, off; got 'maybe'." in capsys.readouterr().err


def test_a_flag_still_wins_over_a_false_variable(tmp_env, monkeypatch):
    monkeypatch.setenv("BGW_JSON", "false")
    assert cli.parse_args(["check", "--json"]).options.json is True


def test_help_shows_the_boolean_spellings():
    text = cli.help_text()
    assert "BGW_JSON=true" in text and "1/0, true/false, yes/no or on/off" in text
    assert "BGW_JSON=1" not in text
