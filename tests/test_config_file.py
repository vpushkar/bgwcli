"""The configuration file: format, location, precedence, the secret permission gate and the new keys."""

from __future__ import annotations

import io
import os
import stat

import pytest
from test_cli import fake, run, run_json  # noqa: F401 - fixtures and helpers shared with the CLI suite

from bgwcli import cli
from bgwcli.config import (
    CONFIG_KEYS,
    SECRET_KEYS,
    default_config_path,
    load_config_file,
    parse_config_text,
)
from bgwcli.errors import UsageError


def write(path, text: str, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


# ---------------------------------------------------------------------------------------------
# parser


def test_parse_accepts_quoted_bare_and_commented_lines():
    text = (
        "# gateway\n"
        "\n"
        "BGW_HOST='192.168.1.254'\n"
        'BGW_TIMEOUT="30"\n'
        "BGW_LIMIT=50\n"
        "  BGW_JSON = '1'  \n"
    )
    assert parse_config_text(text, "f") == {
        "BGW_HOST": "192.168.1.254", "BGW_TIMEOUT": "30", "BGW_LIMIT": "50", "BGW_JSON": "1",
    }


def test_parse_keeps_quoted_text_literally_and_lets_a_later_line_win():
    text = "BGW_ACCESS_CODE='a #b;$c\\d'\nBGW_HOST='one'\nBGW_HOST='two'\n"
    assert parse_config_text(text, "f") == {"BGW_ACCESS_CODE": "a #b;$c\\d", "BGW_HOST": "two"}


@pytest.mark.parametrize(
    ("line", "fragment"),
    [
        ("BGW_HSOT='x'", "unknown setting BGW_HSOT"),
        ("BGW_CONFIG='x'", "unknown setting BGW_CONFIG"),
        ("just text", "expected KEY='value'"),
        ("=value", "expected KEY='value'"),
        ("BGW_HOST='open", "unterminated or doubly quoted"),
        ("BGW_HOST='it''s'", "unterminated or doubly quoted"),
        ("BGW_HOST=two words", "needs quotes"),
        ("BGW_HOST=a#b", "needs quotes"),
        ("BGW_HOST=$HOME", "needs quotes"),
    ],
)
def test_parse_rejects_bad_lines_naming_the_line(line, fragment):
    with pytest.raises(UsageError) as info:
        parse_config_text("# ok\n" + line + "\n", "/tmp/cfg")
    assert "/tmp/cfg:2" in str(info.value)
    assert fragment in str(info.value)


def test_every_documented_key_is_an_environment_name_the_cli_reads():
    assert SECRET_KEYS <= CONFIG_KEYS
    assert "BGW_CONFIG" not in CONFIG_KEYS
    assert "BGW_INCLUDE_SECRETS" not in CONFIG_KEYS  # redaction is a per-run decision


# ---------------------------------------------------------------------------------------------
# loader


def test_default_path_follows_xdg_config_home_then_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "x"))
    assert default_config_path() == str(tmp_path / "x" / "bgw" / "config")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    assert default_config_path() == str(tmp_path / "h" / ".config" / "bgw" / "config")


def test_missing_default_file_is_no_file_and_missing_named_file_is_a_usage_error(tmp_path):
    env: dict[str, str] = {}
    assert load_config_file(environ=env) == {}
    assert env == {}
    with pytest.raises(UsageError, match="--config names .* which does not exist"):
        load_config_file(str(tmp_path / "absent"), environ=env)
    env = {"BGW_CONFIG": str(tmp_path / "absent")}
    with pytest.raises(UsageError, match="BGW_CONFIG names .* which does not exist"):
        load_config_file(environ=env)


