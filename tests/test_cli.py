"""CLI tests re-derived from BGW320-CLI tests/cli.test.ts, driving bgwcli.cli.main() with a fake client.

The real gateway is never touched: `cli._client_factory` is replaced with a factory returning
FakeClient (HTML fixtures inline), and dump/diff/restore tests swap `cli.fetch_parsed_page` for a
page-builder backed fetcher.
"""

from __future__ import annotations

import io
import json
import re

import pytest
from page_builders import (
    APPHOSTING_HEADERS,
    IPALLOC_HEADERS,
    SERVICES_HEADERS,
    apphosting_page,
    button,
    dosprotect_page,
    field,
    hidden,
    page,
    select,
    services_page,
    sysinfo_page,
)

from bgwcli import cli
from bgwcli.client import session_pool_full_error
from bgwcli.errors import RouterAuthError, RouterConnectionError
from bgwcli.fetch import ParsedPageResult
from bgwcli.types import HttpResponse

DIAG_HTML = """<html><head><title>Troubleshoot</title></head><body><h1>Diagnostics</h1>
<form method="post" action="/cgi-bin/diag.ha"><input type="hidden" name="nonce" value="abc123">
<label>Web Address</label><input type="text" name="WebAddress" value="">
<select name="protopref"><option value="IPv4" selected>IPv4</option><option value="IPv6">IPv6</option></select>
<input type="submit" name="Ping" value="Ping"><input type="submit" name="Trace" value="Traceroute">
<input type="submit" name="Nslookup" value="Nslookup">
<textarea name="ProgressWindow">PING example.com: 3 packets</textarea></form>
<table><tr><td>Status</td><td>Up</td></tr></table></body></html>"""

WIFI_HTML = """<html><head><title>Wi-Fi</title></head><body><h1>Wi-Fi</h1>
<form method="post" action="/cgi-bin/wconfig_unified.ha"><input type="hidden" name="nonce" value="n1">
<input type="text" name="ssid" value="MyNet"><input type="password" name="wpa_key" value="hunter2">
<input type="submit" name="Save" value="Save"></form>
<table><tr><td>Radio</td><td>On</td></tr></table></body></html>"""

LOGS_HTML = """<html><head><title>Logs</title></head><body><table>
<tr><th>ID</th><th>Time</th><th>Source</th><th>Destination</th><th>Protocol</th><th>Reason</th></tr>
<tr><td>1</td><td>2026-09-20 10:00</td><td>1.1.1.1</td><td>2.2.2.2</td><td>TCP</td><td>drop</td></tr>
<tr><td>2</td><td>2026-09-20 10:01</td><td>3.3.3.3</td><td>4.4.4.4</td><td>UDP</td><td>drop</td></tr>
</table></body></html>"""

SITEMAP_HTML = """<html><head><title>Site Map</title></head><body>
<a href="/cgi-bin/home.ha">Status</a><a href="/cgi-bin/diag.ha">Troubleshoot</a>
<a href="/cgi-bin/mystery.ha">Mystery</a></body></html>"""

DEVICES_HTML = """<html><head><title>Device List</title></head><body><table>
<tr><th>Status</th><th>IPv4 Address / Name</th><th>IPv6</th><th>MAC Address</th><th>Connection</th></tr>
<tr><td>on</td><td>192.168.1.64 / host-b</td><td></td><td>aa:bb:cc:dd:ee:01</td><td>Ethernet</td></tr>
</table></body></html>"""

RESTART_HTML = """<html><head><title>Restart Device</title></head><body>
<form method="post" action="/cgi-bin/restart.ha"><input type="hidden" name="nonce" value="n2">
<input type="submit" name="Restart" value="Restart"></form></body></html>"""

PAGES = {
    "diag": DIAG_HTML,
    "wconfig_unified": WIFI_HTML,
    "logs": LOGS_HTML,
    "sitemap": SITEMAP_HTML,
    "devices": DEVICES_HTML,
    "restart": RESTART_HTML,
}


SERVICES_TABLE_HEADER_ONLY = (
    '<html><title>Custom Services</title><div id="error-message-text">Changes saved</div>'
    "<table><tr><th>Service Name</th><th>Global Port Range</th><th>Base Host Port</th><th>Protocol</th></tr></table>"
    "</html>"
)


class FakeClient:
    def __init__(self, pages=None, *, post_status=200, post_location=None, post_body=DIAG_HTML):
        self.pages = dict(PAGES if pages is None else pages)
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.form_nonce_pages: list[str] = []
        self.logged_in = False
        self.post_status = post_status
        self.post_location = post_location
        self.post_body = post_body
        self.origin = "https://router.local"

    # -- BGW320Client surface used by cli/session ---------------------------------------------
    def check(self):
        return {"host": "router.local", "reachable": True, "title": "Site Map", "authenticated": False}

    def login(self, initial_login_html=None, *, force=False):
        self.logged_in = True
        self.login_forced = force

    def get_cgi_page(self, page, *, auth=True):
        self.gets.append(page)
        body = self.pages.get(page)
        if body is None:
            raise RouterConnectionError(f"{page}.ha timed out")
        return HttpResponse(200, "OK", {}, body, f"{self.origin}/cgi-bin/{page}.ha")

    def post_cgi_page(self, page, fields):
        self.posts.append((page, dict(fields)))
        headers = {"location": self.post_location} if self.post_location else {}
        body = self.post_body(page) if callable(self.post_body) else self.post_body
        return HttpResponse(self.post_status, "OK", headers, body, f"{self.origin}/cgi-bin/{page}.ha")

    def post_form(self, nonce_page, post_path, fields):
        # Reached by the Wi-Fi Warning's Continue: nonce read from `nonce_page`, POST to its action.
        self.form_nonce_pages.append(nonce_page)
        return self.post_cgi_page(post_path.removesuffix(".ha"), fields)

    def session_identity(self):
        return self.origin

    def has_authenticated_session(self):
        return False

    def export_session(self):  # pragma: no cover - never authenticated
        raise AssertionError("unused")

    def import_session(self, snapshot):
        pass

    def clear_session(self):
        pass


@pytest.fixture
def fake(monkeypatch, tmp_env):
    """Install a FakeClient factory; returns a holder exposing the client and the factory call args."""
    holder = {}

    def factory(options, access_code, *, on_session_wait=None, user_agent="bgw/0.1.0"):
        holder["options"] = options
        holder["access_code"] = access_code
        holder["on_session_wait"] = on_session_wait
        holder["user_agent"] = user_agent
        holder.setdefault("client", FakeClient())
        return holder["client"]

    monkeypatch.setattr(cli, "_client_factory", factory)
    monkeypatch.setenv("BGW_ACCESS_CODE", "unused")
    return holder


def run(capsys, argv):
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(capsys, argv):
    code, out, err = run(capsys, argv)
    return code, json.loads(out), err


# ---------------------------------------------------------------------------------------------
# help / parsing


@pytest.mark.parametrize("argv", [
    ["page", "diag", "--time", "5"],
    ["--time", "5", "page", "diag"],
    ["set", "etherlan", "setting=new", "--comm", "--confirm", "ETHERLAN"],
    ["action", "run-speed-test", "--commit", "--conf", "SPEED"],
])
def test_abbreviated_long_options_are_rejected(capsys, fake, argv):
    code, out, err = run(capsys, argv)
    assert code == 1
    # Before the command name argparse cannot tell the unknown flag's value from the command, so the
    # leading form is rejected as an invalid command instead; either way nothing runs.
    assert "unrecognized arguments" in err or "invalid choice" in err
    assert out == ""
    assert "client" not in fake, "nothing may run on an abbreviated option"


ALL_COMMANDS = [
    "check", "auth", "sitemap", "coverage", "tabs", "actions", "session", "action", "sweep", "scan", "schema",
    "audit", "readiness", "section", "device", "broadband", "home-network", "voice", "firewall", "diagnostics",
    "page", "inspect", "devices", "wifi", "nat", "logs", "set", "submit", "status", "dump", "diff", "restore",
    "autorestore",
]


@pytest.mark.parametrize("argv", [["--help"], ["-h"], ["help"], []])
def test_help_explains_operation_safety_and_high_value_commands(capsys, argv):
    code, out, err = run(capsys, argv)
    assert code == 0
    for needle in [
        "Auth:", "Most-used read commands:", "bgwcli device status", "advanced-wi-fi",
        "bgwcli voice status | line-details | call-statistics", "bgwcli sweep", "bgwcli audit",
        "bgwcli session status | clear-cache", "--include-parsed", "--pages <csv>", "--out <dir>",
        "--wait-for-session", "Operations are dry-run by default:", "Diagnostics operations:",
        "--access-code-stdin", "Fallbacks are intentionally narrow", "dump [--out <file>]", "diff <dumpfile>",
        "restore <dumpfile>", "--confirm RESTORE", "--prune", "--include <csv|all>", "dump --include all",
        "reservations are never released",
    ]:
        assert needle in out, needle
    # The only --include* options are the page selector and the two pre-existing global flags.
    assert set(re.findall(r"--include(?:-\w+)?", out)) == {"--include", "--include-secrets", "--include-parsed"}
    assert "bgw " not in out.replace("bgwcli ", "")


def test_help_lists_every_command(capsys):
    _, out, _ = run(capsys, ["--help"])
    commands_line = next(line for line in out.splitlines() if line.startswith("Commands: "))
    listed = {name.strip() for name in commands_line.removeprefix("Commands: ").split(",")}
    assert set(ALL_COMMANDS) <= listed
    assert set(ALL_COMMANDS) <= set(cli.COMMAND_NAMES)


