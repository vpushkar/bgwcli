import pytest

from bgwcli import config, exit_codes, redact
from bgwcli.client import RouterResponseError
from bgwcli.errors import BgwError, DumpFileError, RouterAuthError, SessionLockTimeoutError, UsageError
from bgwcli.types import ParsedField, ParsedPage, to_json_dict


def test_redact_by_sensitive_name():
    assert redact.redact_value("wpa_key", "secret", False) == "[redacted]"
    assert redact.redact_value("wpa_key", "secret", True) == "secret"
    assert redact.redact_value("maxclients", "80", False) == "80"
    assert redact.redact_value("wpa_key", "", False) == ""


def test_exit_codes():
    assert exit_codes.fatal_exit_code(DumpFileError("x")) == 2
    assert exit_codes.fatal_exit_code(RouterAuthError("x")) == 2
    # An HTTP error status or an oversized body is "no answer", not a negative answer.
    assert exit_codes.fatal_exit_code(RouterResponseError("x", status_code=500)) == 2
    # Unexpected faults (bugs, OS errors, unknown BgwError subclasses) are "no answer" too.
    assert exit_codes.fatal_exit_code(ValueError("x")) == 2
    assert exit_codes.fatal_exit_code(OSError("x")) == 2
    assert exit_codes.fatal_exit_code(BgwError("x")) == 2
    # Only bad usage and lock contention timeouts stay exit 1.
    assert exit_codes.fatal_exit_code(UsageError("x")) == 1
    assert exit_codes.fatal_exit_code(SessionLockTimeoutError("x")) == 1


def test_env_defaults(tmp_env, monkeypatch):
    opts = config.env_default_options()
    assert opts.host == "192.168.1.254" and opts.insecure_tls is True and opts.timeout_ms == 15000
    monkeypatch.setenv("ROUTER_IP", "10.0.0.1")
    monkeypatch.setenv("BGW_INSECURE_TLS", "0")
    monkeypatch.setenv("BGW_TIMEOUT_MS", "2500")
    opts = config.env_default_options()
    assert opts.host == "10.0.0.1" and opts.insecure_tls is False and opts.timeout_ms == 2500
    monkeypatch.setenv("BGW_TIMEOUT_MS", "0")
    with pytest.raises(UsageError):
        config.env_default_options()


def test_access_code_resolution(tmp_env, monkeypatch):
    import io
    assert config.resolve_access_code(config.GlobalOptions(access_code="a")) == "a"
    monkeypatch.setenv("BGW_ACCESS_CODE", "b")
    assert config.resolve_access_code(config.GlobalOptions()) == "b"
    monkeypatch.delenv("BGW_ACCESS_CODE")
    assert config.resolve_access_code(config.GlobalOptions()) is None
    assert config.resolve_access_code(config.GlobalOptions(access_code_stdin=True), stdin=io.StringIO("c\n")) == "c"
    # TS trimEnd(): every trailing whitespace character goes, leading whitespace stays
    stdin_opts = config.GlobalOptions(access_code_stdin=True)
    assert config.resolve_access_code(stdin_opts, stdin=io.StringIO("code \t\r\n")) == "code"
    assert config.resolve_access_code(stdin_opts, stdin=io.StringIO("  c \n")) == "  c"


def test_env_number_keeps_fractional_values_like_js_number(tmp_env, monkeypatch):
    monkeypatch.setenv("BGW_TIMEOUT_MS", "1500.5")
    assert config.env_number("BGW_TIMEOUT_MS", 15000, 1) == 1500.5
    monkeypatch.setenv("BGW_TIMEOUT_MS", "1500")
    value = config.env_number("BGW_TIMEOUT_MS", 15000, 1)
    assert value == 1500 and isinstance(value, int)
    monkeypatch.setenv("BGW_TIMEOUT_MS", "1e3")
    assert config.env_number("BGW_TIMEOUT_MS", 15000, 1) == 1000
    monkeypatch.setenv("BGW_TIMEOUT_MS", " 250 ")
    assert config.env_number("BGW_TIMEOUT_MS", 15000, 1) == 250
    for bad in ("0.5", "-1", "abc", "Infinity", "nan"):
        monkeypatch.setenv("BGW_TIMEOUT_MS", bad)
        with pytest.raises(UsageError, match="BGW_TIMEOUT_MS must be a finite number greater than or equal to 1."):
            config.env_number("BGW_TIMEOUT_MS", 15000, 1)


