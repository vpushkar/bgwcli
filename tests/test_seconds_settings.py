"""Every user-facing duration is given in seconds (fractions allowed); the code keeps milliseconds.

Covers the duration variables, the three duration flags besides --timeout, exact truncation at the
boundary, and the help text."""

from __future__ import annotations

import pytest

from bgwcli import cli, config, session
from bgwcli.config import env_seconds_ms, parse_config_text, seconds_to_ms
from bgwcli.errors import UsageError

DURATION_VARIABLES = (
    "BGW_TIMEOUT", "BGW_SESSION_WAIT_TIMEOUT", "BGW_SESSION_WAIT_INTERVAL", "BGW_SESSION_CACHE_TTL",
    "BGW_SESSION_POOL_COOLDOWN", "BGW_SESSION_LOCK_TIMEOUT", "BGW_SESSION_LOCK_STALE", "BGW_DELAY",
)

# ---------------------------------------------------------------------------------------------
# the conversion


@pytest.mark.parametrize(
    ("text", "milliseconds"),
    [("15", 15000), ("1.5", 1500), ("0.75", 750), ("1.0019", 1001), ("0.001", 1), ("1e-3", 1), ("1e3", 1000000),
     (" 2 ", 2000), ("0", 0), ("0.0004", 0)],
)
def test_seconds_to_ms_truncates_exactly(text, milliseconds):
    assert seconds_to_ms(text) == milliseconds


def test_env_seconds_ms_falls_back_when_unset_or_empty(tmp_env, monkeypatch):
    assert env_seconds_ms("BGW_DELAY", 1, 0) == 1000
    assert env_seconds_ms("BGW_DELAY", 0.75, 0) == 750
    monkeypatch.setenv("BGW_DELAY", "")
    assert env_seconds_ms("BGW_DELAY", 1, 0) == 1000
    monkeypatch.setenv("BGW_DELAY", "0.1")
    assert env_seconds_ms("BGW_DELAY", 1, 0) == 100


@pytest.mark.parametrize("bad", ["abc", "NaN", "Infinity", "-1", "-0.5"])
def test_env_seconds_ms_uses_the_shared_message_with_the_minimum_in_seconds(tmp_env, monkeypatch, bad):
    monkeypatch.setenv("BGW_SESSION_WAIT_INTERVAL", bad)
    expected = r"BGW_SESSION_WAIT_INTERVAL must be a finite number greater than or equal to 0\.001\."
    with pytest.raises(UsageError, match=expected):
        config.env_default_options()


# ---------------------------------------------------------------------------------------------
# the variables


def test_duration_variables_default_to_the_documented_seconds(tmp_env):
    opts = config.env_default_options()
    assert (opts.timeout_ms, opts.timeout_explicit) == (15000, False)
    assert (opts.session_wait_timeout_ms, opts.session_wait_interval_ms) == (120000, 10000)
    assert (opts.session_cache_ttl_ms, opts.session_pool_cooldown_ms, opts.session_lock_timeout_ms) == (
        120000, 300000, 300000,
    )
    assert session._lock_stale_ms() == 900000.0
    assert cli.parse_args(["audit"]).delay_ms == 1000


def test_duration_variables_are_read_in_seconds_and_kept_in_milliseconds(tmp_env, monkeypatch):
    for name, value in [
        ("BGW_TIMEOUT", "1.5"), ("BGW_SESSION_WAIT_TIMEOUT", "0.5"), ("BGW_SESSION_WAIT_INTERVAL", "0.25"),
        ("BGW_SESSION_CACHE_TTL", "60"), ("BGW_SESSION_POOL_COOLDOWN", "0"), ("BGW_SESSION_LOCK_TIMEOUT", "2"),
        ("BGW_SESSION_LOCK_STALE", "0.9"), ("BGW_DELAY", "0.1"),
    ]:
        monkeypatch.setenv(name, value)
    opts = config.env_default_options()
    assert (opts.timeout_ms, opts.timeout_explicit) == (1500, True)
    assert (opts.session_wait_timeout_ms, opts.session_wait_interval_ms) == (500, 250)
    assert (opts.session_cache_ttl_ms, opts.session_pool_cooldown_ms, opts.session_lock_timeout_ms) == (
        60000, 0, 2000,
    )
    assert session._lock_stale_ms() == 900.0
    assert cli.parse_args(["audit"]).delay_ms == 100