def test_subcommand_help_is_argparse_generated(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["set", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--commit" in out and "--confirm" in out


def test_unknown_command_exits_1_with_message(capsys, fake):
    code, out, err = run(capsys, ["frobnicate"])
    assert code == 1
    assert "frobnicate" in err
    assert out == ""


def test_invalid_numeric_options_fail_before_router_access(capsys, fake):
    code, _, err = run(capsys, ["sweep", "--delay", "-1"])
    assert code == 1
    assert "--delay must be a finite number greater than or equal to 0." in err
    assert "client" not in fake

    code, _, err = run(capsys, ["check", "--timeout", "abc"])
    assert code == 1
    assert "--timeout must be a finite number of seconds greater than or equal to 0.001" in err


def test_timeout_above_one_hour_is_rejected_with_a_unit_migration_hint(capsys, fake):
    # Before the seconds migration --timeout took milliseconds; an unmigrated `--timeout 15000`
    # would otherwise be accepted as 15000 s and hang for hours against a dead gateway.
    code, _, err = run(capsys, ["check", "--timeout", "15000"])
    assert code == 1
    assert "--timeout is in seconds and must be at most 3600" in err
    assert "15000" in err and "--timeout 15" in err
    assert "client" not in fake

    code, _, err = run(capsys, ["check", "--timeout", "3600.001"])
    assert code == 1
    assert "--timeout is in seconds and must be at most 3600" in err

    assert cli._timeout_milliseconds("3600") == 3600000
    assert cli._timeout_milliseconds("1.5") == 1500


def test_missing_option_value_exits_1(capsys, fake):
    code, _, err = run(capsys, ["check", "--host"])
    assert code == 1
    assert "--host" in err


def test_global_options_accepted_before_and_after_subcommand(capsys, fake):
    code, payload, _ = run_json(capsys, ["--json", "check"])
    assert code == 0 and payload["reachable"] is True
    code, payload, _ = run_json(capsys, ["check", "--json"])
    assert code == 0 and payload["reachable"] is True
    code, payload, _ = run_json(capsys, ["--host", "http://r.local", "check", "--json"])
    assert fake["options"].host == "http://r.local"


def test_env_defaults_are_overridden_by_flags(capsys, fake, monkeypatch):
    monkeypatch.setenv("BGW_HOST", "http://env.local")
    monkeypatch.setenv("BGW_TIMEOUT_MS", "5000")
    run(capsys, ["check"])
    assert fake["options"].host == "http://env.local"
    assert fake["options"].timeout_ms == 5000
    assert fake["options"].insecure_tls is True
    run(capsys, ["check", "--host", "http://flag.local", "--timeout", "7", "--strict-tls", "--wait-for-session",
                 "--session-wait-timeout", "1000", "--session-wait-interval", "100"])
    opts = fake["options"]
    assert (opts.host, opts.timeout_ms, opts.insecure_tls) == ("http://flag.local", 7000, False)
    assert (opts.wait_for_session, opts.session_wait_timeout_ms, opts.session_wait_interval_ms) == (True, 1000, 100)


def test_access_code_from_stdin_reads_all_of_stdin(capsys, fake, monkeypatch):
    monkeypatch.delenv("BGW_ACCESS_CODE")
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret code\n"))
    code, _, _ = run(capsys, ["check", "--access-code-stdin"])
    assert code == 0
    assert fake["access_code"] == "s3cret code"


def test_json_mode_does_not_configure_session_wait_progress(capsys, fake):
    run(capsys, ["check"])
    assert callable(fake["on_session_wait"])
    run(capsys, ["check", "--json"])
    assert fake["on_session_wait"] is None


def test_session_wait_progress_goes_to_stderr(capsys, fake):
    run(capsys, ["check"])
    fake["on_session_wait"]({"waitedMs": 0, "retryCount": 0, "timeoutMs": 120000, "intervalMs": 10000})
    err = capsys.readouterr().err
    assert err == "Router web session pool is full; waiting 10000ms before retry 1 of approximately 12.\n"


# ---------------------------------------------------------------------------------------------
# read commands


def test_check_text_and_json(capsys, fake):
    code, out, _ = run(capsys, ["check"])
    assert code == 0
    assert "Host" in out and "router.local" in out and "Reachable" in out and "yes" in out
    code, payload, _ = run_json(capsys, ["check", "--json"])
    assert set(payload) >= {"host", "reachable", "title"}


def test_auth_logs_in(capsys, fake):
    code, out, _ = run(capsys, ["auth"])
    assert code == 0 and out == "authenticated\n"
    assert fake["client"].logged_in is True and fake["client"].login_forced is True
    code, payload, _ = run_json(capsys, ["auth", "--json"])
    assert payload == {"authenticated": True}


def test_sitemap_and_coverage(capsys, fake):
    code, payload, _ = run_json(capsys, ["sitemap", "--json"])
    assert code == 0
    assert {entry["page"] for entry in payload} == {"home", "diag", "mystery"}
    code, payload, _ = run_json(capsys, ["coverage", "--json"])
    assert code == 0
    assert set(payload) == {"mappedCount", "liveCount", "missingFromCli", "notInLiveSitemap"}
    assert payload["missingFromCli"] == ["mystery"]
    assert payload["liveCount"] == 3
    code, out, _ = run(capsys, ["coverage"])
    assert "Missing from CLI" in out and "mystery" in out


def test_tabs_section_and_actions_are_local(capsys, fake, tmp_env):
    code, payload, _ = run_json(capsys, ["tabs", "--json"])
    assert code == 0 and any(tab["page"] == "wconfig_unified" for tab in payload)
    code, out, _ = run(capsys, ["tabs"])
    assert "wconfig_unified" in out
    code, payload, _ = run_json(capsys, ["section", "Voice", "--json"])
    assert code == 0 and {tab["page"] for tab in payload} == {"voice", "voiceconfig", "voicestat"}
    code, _, err = run(capsys, ["section", "Nope"])
    assert code == 1 and "Unknown section: Nope" in err
    code, _, err = run(capsys, ["section"])
    assert code == 1 and "Missing section name." in err
    code, payload, _ = run_json(capsys, ["actions", "--json"])
    assert code == 0 and any(a["name"] == "run-speed-test" and a["confirmToken"] == "SPEED" for a in payload)
    code, out, _ = run(capsys, ["actions"])
    assert "run-speed-test" in out and "confirm=SPEED" in out
    # none of these touched the router, and the local-only commands did not create a session cache dir
    assert "client" not in fake or fake["client"].gets == []
    assert not (tmp_env / "cache").exists()


def test_session_status_and_clear_cache_are_local(capsys, fake, tmp_env):
    code, out, _ = run(capsys, ["session", "status", "--host", "http://router.local"])
    assert code == 0 and "Cached session" in out and "no" in out
    code, payload, _ = run_json(capsys, ["session", "--json"])
    assert code == 0 and payload == {"cached": False}
    code, out, _ = run(capsys, ["session", "clear-cache"])
    assert code == 0 and out == "session cache cleared\n"
    code, payload, _ = run_json(capsys, ["session", "clear-cache", "--json"])
    assert payload == {"ok": True, "cleared": True}
    code, _, err = run(capsys, ["session", "bogus"])
    assert code == 1 and "Unknown session command: bogus" in err
    assert "client" not in fake or fake["client"].gets == []


def test_page_and_inspect_parse_a_router_page(capsys, fake):
    code, payload, _ = run_json(capsys, ["page", "diag", "--json"])
    assert code == 0
    assert {"page", "title", "values", "tables", "fields", "buttons", "forms", "summary"} <= set(payload)
    assert payload["page"] == "diag"
    code, out, _ = run(capsys, ["inspect", "Diagnostics/Troubleshoot"])
    assert code == 0 and "WebAddress" in out
    code, out, _ = run(capsys, ["page", "diag", "--raw"])
    assert code == 0 and out.startswith("<html>") and "ProgressWindow" in out
    code, _, err = run(capsys, ["page"])
    assert code == 1 and "Missing page name." in err


def test_page_unavailable_exits_2(capsys, fake):
    code, out, _ = run(capsys, ["page", "nosuchpage"])
    assert code == 2
    assert "Page unavailable: nosuchpage" in out and "timed out" in out
    code, payload, _ = run_json(capsys, ["page", "nosuchpage", "--json"])
    assert code == 2 and payload["ok"] is False and payload["page"] == "nosuchpage"


def test_page_level_403_is_a_page_fetch_failure_not_an_abort(capsys, fake):
    """A single page answering 403 is reported like any unavailable page (exit 2, structured
    result); only login failures and login-page bounces abort the command."""
    def forbidden(page, **kwargs):
        error = RouterAuthError(f"Router rejected https://router.local/cgi-bin/{page}.ha with HTTP 403.")
        error.status_code = 403
        error.url = f"https://router.local/cgi-bin/{page}.ha"
        error.page_level = True
        raise error

    fake["client"] = FakeClient()
    fake["client"].get_cgi_page = forbidden
    # `page logs` reads through _fetch_raw_page; `page diag` through fetch_parsed_page.
    code, out, _ = run(capsys, ["page", "logs"])
    assert code == 2 and "Page unavailable: logs" in out and "HTTP 403" in out
    code, payload, _ = run_json(capsys, ["page", "diag", "--json"])
    assert code == 2 and payload["ok"] is False and payload["page"] == "diag" and "HTTP 403" in payload["error"]


def test_wifi_redacts_secrets_unless_asked(capsys, fake):
    code, payload, _ = run_json(capsys, ["wifi", "--json"])
    assert code == 0
    key = next(f for f in payload["fields"] if f["name"] == "wpa_key")
    assert key["value"] == "[redacted]"
    code, payload, _ = run_json(capsys, ["wifi", "--json", "--include-secrets"])
    key = next(f for f in payload["fields"] if f["name"] == "wpa_key")
    assert key["value"] == "hunter2"


def test_section_commands_resolve_tabs(capsys, fake):
    code, payload, _ = run_json(capsys, ["diagnostics", "troubleshoot", "--json"])
    assert code == 0 and payload["page"] == "diag"
    code, payload, _ = run_json(capsys, ["diagnostics", "--json"])
    assert code == 0 and payload["page"] == "diag"
    code, payload, _ = run_json(capsys, ["home-network", "wi-fi", "--json"])
    assert code == 0 and payload["page"] == "wconfig_unified"
    code, payload, _ = run_json(capsys, ["home", "wi-fi", "--json"])
    assert code == 0 and payload["page"] == "wconfig_unified"
    code, payload, _ = run_json(capsys, ["diagnostics", "logs", "--json"])
    assert code == 0 and payload[0]["id"] == "1"
    code, _, err = run(capsys, ["diagnostics", "bogus"])
    assert code == 1 and "Unknown diagnostics command: bogus" in err
    code, _, err = run(capsys, ["voice", "bogus"])
    assert code == 1 and err == "Unknown command: voice\nRun bgwcli help.\n"
    code, _, err = run(capsys, ["device", "nope"])
    assert code == 1 and err == "Unknown command: device\nRun bgwcli help.\n"


def test_snapshot_meta_timestamp_is_computed_from_a_single_clock_read(monkeypatch):
    from datetime import datetime as real_datetime

    class Clock:
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            return real_datetime(2026, 9, 20, 12, 0, 59, 999_000, tzinfo=tz)

    monkeypatch.setattr(cli, "datetime", Clock)
    meta = cli._snapshot_meta(cli.Command(name="dump", args=[], options=cli.env_default_options()))
    assert meta["ts"] == "2026-09-20T12:00:59.999Z"
    assert Clock.calls == 1


def test_logs_honours_limit_and_reports_fetch_failure(capsys, fake):
    # --limit shapes the text view only: --json always carries every entry.
    code, payload, _ = run_json(capsys, ["logs", "--json", "--limit", "1"])
    assert code == 0 and len(payload) == 2
    code, out, _ = run(capsys, ["logs", "--limit", "1"])
    assert code == 0 and "1.1.1.1" in out and "3.3.3.3" not in out
    assert "... 1 more rows. Use --limit 2 to show all." in out
    assert "Entries".ljust(12) + "  2" in out
    code, out, _ = run(capsys, ["logs"])
    assert code == 0 and "1.1.1.1" in out and "3.3.3.3" in out and "more rows" not in out
    fake["client"].pages.pop("logs")
    code, payload, _ = run_json(capsys, ["logs", "--json"])
    assert code == 2 and payload["ok"] is False and payload["page"] == "logs"


def test_logs_page_not_found_answered_with_http_200_is_a_structured_failure(capsys, fake):
    fake["client"] = FakeClient()
    fake["client"].pages["logs"] = "<html><head><title>Page not found</title></head><body></body></html>"
    code, payload, _ = run_json(capsys, ["logs", "--json"])
    assert code == 2 and payload["ok"] is False and payload["page"] == "logs"
    assert "Page not found" in payload["error"]


def test_devices_and_nat(capsys, fake):
    code, payload, _ = run_json(capsys, ["devices", "--json", "--all"])
    assert code == 0 and payload["devices"][0]["mac"] == "aa:bb:cc:dd:ee:01"
    code, out, _ = run(capsys, ["devices"])
    assert code == 0 and "aa:bb:cc:dd:ee:01" in out
    code, payload, _ = run_json(capsys, ["nat", "--json"])
    assert code == 2 and payload["page"] == "nattable"


def test_devices_exits_2_when_neither_devices_nor_ipalloc_could_be_read(capsys, fake):
    fake["client"] = FakeClient()
    fake["client"].pages.pop("devices")  # devices.ha times out; ipalloc is not served either
    code, payload, _ = run_json(capsys, ["devices", "--json"])
    assert code == 2
    assert payload["fallback"] is True and payload["devices"] == []
    assert payload["error"] == "devices.ha timed out" and "ipalloc" in payload["fallbackError"]
    code, out, _ = run(capsys, ["devices"])
    assert code == 2 and "IP Allocation fallback unavailable" in out


def test_devices_fallback_with_zero_devices_exits_0(capsys, fake):
    fake["client"] = FakeClient()
    fake["client"].pages.pop("devices")
    fake["client"].pages["ipalloc"] = (
        "<html><title>IP Allocation</title><body><table><tr><th>Status</th><th>IPv4 Address / Name</th>"
        "<th>MAC Address</th><th>Allocation</th></tr></table></body></html>"
    )
    code, payload, _ = run_json(capsys, ["devices", "--json"])
    assert code == 0 and payload["fallback"] is True and payload["devices"] == []
    assert "fallbackError" not in payload


def test_devices_fallback_on_a_blank_ipalloc_page_is_no_answer(capsys, fake):
    fake["client"] = FakeClient()
    fake["client"].pages.pop("devices")
    fake["client"].pages["ipalloc"] = "<html><title>IP Allocation</title><body><table></table></body></html>"
    code, payload, _ = run_json(capsys, ["devices", "--json"])
    assert code == 2 and payload["fallback"] is True and payload["devices"] == []
    assert "IP Allocation table" in payload["fallbackError"]


def test_status_sections(capsys, fake):
    sysinfo = ("<html><title>System Information</title><body><table>"
               "<tr><td>Software Version</td><td>4.27.7</td></tr></table></body></html>")
    fake["client"] = FakeClient({"sysinfo": sysinfo})
    code, payload, _ = run_json(capsys, ["status", "--json"])
    assert code == 0
    assert [section["page"] for section in payload] == ["sysinfo", "broadbandstatistics", "fiberstat", "firewall"]
    assert payload[0]["ok"] is True and payload[1]["ok"] is False
    code, out, _ = run(capsys, ["status"])
    assert "System Information" in out and "4.27.7" in out


# ---------------------------------------------------------------------------------------------
# sweep / audit


def test_sweep_refuses_raw_html_for_a_full_sweep(capsys, fake):
    code, _, err = run(capsys, ["sweep", "--raw"])
    assert code == 1 and "Refusing to emit raw HTML for a full sweep" in err
    assert "client" not in fake or fake["client"].gets == []


def test_sweep_json_is_compact_and_progress_goes_to_stderr(capsys, fake):
    code, payload, err = run_json(capsys, ["sweep", "--pages", "logs,diag", "--delay", "0", "--json"])
    assert code == 0
    # router-tab order (Diagnostics: Troubleshoot before Logs), not the order given on the command line
    assert [p["page"] for p in payload] == ["diag", "logs"]
    assert all("parsed" not in p and "rawHtml" not in p for p in payload)
    assert err == ""
    code, out, err = run(capsys, ["sweep", "--pages", "diag", "--delay", "0"])
    assert code == 0 and "diag" in out
    assert "sweep 1/1 diag start" in err and "sweep 1/1 diag ok" in err


def test_sweep_include_parsed_and_raw_single_page(capsys, fake):
    code, payload, _ = run_json(capsys, ["sweep", "--pages", "diag", "--include-parsed", "--delay", "0", "--json"])
    assert code == 0 and payload[0]["parsed"]["page"] == "diag"
    code, payload, _ = run_json(capsys, ["schema", "--pages", "diag", "--delay", "0", "--json"])
    assert code == 0 and "parsed" in payload[0] and "controls" in payload[0]
    code, out, _ = run(capsys, ["sweep", "--raw", "--pages", "diag", "--delay", "0"])
    assert code == 0 and out.startswith("<html>")


def test_sweep_raw_single_page_that_fails_names_the_error_on_stderr(capsys, fake):
    fake["client"] = FakeClient()
    fake["client"].pages.pop("diag")
    code, out, err = run(capsys, ["sweep", "--raw", "--pages", "diag", "--delay", "0"])
    assert code == 2 and out == ""
    assert "diag" in err and "error" in err.lower()


@pytest.mark.parametrize("argv", [
    ["page", "diag"], ["audit"], ["readiness"], ["logs"], ["devices"], ["diff", "d.json"], ["status"],
])
def test_out_is_a_usage_error_for_commands_that_write_no_file(capsys, fake, tmp_path, argv):
    out_dir = tmp_path / "artifacts"
    code, _, err = run(capsys, [*argv, "--out", str(out_dir)])
    assert code == 1 and "--out" in err
    assert not out_dir.exists()
    assert "client" not in fake or fake["client"].gets == []


def test_sweep_out_dir_writes_artifacts(capsys, fake, tmp_path):
    out_dir = tmp_path / "artifacts"
    code, payload, _ = run_json(capsys, ["sweep", "--pages", "diag", "--out", str(out_dir), "--delay", "0", "--json"])
    assert code == 0
    assert (out_dir / "router-html" / "diag.html").exists()
    assert (out_dir / "parsed" / "diag.json").exists()
    assert payload[0]["artifacts"]["html"].endswith("diag.html")


def test_unknown_sweep_page_exits_1(capsys, fake):
    code, _, err = run(capsys, ["scan", "--pages", "nope", "--delay", "0"])
    assert code == 1 and "Unknown sweep page(s): nope" in err


def test_sweep_pool_full_exits_2(capsys, fake, monkeypatch):
    def raise_pool_full(*args, **kwargs):
        raise session_pool_full_error(waited_ms=42, retry_count=3)

    fake["client"] = FakeClient()
    fake["client"].get_cgi_page = raise_pool_full
    code, payload, _ = run_json(capsys, ["sweep", "--pages", "diag", "--delay", "0", "--json"])
    assert code == 2
    assert payload == {
        "ok": False, "page": "login", "error": "Router web session pool is full.", "sessionPoolFull": True,
        "waitedMs": 42, "retryCount": 3, "exitCode": 2,
    }
    # the first failure started a local pool cooldown, so the second run fails fast (still exit 2)
    code, out, err = run(capsys, ["sweep", "--pages", "diag", "--delay", "0"])
    assert code == 2 and out == "" and err.startswith("Router web session pool is full")


def test_status_pool_full_exits_2_and_records_cooldown(capsys, fake, monkeypatch, tmp_env):
    """status routes through _fetch_soft; pool-full must propagate (TS status.ts:187), not become a section error."""
    def raise_pool_full(*args, **kwargs):
        raise session_pool_full_error(waited_ms=7, retry_count=2)

    fake["client"] = FakeClient()
    fake["client"].get_cgi_page = raise_pool_full
    code, payload, _ = run_json(capsys, ["status", "--json"])
    assert code == 2
    assert payload == {
        "ok": False, "page": "login", "error": "Router web session pool is full.", "sessionPoolFull": True,
        "waitedMs": 7, "retryCount": 2, "exitCode": 2,
    }
    cooldowns = list((tmp_env / "cache").glob("*.cooldown.json"))
    assert len(cooldowns) == 1
    cooldown = json.loads(cooldowns[0].read_text())
    assert cooldown["waitedMs"] == 7 and cooldown["retryCount"] == 2
    # device status (home page) goes through the same soft fetcher
    code, out, err = run(capsys, ["device", "status"])
    assert code == 2 and out == "" and err.startswith("Router web session pool is full")


@pytest.mark.parametrize("error", [
    RouterAuthError("Login failed. Check the device access code."),
    RouterConnectionError("Timed out connecting to https://router.local/cgi-bin/diag.ha"),
])
def test_json_mode_prints_a_structured_error_for_a_fatal_error(capsys, fake, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(cli, "run", fail)
    code, payload, err = run_json(capsys, ["page", "diag", "--json"])
    assert code == 2
    assert payload == {"ok": False, "error": str(error), "exitCode": 2, "errorType": type(error).__name__}
    assert err == f"{error}\n"


def test_json_mode_prints_a_structured_error_for_a_usage_error(capsys, fake):
    code, payload, err = run_json(capsys, ["action", "no-such-action", "--json"])
    assert code == 1
    assert payload == {
        "ok": False, "error": "Unknown action: no-such-action", "exitCode": 1, "errorType": "UsageError",
    }
    assert err == "Unknown action: no-such-action\n"


def test_text_mode_fatal_error_stays_on_stderr_only(capsys, fake):
    code, out, err = run(capsys, ["action", "no-such-action"])
    assert code == 1 and out == "" and err == "Unknown action: no-such-action\n"


def test_audit_and_readiness(capsys, fake):
    code, payload, _ = run_json(capsys, ["audit", "--pages", "diag,logs,nattable", "--delay", "0", "--json"])
    assert code == 0
    assert {"totalPages", "okPages", "failedPages", "fallbackPages", "usefulPages", "emptyPages", "dangerousPages",
            "pages"} == set(payload)
    assert payload["totalPages"] == 3 and payload["failedPages"] == 1
    assert all("parsed" not in p for p in payload["pages"])
    code, out, _ = run(capsys, ["readiness", "--pages", "diag", "--delay", "0"])
    assert code == 0 and "Total pages" in out


# ---------------------------------------------------------------------------------------------
# guarded operations


def test_action_dry_run_and_commit(capsys, fake):
    fake.setdefault("client", FakeClient()).pages["speed"] = (
        '<html><body><form method="post" action="/cgi-bin/speed.ha"><input type="hidden" name="nonce" value="n3">'
        '<input type="submit" name="run" value="Run Speed Test"></form></body></html>'
    )
    code, payload, _ = run_json(capsys, ["action", "run-speed-test", "--json"])
    assert code == 0
    assert payload["dryRun"] is True and payload["committed"] is False
    assert payload["confirmation"] == "SPEED"
    assert payload["commitCommand"] == "action run-speed-test --commit --confirm SPEED"
    assert {"operation", "page", "guarded", "dangerous", "payload"} <= set(payload)
    assert fake["client"].posts == []

    code, _, err = run(capsys, ["action", "run-speed-test", "--commit"])
    assert code == 1 and "Refusing to run action 'run-speed-test'. Re-run with --commit --confirm SPEED." in err
    assert fake["client"].posts == []

    code, _, err = run(capsys, ["action", "nope"])
    assert code == 1 and "Unknown action: nope" in err
    code, _, err = run(capsys, ["action"])
    assert code == 1 and "Missing action name." in err

    code, payload, _ = run_json(capsys, ["action", "run-speed-test", "--commit", "--confirm", "SPEED", "--json"])
    assert code == 0
    assert payload["committed"] is True and payload["statusCode"] == 200
    assert "location" in payload and payload["location"] is None
    assert fake["client"].posts[0][0] == "speed"


def test_action_text_output(capsys, fake):
    code, out, _ = run(capsys, ["action", "restart"])
    assert code == 0
    assert "dry-run: no router action was sent" in out and "RESTART" in out


def test_diagnostic_commit_requires_confirmation_before_router_access(capsys, fake):
    code, _, err = run(capsys, ["diagnostics", "ping", "example.com", "--commit"])
    assert code == 1 and "Refusing to run diagnostic 'ping'. Re-run with --commit --confirm DIAG." in err
    assert "client" not in fake or fake["client"].gets == []
    code, _, err = run(capsys, ["diagnostics", "ping"])
    assert code == 1 and "Missing target for diagnostics ping." in err


def test_diagnostic_dry_run_and_commit(capsys, fake):
    code, payload, _ = run_json(capsys, ["diagnostics", "ping", "example.com", "--ipv6", "--json"])
    assert code == 0
    assert payload["operation"] == "diagnostic" and payload["dryRun"] is True
    assert payload["payload"]["WebAddress"] == "example.com" and payload["payload"]["protopref"] == "IPv6"
    assert payload["confirmation"] == "DIAG"
    assert fake["client"].posts == []

    code, payload, _ = run_json(capsys, ["diagnostics", "traceroute", "example.com", "--commit", "--confirm", "DIAG",
                                         "--json"])
    assert code == 0
    assert payload["committed"] is True and payload["result"] == "PING example.com: 3 packets"
    assert payload["pageResult"]["page"] == "diag"
    page, fields = fake["client"].posts[0]
    assert page == "diag" and fields["WebAddress"] == "example.com" and fields["Trace"] == "Traceroute"


def test_diagnostic_page_fetch_failure_exits_2(capsys, fake):
    fake["client"] = FakeClient({})
    code, payload, _ = run_json(capsys, ["diagnostics", "ping", "example.com", "--json"])
    assert code == 2 and payload["ok"] is False and payload["page"] == "diag"


def test_set_dry_run_commit_and_refusals(capsys, fake):
    code, _, err = run(capsys, ["set", "diag", "WebAddress=x", "--commit"])
    assert code == 1 and "Refusing to commit changes to 'diag'. Re-run with --commit --confirm DIAG." in err
    assert "client" not in fake or fake["client"].gets == []

    code, _, err = run(capsys, ["set", "restart", "x=y", "--commit", "--confirm", "RESTART"])
    assert code == 1 and "Refusing to mutate dangerous page 'restart'" in err

    code, _, err = run(capsys, ["set", "diag"])
    assert code == 1 and "Missing KEY=VALUE assignment." in err
    code, _, err = run(capsys, ["set"])
    assert code == 1 and "Missing page name." in err

    code, payload, _ = run_json(capsys, ["set", "diag", "WebAddress=example.com", "--json"])
    assert code == 0
    assert payload["operation"] == "set" and payload["dryRun"] is True
    assert payload["changes"] == {"WebAddress": "example.com"}
    assert payload["commitCommand"].endswith("--commit --confirm DIAG")
    assert fake["client"].posts == []

    code, payload, _ = run_json(capsys, ["set", "diag", "WebAddress=example.com", "--commit", "--confirm", "DIAG",
                                         "--json"])
    # diag has no Save button: the POST is sent, but the re-read shows the field unchanged, so the
    # command reports verified=false and exits 1 instead of claiming success.
    assert code == 1 and payload["committed"] is True and payload["location"] is None
    assert payload["verified"] is False and "no Save button" in payload["warning"]
    assert fake["client"].posts[0][1]["WebAddress"] == "example.com"


def test_set_dry_run_on_dangerous_page_is_blocked(capsys, fake):
    code, _, err = run(capsys, ["set", "restart", "x=y"])
    assert code == 1 and "restart" in err
    assert fake["client"].posts == []


def test_submit_dry_run_commit_and_refusals(capsys, fake):
    code, _, err = run(capsys, ["submit", "diag", "Ping", "--commit"])
    assert code == 1 and "Refusing to submit 'Ping' on 'diag'. Re-run with --commit --confirm DIAG." in err
    assert "client" not in fake or fake["client"].gets == []

    code, _, err = run(capsys, ["submit", "restart", "Restart", "--commit", "--confirm", "RESTART"])
    assert code == 1 and "Refusing generic submit on dangerous page 'restart'" in err

    code, _, err = run(capsys, ["submit", "diag"])
    assert code == 1 and "Missing button name." in err

    code, payload, _ = run_json(capsys, ["submit", "diag", "Ping", "WebAddress=example.com", "--json"])
    assert code == 0 and payload["operation"] == "submit" and payload["button"] == "Ping"
    assert payload["payload"]["Ping"] == "Ping" and payload["dryRun"] is True

    code, _, err = run(capsys, ["submit", "diag", "NoSuchButton"])
    assert code == 1 and "NoSuchButton" in err

    fake["client"].post_location = "/cgi-bin/diag.ha"
    code, payload, _ = run_json(capsys, ["submit", "diag", "Ping", "--commit", "--confirm", "DIAG", "--json"])
    assert code == 0 and payload["committed"] is True and payload["location"] == "/cgi-bin/diag.ha"
    assert fake["client"].posts[0][1]["Ping"] == "Ping"


# ---------------------------------------------------------------------------------------------
# dump / diff / restore (page-builder backed fetcher)


def live_pages():
    pages = {
        "sysinfo": sysinfo_page(),
        "services": services_page(),
        "apphosting": apphosting_page(),
        "dosprotect": dosprotect_page(selects=[select("flood_protect", [("on", "On"), ("off", "Off")])]),
        # Form pages added 2026-09-20; minimal shapes with a Save button so dump/diff/restore fetch them.
        "dhcpserver": page("dhcpserver", title="Subnets & DHCP", selects=[select("dhcp", ["off", "on"], selected="on")], buttons=[button("Save", "Save")]),
        "ippass": page("ippass", title="IP Passthrough", selects=[select("allocmode", ["normal", "passthrough"], selected="normal")], buttons=[button("Save", "Save")]),
        "wmacauth": page("wmacauth", title="Wi-Fi MAC Filtering", selects=[select("wmacr1user", ["allow", "deny", "none"], selected="none")], buttons=[button("Save", "Save")]),
    }
    for name in ("ipalloc", "packetfilter", "wconfig", "wconfig_unified", "etherlan"):
        # A form page needs a real control besides the hidden nonce: a page showing only the nonce (and
        # a page without any control) is an unreadable answer, not an empty form.
        pages[name] = page(name, title=name, fields=[hidden("nonce", "abc"), field("note", "text", "")])
    return pages


@pytest.fixture
def snapshot_fetcher(monkeypatch, fake):
    holder = {"pages": live_pages()}

    holder["fetched"] = []

    def fetch(client, page, include_secrets=False):
        holder["fetched"].append(page)
        parsed = holder["pages"].get(page)
        if parsed is None:
            return ParsedPageResult(page, False, error=f"{page}.ha timed out")
        if isinstance(client, cli._BodyRecorder) and page in TABLE_HEADER_HTML:
            # This fake stands in for the GET: the page showed its table header (an empty section is
            # read as empty only when the header row was seen).
            client.body = TABLE_HEADER_HTML[page]
        return ParsedPageResult(page, True, 200, parsed)

    monkeypatch.setattr(cli, "fetch_parsed_page", fetch)
    return holder


TABLE_HEADER_HTML = {
    page: "<table><tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr></table>"
    for page, headers in (
        ("services", SERVICES_HEADERS), ("apphosting", APPHOSTING_HEADERS), ("ipalloc", IPALLOC_HEADERS),
    )
}


def test_dump_writes_owner_only_file_and_summary(capsys, fake, snapshot_fetcher, tmp_path):
    out = tmp_path / "dump.json"
    code, payload, _ = run_json(capsys, ["dump", "--out", str(out), "--json"])
    assert code == 0
    assert set(payload) == {"path", "meta", "services", "forwards", "reservations", "forms", "tables"}
    assert payload["path"] == str(out) and payload["services"] == 2 and payload["forwards"] == 2
    assert payload["meta"]["schema"] == 2 and payload["meta"]["firmware"] == "4.27.7"
    assert out.stat().st_mode & 0o777 == 0o600
    assert json.loads(out.read_text())["meta"]["schema"] == 2

    code, text, _ = run(capsys, ["dump"])
    assert code == 0 and "Dump written:" in text
    default_dir = tmp_path / "dumps"
    assert any(default_dir.glob("bgw-dump-*.json"))


def test_dump_reports_page_fetch_failures_with_exit_2(capsys, fake, snapshot_fetcher, tmp_path):
    snapshot_fetcher["pages"].pop("services")
    code, payload, _ = run_json(capsys, ["dump", "--out", str(tmp_path / "d.json"), "--json"])
    assert code == 2
    assert payload["ok"] is False and payload["failures"][0]["page"] == "services"
    assert all("parsed" not in failure for failure in payload["failures"])
    assert not (tmp_path / "d.json").exists()


def test_diff_exit_codes(capsys, fake, snapshot_fetcher, tmp_path):
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0

    code, text, _ = run(capsys, ["diff", str(out)])
    assert code == 0 and text == "No differences.\n"
    code, payload, _ = run_json(capsys, ["diff", str(out), "--json"])
    assert code == 0 and payload["identical"] is True
    assert {"services", "forwards", "reservations", "forms", "firmwareChanged"} <= set(payload)

    snapshot_fetcher["pages"]["services"] = services_page(rows=[("custom_ssh", "2483-2483", "22", "TCP")])
    code, payload, _ = run_json(capsys, ["diff", str(out), "--json"])
    assert code == 1 and payload["identical"] is False
    assert payload["services"]["missing"][0]["name"] == "Mosh"
    code, text, _ = run(capsys, ["diff", str(out)])
    assert code == 1 and "- missing service Mosh" in text


def test_diff_reports_unreadable_dump_as_error_exit(capsys, fake, snapshot_fetcher):
    code, _, err = run(capsys, ["diff", "/nonexistent-bgw-dump.json", "--host", "127.0.0.1"])
    assert code == 2 and "Cannot read dump file" in err
    code, _, err = run(capsys, ["diff"])
    assert code == 1 and "Missing dump file." in err


def test_diff_redacts_form_values_in_json(capsys, fake, snapshot_fetcher, tmp_path):
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    snapshot_fetcher["pages"]["dosprotect"] = dosprotect_page(
        selects=[select("flood_protect", [("on", "On"), ("off", "Off", True)])]
    )
    code, payload, _ = run_json(capsys, ["diff", str(out), "--json"])
    assert code == 1
    assert payload["forms"]["dosprotect"] == [{"field": "flood_protect", "dump": "on", "live": "off"}]


def test_restore_commit_requires_confirmation_before_reading_the_dump(capsys, fake, snapshot_fetcher):
    code, _, err = run(capsys, ["restore", "/nonexistent-bgw-dump.json", "--commit", "--host", "127.0.0.1"])
    assert code == 1
    assert "Refusing to restore. Re-run with --commit --confirm RESTORE." in err
    assert "/nonexistent-bgw-dump.json" not in err
    assert "client" not in fake or fake["client"].gets == []


def test_restore_dry_run_plan(capsys, fake, snapshot_fetcher, tmp_path):
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    snapshot_fetcher["pages"]["services"] = services_page(rows=[("custom_ssh", "2483-2483", "22", "TCP")])
    snapshot_fetcher["pages"]["apphosting"] = apphosting_page(rows=[("custom_ssh", "host-b")])

    code, payload, _ = run_json(capsys, ["restore", str(out), "--json"])
    assert code == 0
    assert set(payload) == {"steps", "operation", "missingPages"}
    assert payload["missingPages"] == []
    kinds = [step["kind"] for step in payload["steps"]]
    assert "add-service" in kinds and "add-forward" in kinds
    assert all("rawPayload" not in step for step in payload["steps"])
    assert payload["operation"]["operation"] == "restore" and payload["operation"]["dryRun"] is True
    assert payload["operation"]["confirmation"] == "RESTORE"
    assert fake["client"].posts == []

    code, text, _ = run(capsys, ["restore", str(out)])
    assert code == 0 and "add-service" in text and "dry-run: no router restore was sent" in text


def test_restore_commit_posts_and_exits_1_when_not_converged(capsys, fake, snapshot_fetcher, tmp_path, monkeypatch):
    monkeypatch.setattr("bgwcli.restore.SAVE_CONFIRMATION_TIMEOUT_SECONDS", 0.0)
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    snapshot_fetcher["pages"]["services"] = services_page(rows=[("custom_ssh", "2483-2483", "22", "TCP")])
    # The answer shows the services table (header only): a readable page that lacks the added row.
    fake["client"].post_body = SERVICES_TABLE_HEADER_ONLY

    code, payload, err = run_json(capsys, ["restore", str(out), "--commit", "--confirm", "RESTORE", "--json"])
    assert code == 1  # the fake router never changes, so the re-diff still shows Mosh missing
    assert set(payload) == {"execution", "diff", "verificationFailures", "operation", "missingPages", "writeUnanswered"}
    # "Changes saved" was observed: the write was answered, so the closing diff decides (exit 1).
    assert payload["writeUnanswered"] is False
    assert payload["execution"]["steps"][0]["status"] == "failed"
    assert payload["execution"]["steps"][0]["location"] is None
    assert payload["diff"]["identical"] is False and payload["verificationFailures"] == []
    assert payload["operation"]["committed"] is True
    assert fake["client"].posts and fake["client"].posts[0][0] == "services"
    assert err == ""

    code, text, err = run(capsys, ["restore", str(out), "--commit", "--confirm", "RESTORE"])
    assert code == 1
    assert "[1] failed services" in text and "restore committed" in text and "- missing service Mosh" in text


def test_restore_commit_converges_when_nothing_is_missing(capsys, fake, snapshot_fetcher, tmp_path):
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    code, payload, _ = run_json(capsys, ["restore", str(out), "--commit", "--confirm", "RESTORE", "--json"])
    assert code == 0 and payload["diff"]["identical"] is True
    assert fake["client"].posts == []


def test_restore_surfaces_pages_it_could_not_refetch(capsys, fake, snapshot_fetcher, tmp_path, monkeypatch):
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    calls = {"services": 0}
    original = cli.fetch_parsed_page

    def flaky(client, page, include_secrets=False):
        # the plan-time read succeeds; the post-write verification read of services.ha hangs
        if page == "services":
            calls["services"] += 1
            if calls["services"] > 1:
                return ParsedPageResult(page, False, error="services.ha hung after the write")
        return original(client, page, include_secrets)

    monkeypatch.setattr(cli, "fetch_parsed_page", flaky)
    code, payload, _ = run_json(capsys, ["restore", str(out), "--commit", "--confirm", "RESTORE", "--json"])
    assert code == 2
    assert payload["diff"] is None
    assert payload["verificationFailures"][0]["page"] == "services"
    calls["services"] = 0
    code, text, _ = run(capsys, ["restore", str(out), "--commit", "--confirm", "RESTORE"])
    assert code == 2
    assert "Post-restore verification incomplete" in text and "Page unavailable: services" in text


# ---------------------------------------------------------------------------------------------
# autorestore


def _saved_page_per_write(page):
    """The acknowledgement a write to `page` is answered with: "Changes saved" and that page's own table,
    so the post-save state is readable (a services table served for an apphosting write is not)."""
    from integration_html import APPHOSTING_HTML, CHANGES_SAVED_HTML, SERVICES_HTML

    return CHANGES_SAVED_HTML + (APPHOSTING_HTML if page == "apphosting" else SERVICES_HTML)


def _reset_router(snapshot_fetcher):
    """Make the live pages look factory-reset: every dumped service/forward/reservation gone."""
    snapshot_fetcher["pages"]["services"] = services_page(rows=[])
    snapshot_fetcher["pages"]["apphosting"] = apphosting_page(rows=[])


def test_autorestore_refuses_commit_without_confirm_before_reading_the_dump(capsys, fake, snapshot_fetcher):
    code, _, err = run(capsys, ["autorestore", "/nonexistent-bgw-dump.json", "--commit", "--host", "127.0.0.1"])
    assert code == 1
    assert "Refusing to autorestore. Re-run with --commit --confirm RESTORE." in err
    assert "/nonexistent-bgw-dump.json" not in err
    assert "client" not in fake or fake["client"].posts == []
    code, _, err = run(capsys, ["autorestore"])
    assert code == 1 and "Missing dump file." in err


def test_autorestore_no_reset_exits_0_and_never_posts(capsys, fake, snapshot_fetcher, tmp_path):
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    code, payload, err = run_json(capsys, ["autorestore", str(out), "--commit", "--confirm", "RESTORE", "--json"])
    assert code == 0 and payload["status"] == "no-reset" and payload["exitCode"] == 0
    assert payload["detected"] is False and payload["usedFallbackCode"] is False
    assert payload["missing"] == {"services": 0, "forwards": 0, "reservations": 0, "forms": 0}
    assert payload["missingPages"] == [] and payload["diff"]["identical"] is True and payload["plan"] is None
    assert fake["client"].posts == [] and err == ""

    # ordinary drift: one service removed by hand stays no-reset even with --commit
    snapshot_fetcher["pages"]["services"] = services_page(rows=[("custom_ssh", "2483-2483", "22", "TCP")])
    code, text, _ = run(capsys, ["autorestore", str(out), "--commit", "--confirm", "RESTORE"])
    assert code == 0 and text.startswith("no-reset: services 1/2 missing") and "- missing service Mosh" in text
    assert fake["client"].posts == []


def test_autorestore_dry_run_reports_restore_needed_with_exit_1_and_no_post(capsys, fake, snapshot_fetcher, tmp_path):
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    _reset_router(snapshot_fetcher)
    code, payload, _ = run_json(capsys, ["autorestore", str(out), "--json"])
    assert code == 1 and payload["status"] == "restore-needed" and payload["exitCode"] == 1
    assert payload["detected"] is True and payload["passes"] == []
    assert payload["missing"]["services"] == 2 and payload["missing"]["forwards"] == 2
    assert [step["kind"] for step in payload["plan"]].count("add-service") == 2
    assert all("rawPayload" not in step for step in payload["plan"])
    assert fake["client"].posts == []

    code, text, _ = run(capsys, ["autorestore", str(out)])
    assert code == 1 and "restore-needed: services 2/2 missing; forwards 2/2 missing" in text
    assert "[1] services add-service" in text and "- missing service" in text
    assert fake["client"].posts == []


def test_autorestore_commit_runs_passes_and_exits_1_when_not_converged(capsys, fake, snapshot_fetcher, tmp_path, monkeypatch):
    monkeypatch.setattr("bgwcli.restore.SAVE_CONFIRMATION_TIMEOUT_SECONDS", 0.0)
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    _reset_router(snapshot_fetcher)
    fake["client"].post_body = SERVICES_TABLE_HEADER_ONLY
    sleeps: list[float] = []
    monkeypatch.setattr("bgwcli.autorestore._sleep", sleeps.append)

    argv = ["autorestore", str(out), "--commit", "--confirm", "RESTORE", "--max-passes", "2", "--wait", "5", "--json"]
    code, payload, err = run_json(capsys, argv)
    assert code == 1 and payload["status"] == "not-converged" and payload["exitCode"] == 1
    assert [p["pass"] for p in payload["passes"]] == [1] and payload["passes"][0]["failed"] == 1
    assert sleeps == []  # unconfirmed writes stop all later passes
    assert fake["client"].posts and fake["client"].posts[0][0] == "services"
    assert payload["diff"]["identical"] is False and err == ""

    fake["client"].posts.clear()
    code, text, _ = run(capsys, ["autorestore", str(out), "--commit", "--confirm", "RESTORE", "--max-passes", "1"])
    assert code == 1
    assert "resuming unfinished recovery" in text and "pass 1/1:" in text
    assert "[1] failed services" in text and "not-converged" in text and "- missing service Mosh" in text


class _FlushCountingStdout(io.StringIO):
    """A pipe-like stdout that records every flush with the text written before it."""

    def __init__(self):
        super().__init__()
        self.flushed_at: list[int] = []

    def flush(self):
        self.flushed_at.append(len(self.getvalue()))
        super().flush()


def test_autorestore_flushes_stdout_after_every_progress_line(capsys, fake, snapshot_fetcher, tmp_path, monkeypatch):
    """Under a service manager stdout is a pipe: a line must reach the journal when it is written, so a
    kill at the unit's start timeout or a power loss loses at most the line being written."""
    monkeypatch.setattr("bgwcli.restore.SAVE_CONFIRMATION_TIMEOUT_SECONDS", 0.0)
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    _reset_router(snapshot_fetcher)
    fake["client"].post_body = '<html><title>Custom Services</title><div id="error-message-text">Changes saved</div></html>'
    monkeypatch.setattr("bgwcli.autorestore._sleep", lambda _seconds: None)
    pipe = _FlushCountingStdout()
    monkeypatch.setattr("sys.stdout", pipe)
    cli.main(["autorestore", str(out), "--commit", "--confirm", "RESTORE", "--max-passes", "1"])
    text = pipe.getvalue()
    assert "pass 1/1:" in text and "[1] failed services" in text
    # Every progress line (step lines and the run's own log lines) ends at a point a flush followed.
    progress_ends = [
        match.end() for match in re.finditer(r"^(?:\[\d+\] |pass \d+/\d+: ).*\n", text, re.MULTILINE)
    ]
    assert len(progress_ends) >= 2 and all(end in pipe.flushed_at for end in progress_ends)


def test_autorestore_uses_the_fallback_access_code_and_only_warns_without_a_reset_shaped_diff(capsys, fake, snapshot_fetcher, tmp_path, monkeypatch):
    built: list[FakeClient] = []

    class CodeCheckingClient(FakeClient):
        def __init__(self, code):
            super().__init__()
            self.code = code

        def login(self):
            if self.code != "sticker":
                raise RouterAuthError("Login failed: the access code was rejected.")
            self.logged_in = True

    def factory(options, access_code, *, on_session_wait=None, user_agent="bgw/0.1.0"):
        client = CodeCheckingClient(access_code)
        built.append(client)
        return client

    monkeypatch.setattr(cli, "_client_factory", factory)
    monkeypatch.setenv("BGW_ACCESS_CODE", "primary")
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0  # dump never logs in on the fake
    built.clear()

    # No fallback configured: the auth failure is fatal like everywhere else (exit 2).
    code, _, err = run(capsys, ["autorestore", str(out)])
    assert code == 2 and "rejected" in err and [c.code for c in built] == ["primary"]
    built.clear()

    monkeypatch.setenv("BGW_FALLBACK_ACCESS_CODE", "sticker")
    code, payload, _ = run_json(capsys, ["autorestore", str(out), "--json"])
    assert [c.code for c in built] == ["primary", "sticker"] and built[1].logged_in is True
    # The router still matches the dump: the fallback code alone is no reset, only a warning.
    assert code == 0 and payload["status"] == "no-reset" and payload["usedFallbackCode"] is True, err
    assert payload["detected"] is False and "BGW_ACCESS_CODE" in payload["warnings"][0]

    # With --commit nothing is sent either: exit 0 with the warning.
    code, text, _ = run(capsys, ["autorestore", str(out), "--commit", "--confirm", "RESTORE"])
    assert code == 0 and "warning: logged in with BGW_FALLBACK_ACCESS_CODE" in text and "no-reset" in text
    assert all(c.posts == [] for c in built)

    # Same fallback as primary is not a fallback at all.
    monkeypatch.setenv("BGW_FALLBACK_ACCESS_CODE", "primary")
    code, _, err = run(capsys, ["autorestore", str(out)])
    assert code == 2 and "rejected" in err


def test_autorestore_unreachable_router_is_quiet_and_exits_0(capsys, fake, snapshot_fetcher, tmp_path):
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0

    def boom():
        raise RouterConnectionError("connect EHOSTUNREACH 192.168.1.254:443")

    fake["client"].login = boom
    code, text, err = run(capsys, ["autorestore", str(out), "--commit", "--confirm", "RESTORE"])
    assert code == 0 and err == ""
    assert text == "router-unreachable: connect EHOSTUNREACH 192.168.1.254:443\n"
    code, payload, err = run_json(capsys, ["autorestore", str(out), "--json"])
    assert code == 0 and payload["status"] == "router-unreachable" and payload["exitCode"] == 0 and err == ""

    # a hung page before any POST is treated the same way
    fake["client"].login = lambda: None
    snapshot_fetcher["pages"].pop("services")
    code, payload, _ = run_json(capsys, ["autorestore", str(out), "--json"])
    assert code == 0 and payload["status"] == "router-unreachable" and "services" in payload["reason"]


def test_autorestore_rejects_bad_numeric_options_before_router_access(capsys, fake, snapshot_fetcher, tmp_path):
    code, _, err = run(capsys, ["autorestore", "x.json", "--max-passes", "0"])
    assert code == 1 and "--max-passes must be a finite number greater than or equal to 1." in err
    code, _, err = run(capsys, ["autorestore", "x.json", "--wait", "-1"])
    assert code == 1 and "--wait must be a finite number greater than or equal to 0." in err
    assert snapshot_fetcher["fetched"] == []


def test_autorestore_uses_the_session_coordinator():
    assert cli.uses_session_coordinator("autorestore") is True


def test_restore_uses_the_session_coordinator_and_local_commands_do_not():
    for name in ("dump", "diff", "restore", "check", "sweep", "set"):
        assert cli.uses_session_coordinator(name) is True
    for name in ("actions", "coverage", "section", "session", "sitemap", "tabs"):
        assert cli.uses_session_coordinator(name) is False


# ---------------------------------------------------------------------------------------------
# fatal handling


def test_router_errors_map_to_exit_codes(capsys, fake):
    fake["client"] = FakeClient({})
    code, out, err = run(capsys, ["auth"])
    assert code == 0  # login is a no-op on the fake

    def boom(*args, **kwargs):
        raise RouterConnectionError("connect ECONNREFUSED")

    fake["client"].login = boom
    code, out, err = run(capsys, ["auth"])
    assert code == 2 and err == "connect ECONNREFUSED\n" and out == ""


def test_fixture_capture_pool_full_exits_2_and_records_cooldown(capsys, fake, monkeypatch, tmp_env):
    """The hidden fixtures-capture path must surface a full session pool like sweep does (exit 2 +
    cooldown file), not swallow it by returning None to the session coordinator."""

    def raise_pool_full(*args, **kwargs):
        raise session_pool_full_error(waited_ms=11, retry_count=4)

    fake["client"] = FakeClient()
    fake["client"].get_cgi_page = raise_pool_full
    code, out, err = run(capsys, ["fixtures-capture", "--out", str(tmp_env / "fx"), "--pages", "sysinfo"])
    assert code == 2
    cooldowns = list((tmp_env / "cache").glob("*.cooldown.json"))
    assert len(cooldowns) == 1
    assert json.loads(cooldowns[0].read_text())["waitedMs"] == 11


def test_fixture_capture_reads_stdin_access_code_only_once(capsys, fake, monkeypatch, tmp_env):
    monkeypatch.delenv("BGW_ACCESS_CODE", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret\n"))
    argv = ["fixtures-capture", "--access-code-stdin", "--out", str(tmp_env / "fx"), "--pages", "sysinfo"]
    code, out, err = run(capsys, argv)
    assert "Set BGW_ACCESS_CODE" not in err
    assert fake["access_code"] == "s3cret"


def test_dump_defaults_to_fixed_rows_and_all_clients_flag_keeps_dhcp_rows(capsys, fake, snapshot_fetcher, tmp_path):
    from page_builders import ipalloc_page

    snapshot_fetcher["pages"]["ipalloc"] = ipalloc_page()  # 2 Fixed rows + 1 DHCP row
    default_path, all_path = tmp_path / "default.json", tmp_path / "all.json"
    assert run(capsys, ["dump", "--out", str(default_path)])[0] == 0
    assert run(capsys, ["dump", "--out", str(all_path), "--all-clients"])[0] == 0
    default, everything = json.loads(default_path.read_text()), json.loads(all_path.read_text())
    assert len(default["tables"]["ipalloc"]) == 2 and len(everything["tables"]["ipalloc"]) == 3
    assert all("fixed" in r["Allocation"].lower() for r in default["tables"]["ipalloc"])
    assert default["reservations"] == everything["reservations"]
    code, text, _ = run(capsys, ["help"])
    assert "--all-clients" in text and "--reservations-only" not in text


def test_dump_captures_core_pages_only_unless_include_names_optional_pages(capsys, fake, snapshot_fetcher, tmp_path):
    core, some, everything = tmp_path / "core.json", tmp_path / "some.json", tmp_path / "all.json"
    code, payload, _ = run_json(capsys, ["dump", "--out", str(core), "--json"])
    assert code == 0 and payload["forms"] == ["dosprotect", "wconfig"]
    assert json.loads(core.read_text())["forms"].keys() == {"dosprotect", "wconfig"}
    # Optional pages are never fetched when not included.
    assert not {"etherlan", "dhcpserver", "ippass", "wmacauth"} & set(snapshot_fetcher["fetched"])
    assert "packetfilter" in snapshot_fetcher["fetched"] and "ipalloc" in snapshot_fetcher["fetched"]

    snapshot_fetcher["fetched"].clear()
    code, payload, _ = run_json(capsys, ["dump", "--out", str(some), "--include", "dhcpserver", "--json"])
    assert code == 0 and payload["forms"] == ["dosprotect", "wconfig", "dhcpserver"]
    assert "dhcpserver" in snapshot_fetcher["fetched"] and "etherlan" not in snapshot_fetcher["fetched"]

    code, payload, _ = run_json(capsys, ["dump", "--out", str(everything), "--include", "all", "--json"])
    assert code == 0
    assert payload["forms"] == ["dosprotect", "wconfig", "etherlan", "dhcpserver", "ippass", "wmacauth"]
    assert "etherlan" in payload["tables"] and "wmacauth" in payload["tables"]
    code, text, _ = run(capsys, ["dump", "--out", str(tmp_path / "t.json"), "--include", "ippass,wmacauth"])
    assert code == 0 and "Forms: dosprotect, wconfig, ippass, wmacauth" in text


def test_dump_rejects_an_unknown_include_page_before_touching_the_router(capsys, fake, snapshot_fetcher, tmp_path):
    code, _, err = run(capsys, ["dump", "--out", str(tmp_path / "d.json"), "--include", "dhcpserver,nope"])
    assert code == 1 and "nope" in err and "etherlan, dhcpserver, ippass, wmacauth" in err
    assert snapshot_fetcher["fetched"] == [] and not (tmp_path / "d.json").exists()


def test_diff_include_restricts_the_comparison_and_warns_about_pages_missing_from_the_dump(
    capsys, fake, snapshot_fetcher, tmp_path
):
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0  # core only: no dhcpserver in the dump
    snapshot_fetcher["pages"]["services"] = services_page(rows=[("custom_ssh", "2483-2483", "22", "TCP")])

    code, _, err = run(capsys, ["diff", str(out)])
    assert code == 1 and err == ""
    code, text, err = run(capsys, ["diff", str(out), "--include", "dosprotect"])
    assert code == 0 and text == "No differences.\n" and err == ""
    code, text, err = run(capsys, ["diff", str(out), "--include", "services,dosprotect"])
    assert code == 1 and "- missing service Mosh" in text

    snapshot_fetcher["fetched"].clear()
    code, payload, err = run_json(capsys, ["diff", str(out), "--include", "dhcpserver,wconfig", "--json"])
    assert code == 0 and payload["identical"] is True
    assert payload["missingPages"] == ["dhcpserver"]
    assert err == "warning: page 'dhcpserver' is not present in the dump; nothing to compare/restore\n"
    # Optional pages the dump did not capture are never fetched for a diff.
    assert "dhcpserver" not in snapshot_fetcher["fetched"] and "etherlan" not in snapshot_fetcher["fetched"]
    assert "wconfig" in snapshot_fetcher["fetched"]

    code, payload, err = run_json(capsys, ["diff", str(out), "--json"])
    assert payload["missingPages"] == [] and err == ""
    code, _, err = run(capsys, ["diff", str(out), "--include", "packetfilter"])
    assert code == 1 and "packetfilter" in err and "Unknown" not in err and snapshot_fetcher["fetched"]


def test_diff_and_restore_compare_optional_pages_the_dump_captured(capsys, fake, snapshot_fetcher, tmp_path):
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out), "--include", "dhcpserver"])[0] == 0
    snapshot_fetcher["pages"]["dhcpserver"] = page(
        "dhcpserver", title="Subnets & DHCP", selects=[select("dhcp", ["off", "on"], selected="off")],
        buttons=[button("Save", "Save")],
    )
    snapshot_fetcher["fetched"].clear()
    code, payload, err = run_json(capsys, ["diff", str(out), "--json"])
    assert code == 1 and payload["forms"]["dhcpserver"] == [{"field": "dhcp", "dump": "on", "live": "off"}]
    assert "dhcpserver" in snapshot_fetcher["fetched"] and "ippass" not in snapshot_fetcher["fetched"]

    code, payload, err = run_json(capsys, ["restore", str(out), "--include", "dhcpserver", "--json"])
    assert code == 0 and payload["missingPages"] == [] and err == ""
    assert [(s["page"], s["kind"]) for s in payload["steps"]] == [("dhcpserver", "form")]
    assert payload["steps"][0]["warning"] and "gateway" in payload["steps"][0]["warning"]


def test_restore_include_reports_a_page_missing_from_the_dump(capsys, fake, snapshot_fetcher, tmp_path):
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    snapshot_fetcher["pages"]["services"] = services_page(rows=[("custom_ssh", "2483-2483", "22", "TCP")])

    code, payload, err = run_json(capsys, ["restore", str(out), "--include", "dhcpserver", "--json"])
    assert code == 1
    assert payload["ok"] is False and payload["missingPages"] == ["dhcpserver"]
    assert payload["error"] == "nothing compared: dhcpserver not in dump"
    assert fake["client"].posts == []

    code, text, err = run(capsys, ["restore", str(out), "--include", "dhcpserver"])
    assert code == 1 and "nothing compared: dhcpserver not in dump" in err

    # A committed run with a selection only posts the selected pages and reports the missing ones too.
    code, payload, err = run_json(
        capsys, ["restore", str(out), "--include", "dosprotect,dhcpserver", "--commit", "--confirm", "RESTORE", "--json"]
    )
    assert code == 0 and payload["missingPages"] == ["dhcpserver"] and payload["diff"]["identical"] is True
    assert fake["client"].posts == []  # dosprotect is identical, dhcpserver skipped, services out of scope
    code, _, err = run(capsys, ["restore", str(out), "--include", "bogus"])
    assert code == 1 and "bogus" in err


def test_diagnostics_commit_polls_the_diag_page_when_the_post_answers_with_a_redirect(capsys, fake, monkeypatch):
    """Real gateway: the POST answers 302 with an empty body and fills ProgressWindow asynchronously.
    The CLI must follow up by polling diag until the output is present, instead of reporting
    'no progress output found'."""
    from bgwcli import diagnostics as diag

    monkeypatch.setattr(diag, "_sleep", lambda s: None)
    fake["client"] = FakeClient(post_status=302, post_location="/cgi-bin/diag.ha", post_body="")
    argv = ["diagnostics", "ping", "example.com", "--commit", "--confirm", "DIAG", "--json"]
    code, payload, _ = run_json(capsys, argv)
    assert code == 0
    assert payload["statusCode"] == 302
    assert payload["result"] == "PING example.com: 3 packets"
    assert payload["pageResult"]["page"] == "diag"
    assert fake["client"].gets.count("diag") >= 3  # plan fetch + at least two polls (value, then stable)


DOSPROTECT_HTML = lambda value: f"""<html><head><title>Firewall Advanced</title></head><body><h1>Firewall Advanced</h1>
<form method="post" action="/cgi-bin/dosprotect.ha"><input type="hidden" name="nonce" value="n">
<select name="icmp_downstream_echo_rqst_drop_wan"><option value="off"{' selected' if value == 'off' else ''}>off</option><option value="on"{' selected' if value == 'on' else ''}>on</option></select>
<input type="submit" name="Save" value="Save"></form></body></html>"""  # noqa: E731


class SaveAcknowledgingClient(FakeClient):
    """Serve the gateway's one-shot save acknowledgement only after the final write."""

    pending_ack: str | None = None

    def post_cgi_page(self, page, fields):
        response = super().post_cgi_page(page, fields)
        if "Continue" in fields:
            self.pending_ack = page
            return HttpResponse(302, "Found", {"location": f"/cgi-bin/{page}.ha"}, "", response.url)
        if "Save" in fields and "wifiwarn" not in (self.post_location or ""):
            self.pending_ack = page
        return response

    def get_cgi_page(self, page, *, auth=True):
        response = super().get_cgi_page(page, auth=auth)
        if self.pending_ack != page:
            return response
        self.pending_ack = None
        body = response.body.replace("</body>", '<div id="error-message-text">Changes saved</div></body>')
        return HttpResponse(response.status_code, response.status_message, response.headers, body, response.url)


def test_set_commit_posts_the_save_button_and_verifies_the_change(capsys, fake):
    fake["client"] = SaveAcknowledgingClient({**PAGES, "dosprotect": DOSPROTECT_HTML("on")}, post_status=302, post_location="/cgi-bin/dosprotect.ha", post_body="")
    argv = ["set", "dosprotect", "icmp_downstream_echo_rqst_drop_wan=on", "--commit", "--confirm", "DOSPROTECT", "--json"]
    code, payload, _ = run_json(capsys, argv)
    assert code == 0
    page, fields = fake["client"].posts[0]
    assert page == "dosprotect" and fields["icmp_downstream_echo_rqst_drop_wan"] == "on" and fields["Save"] == "Save"
    assert payload["committed"] is True and payload["verified"] is True
    assert fake["client"].gets.count("dosprotect") == 3  # plan fetch, post-redirect banner check, verification re-read


def test_set_commit_reports_a_discarded_change_with_exit_1(capsys, fake):
    fake["client"] = SaveAcknowledgingClient({**PAGES, "dosprotect": DOSPROTECT_HTML("off")}, post_status=302, post_location="/cgi-bin/dosprotect.ha", post_body="")
    argv = ["set", "dosprotect", "icmp_downstream_echo_rqst_drop_wan=on", "--commit", "--confirm", "DOSPROTECT"]
    code, out, _ = run(capsys, argv)
    assert code == 1
    assert "Verified      no" in out and "icmp_downstream_echo_rqst_drop_wan" in out


WCONFIG_HTML = """<html><body><form method="post" action="/cgi-bin/wconfig.ha"><input type="hidden" name="nonce" value="n">
<input type="text" name="maxclients" value="80"><input type="submit" name="Save" value="Save..."></form></body></html>"""
WIFIWARN_HTML = """<html><body><h1>Wi-Fi Warning</h1><form method="post" action="/cgi-bin/wconfig.ha">
<input type="submit" name="Continue" value="Continue"></form><form method="post" action="/cgi-bin/wifiwarn_advanced.ha">
<input type="submit" name="Cancel" value="Cancel"></form></body></html>"""


def test_set_commit_follows_the_wifi_warning_page_and_posts_continue_to_the_owning_form(capsys, fake):
    fake["client"] = SaveAcknowledgingClient({**PAGES, "wconfig": WCONFIG_HTML, "wifiwarn_advanced": WIFIWARN_HTML}, post_status=302, post_location="/cgi-bin/wifiwarn_advanced.ha", post_body="")
    argv = ["set", "wconfig", "maxclients=81", "--commit", "--confirm", "WCONFIG", "--json"]
    code, payload, _ = run_json(capsys, argv)
    assert [p for p, _ in fake["client"].posts] == ["wconfig", "wconfig"]
    assert fake["client"].posts[0][1]["Save"] == "Save..." and fake["client"].posts[1][1] == {"Continue": "Continue"}
    assert "wifiwarn_advanced" in fake["client"].gets
    assert fake["client"].form_nonce_pages == ["wifiwarn_advanced"]  # Continue's nonce is the warning page's own
    assert payload["committed"] is True


def test_set_and_submit_dry_runs_label_dangerous_pages(capsys, fake):
    code, out, _ = run(capsys, ["submit", "restart", "Restart"])
    assert code == 0 and re.search(r"Dangerous\s+yes", out)


def test_action_with_post_path_routes_through_post_form(capsys, fake):
    class FormClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.forms: list[tuple[str, str, dict[str, str]]] = []

        def post_form(self, nonce_page, post_path, fields):
            self.forms.append((nonce_page, post_path, dict(fields)))
            return HttpResponse(302, "Found", {"location": "/cgi-bin/home.ha"}, "", f"{self.origin}/cgi-bin/{post_path}")

    fake["client"] = FormClient()
    fake["client"].pages["home"] = (  # the source page, then the redirect answer page
        '<html><body>Device Status<form method="post" action="/cgi-bin/wrestart.ha?1">'
        '<input type="hidden" name="nonce" value="n1"><input type="submit" name="WRestart1" value="Restart">'
        "</form></body></html>"
    )
    code, payload, _ = run_json(capsys, ["action", "restart-wifi-2.4", "--commit", "--confirm", "RESTART-WIFI", "--json"])
    assert code == 0 and payload["committed"] is True and payload["statusCode"] == 302
    assert fake["client"].forms == [("home", "wrestart.ha?1", {"WRestart1": "Restart"})]
    assert fake["client"].posts == []  # not posted to home.ha itself
    code, out, _ = run(capsys, ["action", "restart-wifi-2.4"])
    assert code == 0 and "dry-run" in out and "RESTART-WIFI" in out


WCONFIG_SCAN_HTML = """<html><body><form method="post" action="/cgi-bin/wconfig.ha"><input type="hidden" name="nonce" value="n">
<input type="text" name="maxclients" value="80"><select name="wl80211on_5"><option value="on" selected>On</option><option value="off">Off</option></select>
<input type="submit" name="chanscan5" value="Find Best Channel"><input type="submit" name="Save" value="Save..."></form></body></html>"""


def test_form_button_action_posts_the_live_form_payload_plus_its_button_and_follows_the_warning(capsys, fake):
    fake["client"] = SaveAcknowledgingClient({**PAGES, "wconfig": WCONFIG_SCAN_HTML, "wifiwarn_advanced": WIFIWARN_HTML}, post_status=302, post_location="/cgi-bin/wifiwarn_advanced.ha", post_body="")
    code, payload, _ = run_json(capsys, ["action", "find-best-channel-5", "--commit", "--confirm", "CHANSCAN", "--json"])
    assert code == 0 and payload["committed"] is True
    pages = [p for p, _ in fake["client"].posts]
    assert pages == ["wconfig", "wconfig"]  # the scan POST, then Continue posted to the owning form
    first = fake["client"].posts[0][1]
    assert first["chanscan5"] == "Find Best Channel" and first["maxclients"] == "80" and first["wl80211on_5"] == "on"
    assert "Save" not in first  # the scan button, not Save, submits the form
    assert fake["client"].posts[1][1] == {"Continue": "Continue"}
    code, out, _ = run(capsys, ["action", "find-best-channel-5"])
    assert code == 0 and "dry-run" in out and "CHANSCAN" in out and "chanscan5" in out


def test_generic_submit_commit_follows_the_wifi_warning_page(capsys, fake):
    fake["client"] = SaveAcknowledgingClient({**PAGES, "wconfig": WCONFIG_SCAN_HTML, "wifiwarn_advanced": WIFIWARN_HTML}, post_status=302, post_location="/cgi-bin/wifiwarn_advanced.ha", post_body="")
    code, payload, _ = run_json(capsys, ["submit", "wconfig", "chanscan5", "--commit", "--confirm", "WCONFIG", "--json"])
    assert code == 0 and payload["committed"] is True
    assert [p for p, _ in fake["client"].posts] == ["wconfig", "wconfig"]
    assert fake["client"].posts[1][1] == {"Continue": "Continue"}


def test_timeout_flag_marks_the_timeout_explicit_and_default_does_not(capsys, fake):
    run(capsys, ["check"])
    assert fake["options"].timeout_ms == 15000 and fake["options"].timeout_explicit is False
    run(capsys, ["check", "--timeout", "2.5"])
    assert fake["options"].timeout_ms == 2500 and fake["options"].timeout_explicit is True


def test_submit_commit_fails_loudly_when_the_router_shows_its_error_banner_after_the_redirect(capsys, fake):
    banner = """<html><body><form method="post" action="/cgi-bin/apphosting.ha"><input type="hidden" name="nonce" value="n">
<img id="error-message-icon" src="/images/icon_error.png" alt="alert" /><div id="error-message-text"> A required setting is empty <br /></div>
<select name="service"><option value="*Mosh">*Mosh</option></select><select name="device"><option value="aa:bb:cc:dd:ee:02">host-b</option></select>
<input type="submit" name="Add" value="Add"></form></body></html>"""
    fake["client"] = FakeClient({**PAGES, "apphosting": banner}, post_status=302, post_location="/cgi-bin/apphosting.ha")
    code, out, err = run(capsys, ["submit", "apphosting", "Add", "service=*Mosh", "device=aa:bb:cc:dd:ee:02", "--commit", "--confirm", "APPHOSTING"])
    assert code == 1
    # Same reporter as the Wi-Fi/LAN paths: the rejection is a structured result, not a bare stderr line.
    assert "router rejected the change: A required setting is empty" in out and "submit committed" not in out


@pytest.mark.parametrize("failure", ["auth", "transport", "extraction", "pool"])
def test_restore_closing_verification_exception_retains_json_execution(
    capsys, fake, snapshot_fetcher, tmp_path, monkeypatch, failure,
):
    from bgwcli.errors import SnapshotExtractionError

    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    snapshot_fetcher["pages"]["services"] = services_page(rows=[])
    from integration_html import CHANGES_SAVED_HTML, SERVICES_HTML
    fake["client"].post_body = CHANGES_SAVED_HTML + SERVICES_HTML
    original = cli._fetch_snapshot_pages
    calls = 0

    def fetch(client, pages):
        nonlocal calls
        calls += 1
        if calls == 2:
            if failure == "extraction":
                malformed = dict(snapshot_fetcher["pages"])
                malformed["apphosting"] = apphosting_page(rows=[("custom_ssh", "offline-host")], device_options=[])
                return malformed, []
            if failure == "pool":
                raise session_pool_full_error()
            raise {"auth": RouterAuthError, "transport": RouterConnectionError}[failure]("verification unavailable")
        return original(client, pages)

    monkeypatch.setattr(cli, "_fetch_snapshot_pages", fetch)
    code, payload, _ = run_json(capsys, ["restore", str(out), "--commit", "--confirm", "RESTORE", "--json"])
    assert code == 2
    assert payload["diff"] is None and payload["execution"]["steps"][0]["status"] == "applied"
    assert payload["verificationError"]["type"] == {
        "auth": RouterAuthError, "transport": RouterConnectionError, "extraction": SnapshotExtractionError,
        "pool": type(session_pool_full_error()),
    }[failure].__name__
    assert payload["operation"]["committed"] is True
    if failure == "pool":
        assert payload["sessionPoolFull"] is True
        assert cli.read_session_state(fake["client"].origin).pool_cooldown_until is not None


def test_cli_autorestore_resumes_with_durable_state(capsys, fake, snapshot_fetcher, tmp_path, monkeypatch):
    monkeypatch.setattr("bgwcli.restore.SAVE_CONFIRMATION_TIMEOUT_SECONDS", 0.0)
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    original = dict(snapshot_fetcher["pages"])
    _reset_router(snapshot_fetcher)
    fake["client"].post_body = _saved_page_per_write
    argv = ["autorestore", str(out), "--commit", "--confirm", "RESTORE", "--max-passes", "1", "--json"]
    code, first, _ = run_json(capsys, argv)
    assert code == 1 and first["status"] == "not-converged"
    snapshot_fetcher["pages"]["services"] = original["services"]
    fake["client"].posts.clear()
    code, second, _ = run_json(capsys, argv)
    assert code == 1 and second["status"] == "not-converged"
    assert "resuming unfinished recovery" in second["reason"] or second["detected"]
    assert fake["client"].posts and all(page != "services" for page, _ in fake["client"].posts)
    state_dir = tmp_path / "state" / "bgw" / "recovery"
    assert len(list(state_dir.glob("*.recovery.json"))) == 1
    snapshot_fetcher["pages"] = original
    code, third, _ = run_json(capsys, argv)
    assert code == 0 and third["status"] == "converged"
    assert not list(state_dir.glob("*.recovery.json"))



def test_autorestore_closing_pool_full_preserves_evidence_and_starts_cooldown(
    capsys, fake, snapshot_fetcher, tmp_path, monkeypatch,
):
    monkeypatch.setattr("bgwcli.restore.SAVE_CONFIRMATION_TIMEOUT_SECONDS", 0.0)
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    _reset_router(snapshot_fetcher)
    fake["client"].post_body = _saved_page_per_write
    original = cli._fetch_snapshot_pages
    calls = 0

    def fetch(client, pages):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise session_pool_full_error()
        return original(client, pages)

    monkeypatch.setattr(cli, "_fetch_snapshot_pages", fetch)
    code, payload, _ = run_json(capsys, ["autorestore", str(out), "--commit", "--confirm", "RESTORE", "--json"])
    assert code == 2 and payload["status"] == "error"
    assert payload["diff"] is None and payload["passes"][0]["applied"] > 0
    assert payload["sessionPoolFull"] is True
    assert cli.read_session_state(fake["client"].origin).pool_cooldown_until is not None


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("include_secrets", [False, True])
def test_failed_set_redacts_secret_mismatches(capsys, fake, json_output, include_secrets):
    body = ('<title>WiFi</title><form><input name="key11" value="synthetic-old-secret">'
            '<input name="Save" type="submit" value="Save"></form>')
    fake["client"] = FakeClient({"wconfig_unified": body}, post_body='<div id="error-message-text">Changes saved</div>')
    argv = ["set", "wconfig_unified", "key11=synthetic-new-secret", "--commit", "--confirm", "WCONFIG-UNIFIED"]
    if json_output:
        argv.append("--json")
    if include_secrets:
        argv.append("--include-secrets")
    code, out, err = run(capsys, argv)
    assert code == 1 and err == ""
    if include_secrets:
        assert "synthetic-old-secret" in out and "synthetic-new-secret" in out
    else:
        assert "synthetic-old-secret" not in out and "synthetic-new-secret" not in out
        assert "[redacted]" in out
    if json_output:
        assert json.loads(out)["verified"] is False


@pytest.mark.parametrize("operation,args", [("set", ["x=y"]), ("submit", ["Reset"])])
@pytest.mark.parametrize("page,token", [
    ("reset.ha?x=1", "RESET-HA-X-1"), ("reset?x=1", "RESET-X-1"), ("reset#x", "RESET-X"),
    ("/cgi-bin/reset.ha", "-CGI-BIN-RESET-HA"), ("../reset", "-RESET"),
    ("re%73et", "RE-73ET"), ("reset%2eha", "RESET-2EHA"), ("ReSeT.hA", "RESET-HA"), ("Re-SeT", "RESET"),
])
def test_generic_mutations_reject_dangerous_spellings_before_fetch(capsys, fake, operation, args, page, token):
    body = ('<title>Reset</title><form><input name="x" value="original">'
            '<input name="Reset" type="submit" value="Reset"></form>')
    fake["client"] = FakeClient({page: body})
    code, _, err = run(capsys, [operation, page, *args, "--commit", f"--confirm={token}"])
    assert code == 1 and err
    assert fake["client"].gets == [] and fake["client"].posts == []


def test_generic_submit_keeps_the_observed_redirect_when_the_warning_page_has_no_continue(capsys, fake):
    """A failed Wi-Fi Warning confirmation carries no status code of its own; the result must keep the
    POST's observed 302 and redirect target instead of nulling them."""
    no_continue = """<html><body><h1>Wi-Fi Warning</h1><form method="post" action="/cgi-bin/wifiwarn_advanced.ha">
<input type="submit" name="Cancel" value="Cancel"></form></body></html>"""
    fake["client"] = FakeClient({**PAGES, "wconfig": WCONFIG_SCAN_HTML, "wifiwarn_advanced": no_continue}, post_status=302, post_location="/cgi-bin/wifiwarn_advanced.ha", post_body="")
    code, payload, _ = run_json(capsys, ["submit", "wconfig", "chanscan5", "--commit", "--confirm", "WCONFIG", "--json"])
    assert code == 2 and payload["committed"] is False and payload["outcome"] == "failed"
    assert payload["statusCode"] == 302
    assert payload["location"] == "/cgi-bin/wifiwarn_advanced.ha"
    assert "no Continue button" in payload["warning"]


class _FailingPagesClient(FakeClient):
    """Answers every gateway page with a 500 except the pages named in `healthy`."""

    def __init__(self, healthy=()):
        super().__init__()
        self.healthy = set(healthy)

    def get_cgi_page(self, page, *, auth=True):
        if page in self.healthy:
            return super().get_cgi_page(page, auth=auth)
        self.gets.append(page)
        return HttpResponse(500, "Internal Server Error", {}, "<html>error</html>", f"{self.origin}/cgi-bin/{page}.ha")


@pytest.mark.parametrize("name", ["sweep", "schema", "audit"])
def test_sweep_exits_2_with_json_when_every_page_failed(capsys, fake, name):
    fake["client"] = _FailingPagesClient()
    code, payload, _ = run_json(capsys, [name, "--pages", "diag,logs", "--delay", "0", "--json"])
    assert code == 2
    assert payload  # the structured per-page result is still printed


@pytest.mark.parametrize("name", ["sweep", "schema", "audit"])
def test_sweep_exits_0_when_any_page_loaded(capsys, fake, name):
    fake["client"] = _FailingPagesClient(healthy={"diag"})
    code, payload, _ = run_json(capsys, [name, "--pages", "diag,logs", "--delay", "0", "--json"])
    assert code == 0
    assert payload


@pytest.mark.parametrize("flag", ["--include", "--pages"])
@pytest.mark.parametrize("value", ["", ",", " , "])
@pytest.mark.parametrize("command", [["diff", "dump.json"], ["restore", "dump.json"], ["dump"], ["sweep"]])
def test_an_include_or_pages_list_naming_no_page_is_a_usage_error(capsys, fake, flag, value, command):
    """An empty selection must never widen to "every page"."""
    code, out, err = run(capsys, [*command, f"{flag}={value}"])
    assert code == 1
    assert flag in err and "at least one page" in err
    assert "client" not in fake, "nothing may run on an empty selection"


def test_empty_include_is_a_usage_error_in_snapshot_diff_resolution():
    from bgwcli.errors import UsageError
    from bgwcli.snapshot_diff import resolve_include

    with pytest.raises(UsageError):
        resolve_include([])
    with pytest.raises(UsageError):
        resolve_include(" , ")
    assert resolve_include(None) is None
    assert resolve_include("all") is None


def test_snapshot_reads_never_fetch_the_unified_wifi_page(capsys, fake, snapshot_fetcher, tmp_path):
    """extract_snapshot never uses wconfig_unified: dump/diff must not spend a request (or fail) on it."""
    snapshot_fetcher["pages"].pop("wconfig_unified")
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out), "--include", "all"])[0] == 0
    assert run(capsys, ["diff", str(out)])[0] == 0
    assert "wconfig_unified" not in snapshot_fetcher["fetched"]


