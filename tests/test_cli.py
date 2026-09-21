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
from page_builders import apphosting_page, button, dosprotect_page, page, select, services_page, sysinfo_page

from bgwcli import cli
from bgwcli.client import session_pool_full_error
from bgwcli.errors import RouterConnectionError
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


class FakeClient:
    def __init__(self, pages=None, *, post_status=200, post_location=None, post_body=DIAG_HTML):
        self.pages = dict(PAGES if pages is None else pages)
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.logged_in = False
        self.post_status = post_status
        self.post_location = post_location
        self.post_body = post_body
        self.origin = "https://router.local"

    # -- BGW320Client surface used by cli/session ---------------------------------------------
    def check(self):
        return {"host": "router.local", "reachable": True, "title": "Site Map", "authenticated": False}

    def login(self):
        self.logged_in = True

    def get_cgi_page(self, page, *, auth=True):
        self.gets.append(page)
        body = self.pages.get(page)
        if body is None:
            raise RouterConnectionError(f"{page}.ha timed out")
        return HttpResponse(200, "OK", {}, body, f"{self.origin}/cgi-bin/{page}.ha")

    def post_cgi_page(self, page, fields):
        self.posts.append((page, dict(fields)))
        headers = {"location": self.post_location} if self.post_location else {}
        return HttpResponse(self.post_status, "OK", headers, self.post_body, f"{self.origin}/cgi-bin/{page}.ha")

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


