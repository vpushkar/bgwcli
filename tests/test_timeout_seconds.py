"""The request-timeout CLI uses seconds; environment and client APIs retain milliseconds."""

from decimal import localcontext

import pytest

from bgwcli import cli
from bgwcli.client import BGW320Client, RawResponse
from bgwcli.errors import UsageError


@pytest.mark.parametrize(
    ("seconds", "milliseconds"),
    [("45", 45000), ("15", 15000), ("1.5", 1500), ("1.001", 1001), ("1.0019", 1001), ("0.001", 1), ("1e-3", 1)],
)
@pytest.mark.parametrize("before_command", [False, True])
def test_timeout_seconds_are_converted_at_the_cli_boundary(tmp_env, seconds, milliseconds, before_command):
    argv = ["--timeout", seconds, "check"] if before_command else ["check", "--timeout", seconds]
    options = cli.parse_args(argv).options
    assert options.timeout_ms == milliseconds
    assert options.timeout_explicit is True


@pytest.mark.parametrize("precision", [2, 28])
@pytest.mark.parametrize(
    ("seconds", "milliseconds"),
    [
        ("1.000999999999999999999999999999999", 1000),
        ("0.001999999999999999999999999999999", 1),
        ("1.001", 1001),
    ],
)
def test_timeout_truncates_exactly_independently_of_decimal_context(tmp_env, seconds, milliseconds, precision):
    with localcontext() as context:
        context.prec = precision
        options = cli.parse_args(["check", "--timeout", seconds]).options
    assert options.timeout_ms == milliseconds


@pytest.mark.parametrize("value", ["abc", "", "NaN", "Infinity", "-Infinity", "-1", "0", "0.0009", "1e308"])
def test_invalid_seconds_fail_as_usage_errors_before_client_creation(tmp_env, monkeypatch, capsys, value):
    def unexpected_client(*args, **kwargs):
        pytest.fail("invalid timeout reached the client")

    monkeypatch.setattr(cli, "_client_factory", unexpected_client)
    assert cli.main(["check", f"--timeout={value}"]) == 1
    assert "--timeout" in capsys.readouterr().err
    with pytest.raises(UsageError):
        cli.parse_args(["check", f"--timeout={value}"])


def test_timeout_default_and_millisecond_environment_stay_compatible(tmp_env, monkeypatch):
    options = cli.parse_args(["check"]).options
    assert options.timeout_ms == 15000
    assert options.timeout_explicit is False

    monkeypatch.setenv("BGW_TIMEOUT_MS", "1500")
    options = cli.parse_args(["check"]).options
    assert options.timeout_ms == 1500
    assert options.timeout_explicit is True

    options = cli.parse_args(["check", "--timeout", "45"]).options
    assert options.timeout_ms == 45000
    assert options.timeout_explicit is True


@pytest.mark.parametrize(
    ("argv", "environment_ms", "expected"),
    [
        ([], None, [45000, 45000, 15000]),
        (["--timeout", "1.5"], None, [1500, 1500, 1500]),
        ([], "1500.5", [1500.5, 1500.5, 1500.5]),
        (["--timeout", "1.5"], "45000", [1500, 1500, 1500]),
    ],
)
def test_cli_timeout_controls_request_deadlines_and_explicit_overrides_disable_floors(
    tmp_env, monkeypatch, argv, environment_ms, expected
):
    if environment_ms is not None:
        monkeypatch.setenv("BGW_TIMEOUT_MS", environment_ms)
    options = cli.parse_args(["check", *argv]).options
    seen = []

    def transport(request):
        seen.append(request.timeout_ms)
        return RawResponse(status=200, reason="OK", headers=[], body=b"<title>Status</title>")

    client = BGW320Client.from_options(options, None, transport=transport)
    for page in ["home", "lanstatistics", "sysinfo"]:
        client.get_cgi_page(page, auth=False)
    assert seen == expected