def test_an_unreadable_documentary_packet_filter_page_is_a_warning_not_an_abort(
    capsys, fake, snapshot_fetcher, tmp_path
):
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    snapshot_fetcher["pages"].pop("packetfilter")

    code, payload, err = run_json(capsys, ["diff", str(out), "--json"])
    assert code == 0 and payload["identical"] is True
    assert "warning" in err and "packetfilter" in err

    second = tmp_path / "second.json"
    code, payload, err = run_json(capsys, ["dump", "--out", str(second), "--json"])
    assert code == 0 and second.exists()
    assert "warning" in err and "packetfilter" in err
    assert "packetfilter" not in json.loads(second.read_text())["tables"]


@pytest.mark.parametrize("argv,page", [
    (["page", "diag"], "diag"),
    (["logs"], "logs"),
    (["wifi"], "wconfig_unified"),
])
def test_raw_json_emits_the_page_html_inside_a_json_object(capsys, fake, argv, page):
    code, payload, _ = run_json(capsys, [*argv, "--raw", "--json"])
    assert code == 0
    assert payload == {"page": page, "raw": PAGES[page]}


def test_raw_without_json_still_writes_the_bare_html(capsys, fake):
    code, out, _ = run(capsys, ["page", "diag", "--raw"])
    assert code == 0 and out == DIAG_HTML