ALL_COMMANDS = [
    "check", "auth", "sitemap", "coverage", "tabs", "actions", "session", "action", "sweep", "scan", "schema",
    "audit", "readiness", "section", "device", "broadband", "home-network", "voice", "firewall", "diagnostics",
    "page", "inspect", "devices", "wifi", "nat", "logs", "set", "submit", "status", "dump", "diff", "restore",
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
    assert "--timeout must be a finite number greater than or equal to 1." in err


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
    run(capsys, ["check", "--host", "http://flag.local", "--timeout", "7000", "--strict-tls", "--wait-for-session",
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
    assert fake["client"].logged_in is True
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
    code, payload, _ = run_json(capsys, ["logs", "--json", "--limit", "1"])
    assert code == 0 and len(payload) == 1
    code, out, _ = run(capsys, ["logs"])
    assert code == 0 and "1.1.1.1" in out
    fake["client"].pages.pop("logs")
    code, payload, _ = run_json(capsys, ["logs", "--json"])
    assert code == 2 and payload["ok"] is False and payload["page"] == "logs"


def test_devices_and_nat(capsys, fake):
    code, payload, _ = run_json(capsys, ["devices", "--json", "--all"])
    assert code == 0 and payload["devices"][0]["mac"] == "aa:bb:cc:dd:ee:01"
    code, out, _ = run(capsys, ["devices"])
    assert code == 0 and "aa:bb:cc:dd:ee:01" in out
    code, payload, _ = run_json(capsys, ["nat", "--json"])
    assert code == 2 and payload["page"] == "nattable"


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
    code, payload, err = run_json(capsys, ["sweep", "--pages", "diag,logs", "--delay", "0", "--json"])
    assert code == 0
    assert [p["page"] for p in payload] == ["logs", "diag"] or [p["page"] for p in payload] == ["diag", "logs"]
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
        "waitedMs": 42, "retryCount": 3,
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
        "waitedMs": 7, "retryCount": 2,
    }
    cooldowns = list((tmp_env / "cache").glob("*.cooldown.json"))
    assert len(cooldowns) == 1
    cooldown = json.loads(cooldowns[0].read_text())
    assert cooldown["waitedMs"] == 7 and cooldown["retryCount"] == 2
    # device status (home page) goes through the same soft fetcher
    code, out, err = run(capsys, ["device", "status"])
    assert code == 2 and out == "" and err.startswith("Router web session pool is full")


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
        pages[name] = page(name, title=name)
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
        return ParsedPageResult(page, True, 200, parsed)

    monkeypatch.setattr(cli, "fetch_parsed_page", fetch)
    return holder


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


def test_restore_commit_posts_and_exits_1_when_not_converged(capsys, fake, snapshot_fetcher, tmp_path):
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    snapshot_fetcher["pages"]["services"] = services_page(rows=[("custom_ssh", "2483-2483", "22", "TCP")])
    fake["client"].post_body = "<html><title>Custom Services</title></html>"

    code, payload, err = run_json(capsys, ["restore", str(out), "--commit", "--confirm", "RESTORE", "--json"])
    assert code == 1  # the fake router never changes, so the re-diff still shows Mosh missing
    assert set(payload) == {"execution", "diff", "verificationFailures", "operation", "missingPages"}
    assert payload["execution"]["steps"][0]["status"] == "applied"
    assert payload["execution"]["steps"][0]["location"] is None
    assert payload["diff"]["identical"] is False and payload["verificationFailures"] == []
    assert payload["operation"]["committed"] is True
    assert fake["client"].posts and fake["client"].posts[0][0] == "services"
    assert err == ""

    code, text, err = run(capsys, ["restore", str(out), "--commit", "--confirm", "RESTORE"])
    assert code == 1
    assert "[1] applied services" in text and "restore committed" in text and "- missing service Mosh" in text


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
    assert code == 1
    assert payload["diff"] is None
    assert payload["verificationFailures"][0]["page"] == "services"
    calls["services"] = 0
    code, text, _ = run(capsys, ["restore", str(out), "--commit", "--confirm", "RESTORE"])
    assert code == 1
    assert "Post-restore verification incomplete" in text and "Page unavailable: services" in text


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

    def boom():
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


def test_restore_include_plans_a_skip_step_for_a_page_missing_from_the_dump(capsys, fake, snapshot_fetcher, tmp_path):
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    snapshot_fetcher["pages"]["services"] = services_page(rows=[("custom_ssh", "2483-2483", "22", "TCP")])

    code, payload, err = run_json(capsys, ["restore", str(out), "--include", "dhcpserver", "--json"])
    assert code == 0
    assert payload["missingPages"] == ["dhcpserver"]
    assert err == "warning: page 'dhcpserver' is not present in the dump; nothing to compare/restore\n"
    assert [(s["page"], s["kind"]) for s in payload["steps"]] == [("dhcpserver", "skip")]
    assert payload["steps"][0]["description"] == "page 'dhcpserver' requested with --include but not present in the dump"
    assert payload["operation"]["dryRun"] is True and fake["client"].posts == []

    code, text, err = run(capsys, ["restore", str(out), "--include", "dhcpserver"])
    assert code == 0 and "skip" in text and "not present in the dump" in text
    assert err.startswith("warning: page 'dhcpserver' is not present in the dump")
    assert "add-service" not in text  # services were not selected

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


def test_set_commit_posts_the_save_button_and_verifies_the_change(capsys, fake):
    fake["client"] = FakeClient({**PAGES, "dosprotect": DOSPROTECT_HTML("on")}, post_status=302, post_location="/cgi-bin/dosprotect.ha")
    argv = ["set", "dosprotect", "icmp_downstream_echo_rqst_drop_wan=on", "--commit", "--confirm", "DOSPROTECT", "--json"]
    code, payload, _ = run_json(capsys, argv)
    assert code == 0
    page, fields = fake["client"].posts[0]
    assert page == "dosprotect" and fields["icmp_downstream_echo_rqst_drop_wan"] == "on" and fields["Save"] == "Save"
    assert payload["committed"] is True and payload["verified"] is True
    assert fake["client"].gets.count("dosprotect") == 2  # plan fetch + verification re-read


def test_set_commit_reports_a_discarded_change_with_exit_1(capsys, fake):
    fake["client"] = FakeClient({**PAGES, "dosprotect": DOSPROTECT_HTML("off")}, post_status=302, post_location="/cgi-bin/dosprotect.ha")
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
    fake["client"] = FakeClient({**PAGES, "wconfig": WCONFIG_HTML, "wifiwarn_advanced": WIFIWARN_HTML}, post_status=302, post_location="/cgi-bin/wifiwarn_advanced.ha")
    argv = ["set", "wconfig", "maxclients=81", "--commit", "--confirm", "WCONFIG", "--json"]
    code, payload, _ = run_json(capsys, argv)
    assert [p for p, _ in fake["client"].posts] == ["wconfig", "wconfig"]
    assert fake["client"].posts[0][1]["Save"] == "Save..." and fake["client"].posts[1][1] == {"Continue": "Continue"}
    assert "wifiwarn_advanced" in fake["client"].gets
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
    fake["client"] = FakeClient({**PAGES, "wconfig": WCONFIG_SCAN_HTML, "wifiwarn_advanced": WIFIWARN_HTML}, post_status=302, post_location="/cgi-bin/wifiwarn_advanced.ha")
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
    fake["client"] = FakeClient({**PAGES, "wconfig": WCONFIG_SCAN_HTML, "wifiwarn_advanced": WIFIWARN_HTML}, post_status=302, post_location="/cgi-bin/wifiwarn_advanced.ha")
    code, payload, _ = run_json(capsys, ["submit", "wconfig", "chanscan5", "--commit", "--confirm", "WCONFIG", "--json"])
    assert code == 0 and payload["committed"] is True
    assert [p for p, _ in fake["client"].posts] == ["wconfig", "wconfig"]
    assert fake["client"].posts[1][1] == {"Continue": "Continue"}


def test_timeout_flag_marks_the_timeout_explicit_and_default_does_not(capsys, fake):
    run(capsys, ["check"])
    assert fake["options"].timeout_ms == 15000 and fake["options"].timeout_explicit is False
    run(capsys, ["check", "--timeout", "2500"])
    assert fake["options"].timeout_ms == 2500 and fake["options"].timeout_explicit is True