def test_file_fills_only_unset_variables(tmp_path):
    cfg = write(tmp_path / "cfg", "BGW_HOST='file.local'\nBGW_TIMEOUT='30'\nBGW_LIMIT='5'\n")
    env = {"BGW_TIMEOUT": "7"}
    applied = load_config_file(str(cfg), environ=env)
    assert applied == {"BGW_HOST": "file.local", "BGW_LIMIT": "5"}
    assert env == {"BGW_TIMEOUT": "7", "BGW_HOST": "file.local", "BGW_LIMIT": "5"}


def test_explicit_path_wins_over_bgw_config_which_wins_over_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    write(tmp_path / "xdg" / "bgw" / "config", "BGW_HOST='default.local'\n")
    named = write(tmp_path / "named", "BGW_HOST='named.local'\n")
    flagged = write(tmp_path / "flagged", "BGW_HOST='flag.local'\n")
    env: dict[str, str] = {}
    load_config_file(environ=env)
    assert env["BGW_HOST"] == "default.local"
    env = {"BGW_CONFIG": str(named)}
    load_config_file(environ=env)
    assert env["BGW_HOST"] == "named.local"
    env = {"BGW_CONFIG": str(named)}
    load_config_file(str(flagged), environ=env)
    assert env["BGW_HOST"] == "flag.local"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_secrets_are_skipped_with_one_warning_when_others_can_read_the_file(tmp_path, mode):
    cfg = write(tmp_path / "cfg", "BGW_ACCESS_CODE='s'\nBGW_FALLBACK_ACCESS_CODE='f'\nBGW_HOST='h'\n", mode)
    warnings: list[str] = []
    env: dict[str, str] = {}
    applied = load_config_file(str(cfg), environ=env, warn=warnings.append)
    assert applied == {"BGW_HOST": "h"}
    assert "BGW_ACCESS_CODE" not in env and "BGW_FALLBACK_ACCESS_CODE" not in env
    assert len(warnings) == 1
    assert warnings[0].startswith(f"Ignoring BGW_ACCESS_CODE and BGW_FALLBACK_ACCESS_CODE in {cfg}")
    assert f"chmod 600 {cfg}" in warnings[0]
    assert "s" not in warnings[0].split("'")  # the code itself is never printed


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_secrets_are_applied_from_an_owner_only_file(tmp_path, mode):
    cfg = write(tmp_path / "cfg", "BGW_ACCESS_CODE='s3cret'\n", mode)
    env: dict[str, str] = {}
    warnings: list[str] = []
    assert load_config_file(str(cfg), environ=env, warn=warnings.append) == {"BGW_ACCESS_CODE": "s3cret"}
    assert warnings == []


def test_a_readable_file_without_secrets_needs_no_special_mode(tmp_path):
    cfg = write(tmp_path / "cfg", "BGW_HOST='h'\n", 0o644)
    warnings: list[str] = []
    env: dict[str, str] = {}
    assert load_config_file(str(cfg), environ=env, warn=warnings.append) == {"BGW_HOST": "h"}
    assert warnings == []


def test_a_directory_or_undecodable_file_is_a_usage_error(tmp_path):
    (tmp_path / "dir").mkdir()
    with pytest.raises(UsageError, match="Cannot read the configuration file"):
        load_config_file(str(tmp_path / "dir"), environ={})
    binary = tmp_path / "bin"
    binary.write_bytes(b"BGW_HOST='\xff\xfe'\n")
    with pytest.raises(UsageError, match="not UTF-8 text"):
        load_config_file(str(binary), environ={})


def test_a_bad_value_in_the_file_gets_the_same_message_as_the_variable(tmp_path, monkeypatch, capsys, fake):  # noqa: F811
    cfg = write(tmp_path / "cfg", "BGW_TIMEOUT='soon'\n")
    code, _, err = run(capsys, ["check", "--config", str(cfg)])
    assert code == 1
    assert "BGW_TIMEOUT must be a finite number greater than or equal to 0.001." in err


# ---------------------------------------------------------------------------------------------
# through the CLI