def test_fixture_capture_json_prints_a_json_summary(capsys, fake, tmp_env):
    out_dir = tmp_env / "fx"
    code, out, err = run(capsys, ["fixtures-capture", "--out", str(out_dir), "--pages", "diag", "--json"])
    assert code == 0
    payload = json.loads(out)
    assert payload["out"] == str(out_dir)
    assert payload["captured"] == 1 and payload["total"] == 1
    assert payload["degraded"] == []
    assert payload["pages"] == [{"page": "diag", "ok": True, "captured": True, "degraded": False, "error": None}]
    assert "capturing diag" in err


@pytest.mark.parametrize("json_mode", [True, False])
def test_fixture_capture_where_every_fetch_failed_exits_2_and_keeps_the_receipts(capsys, fake, tmp_env, json_mode):
    fake["client"] = FakeClient({})  # every page times out
    out_dir = tmp_env / "fx"
    argv = ["fixtures-capture", "--out", str(out_dir), "--pages", "diag,sysinfo"]
    code, out, _ = run(capsys, [*argv, "--json"] if json_mode else argv)
    assert code == 2
    if json_mode:
        payload = json.loads(out)
        assert payload["captured"] == 0 and payload["total"] == 2
    assert any(out_dir.rglob("*diag*"))  # the failure receipt is still written