def test_json_dict_camel_case_drops_none():
    page = ParsedPage(page="p", title="t", heading="h", fields=[ParsedField("n", "text", "v", False, False)])
    d = to_json_dict(page)
    assert d["fields"][0] == {"name": "n", "type": "text", "value": "v", "checked": False, "sensitive": False}
    assert "valueEntries" not in d


def test_timeout_is_explicit_only_when_the_user_set_it(tmp_env, monkeypatch):
    assert config.env_default_options().timeout_explicit is False
    monkeypatch.setenv("BGW_TIMEOUT_MS", "20000")
    assert config.env_default_options().timeout_explicit is True


def test_package_version_has_a_single_source():
    """bgwcli.__version__ is the only version literal; the user agents and the build metadata derive from it."""
    import re
    from pathlib import Path

    import bgwcli
    from bgwcli import cli, client

    root = Path(__file__).resolve().parent.parent
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    # Plain-text checks of the few asserted keys so the test also runs on 3.10 (no tomllib, no deps).
    tables = dict(re.findall(r"(?ms)^\[([^\]\n]+)\]\n(.*?)(?=^\[|\Z)", pyproject))
    assert not re.search(r"(?m)^version\s*=", tables["project"])
    assert re.search(r'(?m)^dynamic\s*=\s*\[[^\]]*"version"', tables["project"])
    assert re.search(
        r'(?m)^version\s*=\s*\{\s*attr\s*=\s*"bgwcli\.__version__"\s*\}', tables["tool.setuptools.dynamic"]
    )

    expected_agent = f"bgw/{bgwcli.__version__}"
    assert {client.DEFAULT_USER_AGENT, cli.USER_AGENT} == {expected_agent}
    literal = re.compile(r"[\"']\d+\.\d+\.\d+[\"']")
    for module in ("client.py", "cli.py"):
        source = (root / "src" / "bgwcli" / module).read_text(encoding="utf-8")
        assert not literal.search(source), f"{module} carries its own version literal"


def test_bgw_timeout_ms_is_capped_at_one_hour_like_the_timeout_flag(tmp_env, monkeypatch):
    monkeypatch.setenv("BGW_TIMEOUT_MS", "3600000")
    assert config.env_default_options().timeout_ms == 3600000
    for too_large in ("3600001", "1e9", "1e16"):
        monkeypatch.setenv("BGW_TIMEOUT_MS", too_large)
        with pytest.raises(UsageError, match=r"BGW_TIMEOUT_MS is in milliseconds and must be at most 3600000"):
            config.env_default_options()



def test_explicit_access_code_stdin_wins_over_the_environment(tmp_env, monkeypatch):
    import io

    monkeypatch.setenv("BGW_ACCESS_CODE", "from-env")
    stdin_opts = config.GlobalOptions(access_code_stdin=True)
    assert config.resolve_access_code(stdin_opts, stdin=io.StringIO("from-stdin\n")) == "from-stdin"
    assert config.resolve_access_code(config.GlobalOptions(), stdin=io.StringIO("unused")) == "from-env"


@pytest.fixture
def leaked_environment(monkeypatch):
    """Variables a developer's shell (or the autorestore EnvironmentFile) may export."""
    monkeypatch.setenv("BGW_FALLBACK_ACCESS_CODE", "sticker-from-shell")


def test_tmp_env_isolates_every_access_code_variable(leaked_environment, tmp_env):
    import os

    assert "BGW_FALLBACK_ACCESS_CODE" not in os.environ
    assert "BGW_ACCESS_CODE" not in os.environ
