"""A failure before any POST reports the same write evidence on every save path: the generic page, the
Wi-Fi step and the LAN-move step all say `writeAttempted/writePerformed/acknowledgementObserved/
committed: false` and the same "nothing was sent" sentence, for every pre-send fault."""

from __future__ import annotations

import json

import pytest
from save_helpers import client_with, form, html

from bgwcli import cli
from bgwcli.client import session_pool_full_error

WAIT = "<html><head><title>Please wait</title></head><body>Please wait...</body></html>"
LOGIN = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'
DEVICES = (
    "<html><head><title>Devices</title></head><body>"
    '<form method="post" action="/cgi-bin/devices.ha"><input type="hidden" name="nonce" value="abcd01">'
    '<input type="submit" name="Clear" value="Clear Device List"></form></body></html>'
)
KEYS = ("writeAttempted", "writeResponseReceived", "writePerformed", "acknowledgementObserved", "committed")
EXPECTED = {key: False for key in KEYS} | {"nothingSent": True}
FAULTS = ("please-wait", "pool-full", "auth-failure")

# (page, field, live value, `set` argument, confirm token)
PAGES = {
    "generic": ("dosprotect", "setting", "old", "setting=new", "DOSPROTECT"),
    "wifi": ("wconfig", "maxclients", "80", "maxclients=79", "WCONFIG"),
    "lan": ("dhcpserver", "ipaddr", "192.168.1.254", "ipaddr=192.168.1.200", "DHCPSERVER"),
}


def _handler(page, field, live, fault, source_body=None):
    """The plan read (first GET) serves the page; every later GET is the pre-send fault."""
    gets = []

    def handle(request, _n):
        path = request.url.split("?")[0]
        if request.method == "POST":
            if path.endswith("login.ha"):
                return html(LOGIN)  # every login is refused
            raise AssertionError("a pre-send fault must not be followed by a configuration POST")
        gets.append(request.url)
        if len(gets) == 1:
            return html(source_body if source_body is not None else form(page, live, field))
        if fault == "please-wait":
            return html(WAIT)
        if fault == "pool-full":
            raise session_pool_full_error(waited_ms=2300, retry_count=4)
        return html(LOGIN)

    return handle


def _run(monkeypatch, capsys, argv, handler):
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--commit", "--json"])
    output = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.split("?")[0].endswith("login.ha")]
    return code, output, posts


def _evidence(output):
    """The whole evidence shape: the four keys (absent stays absent) and the nothing-sent sentence."""
    text = " ".join(str(output.get(key) or "") for key in ("error", "warning"))
    return {**{key: output.get(key, "<absent>") for key in KEYS}, "nothingSent": cli.NO_WRITE_SENT in text}


@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("operation", ["set", "submit"])
def test_every_save_path_reports_the_same_presend_evidence(
    clock, tmp_env, capsys, monkeypatch, operation, fault
):
    shapes = {}
    for label, (page, field, live, set_arg, token) in PAGES.items():
        # A pool-full answer starts a cooldown in the session cache: each page gets a fresh one.
        monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(tmp_env / f"cache-{label}"))
        argv = ([operation, page, set_arg] if operation == "set" else [operation, page, "Save"])
        code, output, posts = _run(
            monkeypatch, capsys, [*argv, "--confirm", token], _handler(page, field, live, fault)
        )
        assert code == 2, (label, output)
        assert posts == [], label
        shapes[label] = _evidence(output)
    assert shapes["generic"] == EXPECTED
    assert shapes["wifi"] == shapes["generic"]
    assert shapes["lan"] == shapes["generic"]


@pytest.mark.parametrize("fault", FAULTS)
def test_action_reports_the_same_presend_evidence_as_a_save(clock, tmp_env, capsys, monkeypatch, fault):
    code, output, posts = _run(
        monkeypatch, capsys, ["action", "clear-device-list", "--confirm", "CLEAR-DEVICES"],
        _handler("devices", "", "", fault, source_body=DEVICES),
    )
    assert code == 2 and posts == []
    assert _evidence(output) == EXPECTED