@pytest.mark.parametrize("argv", [["page", "home"], ["page", "lanstatistics"], ["page", "securityoptions"], ["status"]])
def test_a_status_view_that_read_nothing_is_no_answer(capsys, fake, argv):
    fake["client"] = FakeClient({})  # every page times out
    code, payload, _ = run_json(capsys, [*argv, "--json"])
    assert code == 2
    sections = payload if isinstance(payload, list) else payload["sections"]
    assert sections and not any(section["ok"] for section in sections)
    code, _, _ = run(capsys, argv)
    assert code == 2


@pytest.mark.parametrize("argv", [["page", "home"], ["status"]])
def test_a_status_view_with_one_readable_section_still_answers(capsys, fake, argv):
    fake["client"] = FakeClient({"sysinfo": DIAG_HTML})
    code, _, _ = run_json(capsys, [*argv, "--json"])
    assert code == 0


@pytest.mark.parametrize("command", ["audit", "readiness"])
def test_raw_is_refused_for_audit(capsys, fake, command):
    code, out, err = run(capsys, [command, "--raw", "--pages", "diag"])
    assert code == 1 and "--raw" in err and out == ""
    assert fake["client"].gets == []


def test_autorestore_caches_the_fallback_session_and_drops_the_primary_one(
    capsys, fake, snapshot_fetcher, tmp_path, monkeypatch
):
    """The fallback client's session is the live one: it is what the coordinator caches, so the next
    timer run reuses it instead of spending two more logins of the small session pool."""
    from bgwcli import session
    from bgwcli.types import RouterSessionSnapshot

    built: list[FakeClient] = []

    class SessionfulClient(FakeClient):
        def __init__(self, code):
            super().__init__()
            self.code = code
            self.cookies: dict[str, str] = {}
            self.authenticated = False

        def login(self, initial_login_html=None, *, force=False):
            if self.authenticated and not force:
                return
            self.authenticated = False
            if self.code != "sticker":
                raise RouterAuthError("Login failed: the access code was rejected.")
            self.cookies, self.authenticated = {"sid": "fallback-session"}, True

        def has_authenticated_session(self):
            return self.authenticated and bool(self.cookies)

        def export_session(self):
            return RouterSessionSnapshot(origin=self.origin, authenticated=self.authenticated, cookies=dict(self.cookies))

        def import_session(self, snapshot):
            if not isinstance(snapshot, dict):
                snapshot = {"authenticated": snapshot.authenticated, "cookies": snapshot.cookies}
            if snapshot.get("authenticated") is True and snapshot.get("cookies"):
                self.cookies, self.authenticated = dict(snapshot["cookies"]), True

        def clear_session(self):
            self.cookies, self.authenticated = {}, False

    def factory(options, access_code, *, on_session_wait=None, user_agent="bgw/0.1.0"):
        client = SessionfulClient(access_code)
        built.append(client)
        return client

    monkeypatch.setattr(cli, "_client_factory", factory)
    monkeypatch.setenv("BGW_ACCESS_CODE", "primary")
    monkeypatch.setenv("BGW_FALLBACK_ACCESS_CODE", "sticker")
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    built.clear()

    code, payload, _ = run_json(capsys, ["autorestore", str(out), "--json"])
    assert code == 0 and payload["usedFallbackCode"] is True
    assert [c.code for c in built] == ["primary", "sticker"]
    cache = session.session_paths(built[0].origin).cache
    assert json.loads(cache.read_text())["cookies"] == {"sid": "fallback-session"}

    # The next run imports the cached fallback session: no login is attempted at all.
    built.clear()
    code, payload, _ = run_json(capsys, ["autorestore", str(out), "--json"])
    assert [c.code for c in built] == ["primary"] and built[0].authenticated is True
    assert payload["usedFallbackCode"] is False