def test_cli_precedence_is_flag_then_environment_then_file(tmp_path, monkeypatch, capsys, fake):  # noqa: F811
    cfg = write(
        tmp_path / "cfg",
        "BGW_HOST='file.local'\nBGW_TIMEOUT='30'\nBGW_WAIT_FOR_SESSION='1'\nBGW_SESSION_WAIT_TIMEOUT='5'\n",
    )
    monkeypatch.setenv("BGW_TIMEOUT", "7")
    run(capsys, ["check", "--config", str(cfg), "--session-wait-timeout", "9"])
    opts = fake["options"]
    assert opts.host == "file.local"  # file: nothing else set it
    assert opts.timeout_ms == 7000 and opts.timeout_explicit is True  # environment beats file
    assert opts.wait_for_session is True
    assert opts.session_wait_timeout_ms == 9000  # flag beats file; seconds in, milliseconds kept


def test_cli_reads_the_default_location_and_bgw_config(tmp_path, monkeypatch, capsys, fake):  # noqa: F811
    write(tmp_path / "xdg-config" / "bgw" / "config", "BGW_HOST='default.local'\n")
    run(capsys, ["check"])
    assert fake["options"].host == "default.local"
    monkeypatch.delenv("BGW_HOST")  # the first run filled it; a second process would start clean
    named = write(tmp_path / "named", "BGW_HOST='named.local'\n")
    monkeypatch.setenv("BGW_CONFIG", str(named))
    run(capsys, ["check"])
    assert fake["options"].host == "named.local"


def test_cli_access_code_comes_from_a_private_file_and_a_flag_still_wins(tmp_path, monkeypatch, capsys, fake):  # noqa: F811
    monkeypatch.delenv("BGW_ACCESS_CODE")
    cfg = write(tmp_path / "cfg", "BGW_ACCESS_CODE='from-file'\n")
    code, _, err = run(capsys, ["check", "--config", str(cfg)])
    assert (code, err) == (0, "")
    assert fake["access_code"] == "from-file"
    monkeypatch.setattr("sys.stdin", io.StringIO("piped\n"))
    run(capsys, ["check", "--config", str(cfg), "--access-code-stdin"])
    assert fake["access_code"] == "piped"


def test_cli_warns_and_ignores_a_world_readable_access_code(tmp_path, monkeypatch, capsys, fake):  # noqa: F811
    monkeypatch.delenv("BGW_ACCESS_CODE")
    cfg = write(tmp_path / "cfg", "BGW_ACCESS_CODE='from-file'\nBGW_HOST='h.local'\n", 0o644)
    code, _, err = run(capsys, ["check", "--config", str(cfg)])
    assert code == 0  # check needs no access code; the host from the same file was applied
    assert fake["access_code"] is None
    assert fake["options"].host == "h.local"
    assert err.count("Ignoring BGW_ACCESS_CODE in") == 1
    assert "from-file" not in err


def test_cli_missing_named_file_is_exit_1_before_the_router_is_touched(tmp_path, capsys, fake):  # noqa: F811
    code, _, err = run(capsys, ["check", "--config", str(tmp_path / "absent")])
    assert code == 1
    assert "does not exist" in err
    assert "options" not in fake


def test_help_never_reads_the_file(tmp_path, capsys, fake):  # noqa: F811
    write(tmp_path / "xdg-config" / "bgw" / "config", "BGW_HSOT='typo'\n")
    code, out, err = run(capsys, ["help"])
    assert (code, err) == (0, "")
    assert "Configuration file:" in out
    code, _, err = run(capsys, ["check"])
    assert code == 1 and "unknown setting BGW_HSOT" in err


def test_an_empty_host_in_the_file_is_the_usual_empty_host_error(tmp_path, capsys, fake):  # noqa: F811
    cfg = write(tmp_path / "cfg", "BGW_HOST=''\n")
    code, _, err = run(capsys, ["check", "--config", str(cfg)])
    assert code == 1
    assert "BGW_HOST is empty" in err


# ---------------------------------------------------------------------------------------------
# the preference keys