def test_bgw_timeout_minimum_is_one_millisecond(tmp_env, monkeypatch):
    monkeypatch.setenv("BGW_TIMEOUT", "0.001")
    assert config.env_default_options().timeout_ms == 1
    monkeypatch.setenv("BGW_TIMEOUT", "0.0009")
    with pytest.raises(UsageError, match=r"BGW_TIMEOUT must be a finite number greater than or equal to 0\.001\."):
        config.env_default_options()


# ---------------------------------------------------------------------------------------------
# the flags


@pytest.mark.parametrize(
    ("argv", "attribute", "milliseconds"),
    [
        (["audit", "--delay", "0.5"], "delay_ms", 500),
        (["audit", "--delay", "0"], "delay_ms", 0),
        (["audit", "--delay", "2.0019"], "delay_ms", 2001),
    ],
)
def test_delay_flag_takes_seconds(tmp_env, argv, attribute, milliseconds):
    assert getattr(cli.parse_args(argv), attribute) == milliseconds


def test_session_wait_flags_take_seconds(tmp_env):
    options = cli.parse_args(["check", "--session-wait-timeout", "1.5", "--session-wait-interval", "0.01"]).options
    assert (options.session_wait_timeout_ms, options.session_wait_interval_ms) == (1500, 10)
    options = cli.parse_args(["check", "--session-wait-timeout", "0", "--session-wait-interval", "0.001"]).options
    assert (options.session_wait_timeout_ms, options.session_wait_interval_ms) == (0, 1)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["audit", "--delay", "abc"], "--delay must be a finite number of seconds greater than or equal to 0."),
        (["audit", "--delay", "-1"], "--delay must be a finite number of seconds greater than or equal to 0."),
        (["check", "--session-wait-timeout", "nan"],
         "--session-wait-timeout must be a finite number of seconds greater than or equal to 0."),
        (["check", "--session-wait-interval", "0"],
         "--session-wait-interval must be a finite number of seconds greater than or equal to 0.001."),
    ],
)
def test_invalid_duration_flags_are_usage_errors_naming_seconds(tmp_env, capsys, argv, message):
    with pytest.raises(UsageError) as info:
        cli.parse_args(argv)
    assert str(info.value) == message
    assert cli.main(argv) == 1
    assert message in capsys.readouterr().err


def test_flag_in_seconds_beats_variable_in_seconds(tmp_env, monkeypatch):
    monkeypatch.setenv("BGW_DELAY", "5")
    assert cli.parse_args(["audit", "--delay", "0.25"]).delay_ms == 250


# ---------------------------------------------------------------------------------------------
# no millisecond setting exists


def test_every_duration_variable_is_a_config_key_and_no_millisecond_name_is():
    assert set(DURATION_VARIABLES) <= config.CONFIG_KEYS
    assert not any(key.endswith("_MS") for key in config.CONFIG_KEYS)


def test_a_millisecond_name_in_the_configuration_file_is_an_unknown_setting():
    with pytest.raises(UsageError, match=r"cfg:2: unknown setting BGW_DELAY_MS"):
        parse_config_text("BGW_HOST='h'\nBGW_DELAY_MS='750'\n", "cfg")


def test_a_millisecond_name_in_the_environment_is_not_read(tmp_env, monkeypatch):
    monkeypatch.setenv("BGW_TIMEOUT_MS", "1")
    monkeypatch.setenv("BGW_DELAY_MS", "1")
    assert config.env_default_options().timeout_ms == 15000
    assert cli.parse_args(["audit"]).delay_ms == 1000


# ---------------------------------------------------------------------------------------------
# what the user sees


def test_help_speaks_only_in_seconds():
    text = cli.help_text()
    assert "<ms>" not in text and "milliseconds" not in text and "_MS" not in text
    for stale_default in ("Default: 750", "Default: 120000", "Default: 10000"):
        assert stale_default not in text, stale_default
    assert "--delay <seconds>" in text and "Default: 1\n" in text
    assert "--session-wait-timeout <seconds>" in text and "Default: 120" in text
    assert "--session-wait-interval <seconds>" in text and "Default: 10" in text
    for name in DURATION_VARIABLES:
        assert name in text, name