def test_autorestore_login_http_error_is_router_unreachable_without_trying_the_fallback(
    capsys, fake, snapshot_fetcher, tmp_path, monkeypatch
):
    from bgwcli.client import RouterResponseError

    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    built: list[str] = []

    def factory(options, access_code, *, on_session_wait=None, user_agent="bgw/0.1.0"):
        built.append(access_code)
        client = FakeClient()

        def login(*args, **kwargs):
            raise RouterResponseError("Router rejected https://router.local/cgi-bin/login.ha with HTTP 503.",
                                      status_code=503)

        client.login = login
        return client

    monkeypatch.setattr(cli, "_client_factory", factory)
    monkeypatch.setenv("BGW_FALLBACK_ACCESS_CODE", "sticker")
    code, text, _ = run(capsys, ["autorestore", str(out), "--commit", "--confirm", "RESTORE"])
    assert code == 0 and text.startswith("router-unreachable:") and "HTTP 503" in text
    assert built == ["unused"], "an HTTP error is not a rejected access code"



def _autorestore_over_a_dead_cached_session(capsys, snapshot_fetcher, tmp_path, monkeypatch, fallback):
    """A real client over a fake router whose only accepted code is "sticker"; a cached primary
    session on disk that the router no longer honours. Returns (exit, payload, stderr, built codes,
    transport, session paths)."""
    import hashlib
    from urllib.parse import parse_qs

    from save_helpers import FakeTransport, html

    from bgwcli import session
    from bgwcli.client import BGW320Client

    origin = "http://router.local"
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    login_page = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'

    def router(request, n):
        path = request.url.rsplit("/", 1)[-1]
        cookie = request.headers.get("Cookie", "")
        if path.startswith("login.ha"):
            if request.method != "POST":
                return html(login_page)
            sent = parse_qs(request.body.decode() if isinstance(request.body, bytes) else request.body)
            if sent["hashpassword"] == [hashlib.md5(b"stickerabc123").hexdigest()]:  # noqa: S324 - router protocol
                return html("", status=302, headers={"location": "/cgi-bin/home.ha", "set-cookie": "sid=fallback; Path=/"})
            return html("<html><body>Login Failed</body></html>")
        if "sid=fallback" in cookie:
            return html(f"<html><body>{TABLE_HEADER_HTML.get(path.removesuffix('.ha'), 'ok')}</body></html>")
        return html(login_page)  # the cached session died with the reset: bounced to login

    transport = FakeTransport(router)
    built: list[str] = []

    def factory(options, access_code, *, on_session_wait=None, user_agent="bgw/0.1.0"):
        built.append(access_code)
        return BGW320Client(origin, access_code=access_code, timeout_ms=1000, user_agent="test", transport=transport)

    def fetch(client, page, include_secrets=False):
        client.get_cgi_page(page)  # the real auth path: bounce, re-login, raise on a refused code
        return ParsedPageResult(page, True, 200, snapshot_fetcher["pages"][page])

    monkeypatch.setattr(cli, "_client_factory", factory)
    monkeypatch.setattr(cli, "fetch_parsed_page", fetch)
    monkeypatch.setenv("BGW_ACCESS_CODE", "primary")
    if fallback:
        monkeypatch.setenv("BGW_FALLBACK_ACCESS_CODE", fallback)
    paths = session.session_paths(origin)
    session._ensure_private_dir(paths.cache.parent)
    now = session._now_ms()
    session._write_json(paths.cache, {
        "origin": origin, "authenticated": True, "cookies": {"sid": "cached"}, "version": 1,
        "cachedAt": now, "expiresAt": now + 600_000,
    })
    code, out_text, err = run(capsys, ["autorestore", str(out), "--host", origin, "--json"])
    return code, json.loads(out_text), err, built, transport, paths


