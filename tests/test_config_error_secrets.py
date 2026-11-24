"""Configuration and host errors never echo a secret.

Review finding: a shell-style `export BGW_ACCESS_CODE='...'` line in the config file was quoted whole
in the usage error, and a host given as https://user:pass@host echoed the password.
"""

import json

import pytest

from bgwcli import cli
from bgwcli.client import normalize_origin
from bgwcli.config import parse_config_text
from bgwcli.errors import UsageError

MARKER = "S3cretMarker42"


@pytest.mark.parametrize(
    "line",
    [
        f"export BGW_ACCESS_CODE='{MARKER}'",
        f"BGW ACCESS CODE={MARKER}",
        f"{MARKER}",
        f"BGW_ACCESS_CODE {MARKER}",
    ],
)
def test_a_malformed_config_line_is_never_quoted(line):
    with pytest.raises(UsageError) as caught:
        parse_config_text(f"BGW_HOST='192.168.1.254'\n{line}\n", "/home/u/.config/bgw/config")
    message = str(caught.value)
    assert MARKER not in message
    assert "/home/u/.config/bgw/config:2" in message


def test_an_export_prefix_gets_a_hint():
    with pytest.raises(UsageError) as caught:
        parse_config_text(f"export BGW_ACCESS_CODE='{MARKER}'\n", "cfg")
    assert "export" in str(caught.value) and MARKER not in str(caught.value)


@pytest.mark.parametrize(
    "host",
    [
        f"https://admin:{MARKER}@192.168.1.254",
        f"admin:{MARKER}@192.168.1.254",
        f"https://admin:{MARKER}@192.168.1.254/path",
    ],
)
def test_a_host_error_never_shows_credentials(host):
    with pytest.raises(UsageError) as caught:
        normalize_origin(host)
    assert MARKER not in str(caught.value)


def test_the_cli_json_error_for_a_bad_config_line_carries_no_secret(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config"
    config.write_text(f"export BGW_ACCESS_CODE='{MARKER}'\n")
    config.chmod(0o600)
    for name in ("BGW_HOST", "ROUTER_IP", "BGW_ACCESS_CODE", "BGW_FALLBACK_ACCESS_CODE", "BGW_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    code = cli.main(["--config", str(config), "check", "--json"])
    captured = capsys.readouterr()
    assert code == 1
    assert MARKER not in captured.out and MARKER not in captured.err
    assert json.loads(captured.out)["ok"] is False
