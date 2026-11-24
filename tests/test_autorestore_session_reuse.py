"""The autorestore timer fires every 15 minutes, far past the 120 s session-cache lifetime other
commands use. Each run used to log in afresh and leave its session to the gateway's idle expiry, so
the runs filled the gateway's small web-session pool. autorestore now tries its cached session once
whatever its age: a live one costs no login, a dead one costs one extra page read and one login,
never a write."""

from __future__ import annotations

import pytest
from save_helpers import FakeTransport, form, html

from bgwcli import autorestore, cli, session
from bgwcli.client import BGW320Client
from bgwcli.dumpfile import write_dump_file
from bgwcli.snapshot import Snapshot, SnapshotMeta

PAGE = "dosprotect"
LOGIN = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'
FIFTEEN_MINUTES_MS = 15 * 60 * 1000


class Gateway:
    """Logs in by cookie; `valid` holds the session ids it still accepts."""

    def __init__(self, *, keeps_sessions: bool):
        self.keeps_sessions = keeps_sessions
        self.valid: set[str] = set()
        self.logins = 0
        self.writes = 0
        self.page_reads = 0

    def handle(self, request, _n):
        cookie = request.headers.get("Cookie", "")
        sid = cookie.partition("sid=")[2].split(";")[0] if "sid=" in cookie else ""
        if request.url.endswith("/cgi-bin/login.ha"):
            if request.method == "POST":
                self.logins += 1
                new = f"s{self.logins}"
                self.valid.add(new)
                return html("", 302, {"location": "/cgi-bin/home.ha", "set-cookie": f"sid={new}; Path=/"})
            return html(LOGIN)
        if request.method == "POST":
            self.writes += 1
            return html("", 500)
        if sid not in self.valid:
            return html(LOGIN)
        self.page_reads += 1
        return html(form(PAGE, "new"))

    def forget_sessions(self):
        if not self.keeps_sessions:
            self.valid.clear()


@pytest.fixture
def baseline(tmp_env):
    path = tmp_env / "baseline.json"
    write_dump_file(path, Snapshot(SnapshotMeta("", "", "router.local"), forms={PAGE: {"setting": "new"}}))
    return path


def _three_timer_runs(monkeypatch, baseline, gateway, argv_extra=()):
    monkeypatch.setattr(autorestore, "_sleep", lambda seconds: None)
    now = {"ms": 10_000_000}
    monkeypatch.setattr(session, "_now_ms", lambda: now["ms"])
    codes = []
    for _ in range(3):
        transport = FakeTransport(gateway.handle)
        client = BGW320Client(
            "http://router.local", access_code="12345", timeout_ms=1000, insecure_tls=True,
            user_agent="test", transport=transport,
        )
        monkeypatch.setattr(cli, "_client_factory", lambda *a, _c=client, **kw: _c)
        codes.append(cli.main(["autorestore", str(baseline), "--include", PAGE, "--host", "http://router.local",
                               *argv_extra]))
        now["ms"] += FIFTEEN_MINUTES_MS
        gateway.forget_sessions()
    return codes


def test_a_still_valid_cached_session_is_reused_across_timer_runs(tmp_env, monkeypatch, baseline, capsys):
    gateway = Gateway(keeps_sessions=True)
    codes = _three_timer_runs(monkeypatch, baseline, gateway)
    assert codes == [0, 0, 0], capsys.readouterr()
    assert gateway.logins == 1, "the first run logs in; the next two reuse its session"


def test_a_dead_cached_session_costs_one_read_and_one_login_per_run(tmp_env, monkeypatch, baseline, capsys):
    gateway = Gateway(keeps_sessions=False)
    codes = _three_timer_runs(monkeypatch, baseline, gateway)
    assert codes == [0, 0, 0], capsys.readouterr()
    assert gateway.logins == 3, "one login per run, never more"
    assert gateway.writes == 0, "nothing is written on a dead session"


def test_other_commands_keep_the_configured_cache_lifetime(tmp_env, monkeypatch, baseline, capsys):
    gateway = Gateway(keeps_sessions=True)
    monkeypatch.setattr(autorestore, "_sleep", lambda seconds: None)
    now = {"ms": 10_000_000}
    monkeypatch.setattr(session, "_now_ms", lambda: now["ms"])
    for _ in range(2):
        client = BGW320Client(
            "http://router.local", access_code="12345", timeout_ms=1000, insecure_tls=True,
            user_agent="test", transport=FakeTransport(gateway.handle),
        )
        monkeypatch.setattr(cli, "_client_factory", lambda *a, _c=client, **kw: _c)
        assert cli.main(["diff", str(baseline), "--include", PAGE, "--host", "http://router.local"]) == 0
        now["ms"] += FIFTEEN_MINUTES_MS
    assert gateway.logins == 2, "diff past the 120 s lifetime logs in again, as before"