def _login_posts(transport):
    return [call for call in transport.calls if call.startswith("POST /cgi-bin/login.ha")]


def test_autorestore_falls_back_when_the_cached_primary_session_is_rejected(
    capsys, fake, snapshot_fetcher, tmp_path, monkeypatch
):
    """After a factory reset the cached primary session is dead and the primary code is refused.
    Importing the cache made login() a no-op, so the rejection only shows on the first page read;
    the run must still try the fallback code then, exactly as if nothing had been cached."""
    code, payload, err, built, transport, paths = _autorestore_over_a_dead_cached_session(
        capsys, snapshot_fetcher, tmp_path, monkeypatch, "sticker"
    )

    # The router still matches the dump: the fallback login alone is no reset (warning only).
    assert code == 0 and payload["status"] == "no-reset" and payload["usedFallbackCode"] is True, err
    assert payload["warnings"] and "BGW_ACCESS_CODE" in payload["warnings"][0]
    assert built == ["primary", "sticker"]
    # The bounce's re-login and the uncached-style primary login are both refused; the fallback succeeds.
    assert len(_login_posts(transport)) == 3
    assert not any(call.startswith("POST") and "login.ha" not in call for call in transport.calls)
    assert json.loads(paths.cache.read_text())["cookies"] == {"sid": "fallback"}