def test_json_preference_selects_json_including_for_errors(tmp_path, monkeypatch, capsys, fake):  # noqa: F811
    cfg = write(tmp_path / "cfg", "BGW_JSON='1'\n")
    code, payload, _ = run_json(capsys, ["tabs", "--config", str(cfg)])
    assert code == 0 and isinstance(payload, (dict, list))
    monkeypatch.setenv("BGW_JSON", "1")
    code, payload, _ = run_json(capsys, ["tabs"])
    assert code == 0
    monkeypatch.setenv("BGW_JSON", "0")
    code, out, _ = run(capsys, ["tabs"])
    assert code == 0 and not out.lstrip().startswith(("{", "["))


def test_numeric_and_flag_preferences_fill_the_command_and_flags_still_win(tmp_path, monkeypatch, tmp_env):
    cfg = write(
        tmp_path / "cfg",
        "BGW_LIMIT='7'\nBGW_DELAY='0.01'\nBGW_DUMP_ALL_CLIENTS='1'\nBGW_DUMP_INCLUDE='firewall-advanced, lan'\n"
        "BGW_AUTORESTORE_MAX_PASSES='5'\nBGW_AUTORESTORE_WAIT='1'\nBGW_AUTORESTORE_ON_ANY_DIFF='1'\n",
    )
    env: dict[str, str] = {}
    load_config_file(str(cfg), environ=env)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    command = cli.parse_args(["dump"])
    assert (command.limit, command.delay_ms, command.all_clients) == (7, 10, True)
    assert command.include == ["firewall-advanced", "lan"]
    assert (command.max_passes, command.wait_seconds, command.on_any_diff) == (5, 1, True)
    flagged = cli.parse_args(["dump", "--limit", "2", "--delay", "3", "--include", "lan", "--max-passes", "1",
                              "--wait", "0"])
    assert (flagged.limit, flagged.delay_ms, flagged.include, flagged.max_passes, flagged.wait_seconds) == (
        2, 3000, ["lan"], 1, 0,
    )


def test_dump_include_preference_applies_to_dump_only(monkeypatch, tmp_env):
    monkeypatch.setenv("BGW_DUMP_INCLUDE", "lan")
    assert cli.parse_args(["dump"]).include == ["lan"]
    assert cli.parse_args(["diff", "x.json"]).include is None
    assert cli.parse_args(["restore", "x.json"]).include is None
    monkeypatch.setenv("BGW_DUMP_INCLUDE", " , ")
    with pytest.raises(UsageError, match="BGW_DUMP_INCLUDE needs at least one page id"):
        cli.parse_args(["dump"])


@pytest.mark.parametrize(
    ("name", "value", "minimum"),
    [("BGW_LIMIT", "-1", 0), ("BGW_DELAY", "abc", 0), ("BGW_AUTORESTORE_MAX_PASSES", "0", 1),
     ("BGW_AUTORESTORE_WAIT", "inf", 0)],
)
def test_numeric_preferences_are_validated_like_the_other_variables(monkeypatch, tmp_env, name, value, minimum):
    monkeypatch.setenv(name, value)
    with pytest.raises(UsageError, match=f"{name} must be a finite number greater than or equal to {minimum}."):
        cli.parse_args(["dump"])


def test_fractional_preferences_truncate_like_their_flags(monkeypatch, tmp_env):
    monkeypatch.setenv("BGW_LIMIT", "7.9")
    assert cli.parse_args(["devices"]).limit == 7


def test_the_example_env_file_parses_as_a_configuration_file():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "deploy", "autorestore.env.example"), encoding="utf-8") as handle:
        text = handle.read()
    values = parse_config_text(text, "autorestore.env.example")
    assert set(values) == {"BGW_ACCESS_CODE", "BGW_FALLBACK_ACCESS_CODE"}


def test_mode_constant_sanity():
    assert stat.S_IRWXG | stat.S_IRWXO == 0o077