@pytest.mark.parametrize("fallback", [None, "primary"])
def test_autorestore_dead_cached_session_without_a_usable_fallback_stays_fatal(
    capsys, fake, snapshot_fetcher, tmp_path, monkeypatch, fallback
):
    code, _, err, built, transport, paths = _autorestore_over_a_dead_cached_session(
        capsys, snapshot_fetcher, tmp_path, monkeypatch, fallback
    )

    assert code == 2 and "Login failed" in err
    assert built == ["primary"] and len(_login_posts(transport)) == 1, "no second login without a usable fallback"
    assert not paths.cache.exists(), "the refused cached session is forgotten"


def test_help_text_states_the_applied_and_autorestore_exit_contract():
    text = " ".join(cli.help_text().split())
    assert "accepted the POST" not in text
    assert "applied means the gateway acknowledged the save (Changes saved) and the requested" in text
    assert "Exit 0 converged, 1 not converged (the next timer run retries), 2 error" in text
    assert "recovery checkpoint" in text and "another unfinished recovery" in text
    assert "the same failure ended three consecutive runs" in text


def test_autorestore_help_does_not_call_the_fallback_code_a_reset_signal():
    text = " ".join(cli.help_text().split())
    assert "itself a reset signal" not in text
    assert "needing it alone is no reset signal" in text and "three consecutive runs" in text


class _Observed:
    def __init__(self, attempts, responses=0):
        self.attempts, self.responses, self.last_status = attempts, responses, None


@pytest.mark.parametrize("primary_posts,fallback_posts,expected", [
    (0, 1, True),    # the write went out on the fallback client: the interrupt report must say so
    (0, 0, False),   # nothing sent anywhere
    (1, 0, True),    # the primary client's POST still counts (conservative)
])
def test_ctrl_c_after_a_fallback_login_reads_the_write_evidence_of_both_clients(
    capsys, fake, snapshot_fetcher, tmp_path, monkeypatch, primary_posts, fallback_posts, expected
):
    built: list[FakeClient] = []

    class CodeClient(FakeClient):
        def __init__(self, code, posts):
            super().__init__()
            self.code, self.sent = code, posts

        def login(self, *args, **kwargs):
            if self.code != "sticker":
                raise RouterAuthError("Login failed: the access code was rejected.")
            self.logged_in = True

        def observe_writes(self):
            return _Observed(self.sent)

    def factory(options, access_code, *, on_session_wait=None, user_agent="bgw/0.1.0"):
        client = CodeClient(access_code, fallback_posts if access_code == "sticker" else primary_posts)
        built.append(client)
        return client

    monkeypatch.setattr(cli, "_client_factory", factory)
    monkeypatch.setenv("BGW_ACCESS_CODE", "primary")
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    built.clear()
    monkeypatch.setenv("BGW_FALLBACK_ACCESS_CODE", "sticker")

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_fetch_snapshot_pages", interrupted)
    code, payload, _ = run_json(
        capsys, ["autorestore", str(out), "--commit", "--confirm", "RESTORE", "--json"]
    )
    assert [c.code for c in built] == ["primary", "sticker"]
    assert code == 130 and payload["errorType"] == "Interrupted"
    assert payload["writeAttempted"] is expected
    assert all(c.posts == [] for c in built), "the fake transport counted the POSTs; none went through the fake"


def test_ctrl_c_on_the_primary_client_alone_is_unchanged(capsys, fake, snapshot_fetcher, tmp_path, monkeypatch):
    class Counting(FakeClient):
        def observe_writes(self):
            return _Observed(1)

    fake["client"] = Counting()
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_fetch_snapshot_pages", interrupted)
    code, payload, _ = run_json(
        capsys, ["autorestore", str(out), "--commit", "--confirm", "RESTORE", "--json"]
    )
    assert code == 130 and payload["writeAttempted"] is True and payload["writeAttempts"] == 1


@pytest.mark.parametrize("interrupt_on", ["write POST", "first page read"])
def test_ctrl_c_on_the_fallback_client_reads_its_real_write_evidence_over_the_wire(
    tmp_env, clock, capsys, monkeypatch, interrupt_on
):
    """No stubbed observe_writes: the primary client's code is refused on a scripted wire, the real
    fallback client logs in and is interrupted either in its configuration POST (the counter moves
    before the transport runs) or before anything was sent."""
    from save_helpers import FakeTransport, form, html

    from bgwcli.client import BGW320Client
    from bgwcli.dumpfile import write_dump_file
    from bgwcli.snapshot import Snapshot, SnapshotMeta

    login_page = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'

    def refused(request, _n):
        return html(login_page)  # the login GET and the login POST both answer the Login page

    def fallback_gateway(request, _n):
        if request.url.endswith("login.ha"):
            return html("", 302, {"location": "/cgi-bin/home.ha"}) if request.method == "POST" else html(login_page)
        if request.method == "POST" or interrupt_on == "first page read":
            raise KeyboardInterrupt
        return html(form("dosprotect", "old"))

    wires = {"primary": FakeTransport(refused), "sticker": FakeTransport(fallback_gateway)}

    def factory(options, access_code, *, on_session_wait=None, user_agent="bgw/0.1.0"):
        return BGW320Client(
            "http://router.local", access_code=access_code, timeout_ms=1000, insecure_tls=True,
            user_agent="test", transport=wires[access_code],
        )

    monkeypatch.setattr(cli, "_client_factory", factory)
    monkeypatch.setenv("BGW_ACCESS_CODE", "primary")
    monkeypatch.setenv("BGW_FALLBACK_ACCESS_CODE", "sticker")
    path = tmp_env / "dump.json"
    write_dump_file(path, Snapshot(SnapshotMeta("", "", "router.local"), forms={"dosprotect": {"setting": "new"}}))
    code, payload, _ = run_json(
        capsys, ["autorestore", str(path), "--include", "dosprotect", "--commit", "--confirm", "RESTORE", "--json"]
    )
    assert code == 130 and payload["errorType"] == "Interrupted"
    sent = [r for r in wires["sticker"].requests if r.method == "POST" and not r.url.endswith("login.ha")]
    assert not [r for r in wires["primary"].requests if r.method == "POST" and not r.url.endswith("login.ha")]
    if interrupt_on == "write POST":
        assert len(sent) == 1
        assert payload["writeAttempted"] is True and payload["writeAttempts"] == 1
        assert payload["writeResponseReceived"] is False
    else:
        assert sent == []
        assert payload["writeAttempted"] is False
