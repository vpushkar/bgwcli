"""set ipalloc alloc_<mac>=<ip> verifies a reservation from the re-read page's Fixed Allocation rows.

After a save the gateway stops rendering the `alloc_<mac>` select, so an acknowledged save whose wait could
not read the page (Please-wait polls to the deadline) must not be judged by that vanished select: a row with
the requested mac and ip verifies it (exit 0), a readable table without it is a truthful row-based mismatch
(exit 1), and an unreadable re-read stays unverifiable (exit 2). Verification is GETs only: one POST."""

from __future__ import annotations

import json

import pytest
from integration_html import allocation_saved_html, entry_page_html
from save_helpers import SAVED_RED, client_with, html

from bgwcli import cli

MAC = "02:0a:0b:0c:0d:02"
OTHER_MAC = "02:0a:0b:0c:0d:09"
IP = "192.168.1.64"
PLEASE_WAIT = "<html><head><title>Please wait</title></head><body>Loading configuration...</body></html>"
HEADER = (
    "<tr><th>IPv4 Address / Name</th><th>MAC Address</th><th>Status</th><th>Allocation</th><th>Action</th></tr>"
)


def table(*rows: tuple[str, str], dhcp: tuple[tuple[str, str], ...] = ()) -> str:
    body = "".join(
        f"<tr><td>{ip}</td><td>{mac}</td><td>on</td><td>{kind}</td>"
        f'<td><input type="submit" name="Allocate_{mac}" value="Allocate"></td></tr>'
        for kind, group in (("Fixed Allocation", rows), ("DHCP Allocation", dhcp))
        for ip, mac in group
    )
    return (
        '<html><head><title>IP Allocation</title></head><body><form action="/cgi-bin/ipalloc.ha">'
        f'<input type="hidden" name="nonce" value="n"><table>{HEADER}{body}</table></form></body></html>'
    )


HOLDS = table((IP, MAC))
WITHOUT_ROW = table(("192.168.1.70", OTHER_MAC))
WRONG_IP = table(("192.168.1.99", MAC))
# One body per poll shape letter: W = Please-wait, M = a readable table that lacks the reservation.
SHAPES = {"W": PLEASE_WAIT, "M": WITHOUT_ROW}


def _run(
    monkeypatch, capsys, clock, polls, reread, *, ack_body=SAVED_RED + PLEASE_WAIT, verb=("set", "ipalloc"),
    value=IP, gets=None,
):
    """`set ipalloc` (or `submit ipalloc Save`, via `verb`) with an acknowledged POST. Polls (one letter
    per shape) are served while the fake clock is inside the wait window; afterwards the verification
    re-read gets `reread`."""
    state = {"posted": False, "served": 0}
    wait_window = 3.0  # install_clock default timeout

    def handler(request, _n):
        if request.method == "POST":
            state["posted"] = True
            return html(ack_body)
        if not state["posted"]:
            return html(entry_page_html(MAC, [IP]).replace('name="nonce" value="n"', 'name="nonce" value="ab12"'))
        if polls and clock.now < wait_window:
            index = min(state["served"], len(polls) - 1)
            state["served"] += 1
            return html(SHAPES[polls[index]])
        return html(reread)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*verb, f"alloc_{MAC}={value}", "--commit", "--confirm", "IPALLOC", "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    if gets is not None:
        # GETs of the IP Allocation page issued after the POST (wait polls plus any verification re-read).
        after = wire.requests[wire.requests.index(posts[0]) + 1:]
        gets.extend(r for r in after if r.method == "GET" and "ipalloc" in r.url)
    return code, out, posts


RELEASED = table(dhcp=((IP, MAC),))
STILL_FIXED = table(("192.168.1.99", MAC))
SHAPES["R"] = RELEASED
SHAPES["F"] = STILL_FIXED


def test_release_verifies_when_the_device_is_listed_without_a_fixed_allocation(
    tmp_env, clock, monkeypatch, capsys
):
    code, out, posts = _run(monkeypatch, capsys, clock, "", RELEASED, value="normal")
    assert len(posts) == 1
    assert out["committed"] is True and out["outcome"] == "applied"
    assert code == 0 and out["verified"] is True, json.dumps(out)
    assert not out.get("mismatches")


def test_release_that_left_the_fixed_allocation_is_a_row_mismatch(tmp_env, clock, monkeypatch, capsys):
    code, out, posts = _run(monkeypatch, capsys, clock, "", STILL_FIXED, value="normal")
    assert len(posts) == 1
    assert code == 1 and out["verified"] is False
    live = json.dumps(out["mismatches"])
    assert f"Fixed Allocation row {MAC} -> 192.168.1.99" in live
    assert "<absent>" not in json.dumps(out)


def test_release_with_no_row_for_the_device_cannot_be_confirmed(tmp_env, clock, monkeypatch, capsys):
    code, out, posts = _run(monkeypatch, capsys, clock, "", WITHOUT_ROW, value="normal")
    assert len(posts) == 1
    assert out["committed"] is True
    assert code == 1 and out["verified"] is False
    assert "the release could not be confirmed" in json.dumps(out["mismatches"])
    assert "<absent>" not in json.dumps(out)


SUBMIT = ("submit", "ipalloc", "Save")


def test_submit_release_is_verified_like_set(tmp_env, clock, monkeypatch, capsys):
    set_code, set_out, set_posts = _run(monkeypatch, capsys, clock, "R", RELEASED, value="normal")
    gets: list = []
    code, out, posts = _run(monkeypatch, capsys, clock, "R", RELEASED, verb=SUBMIT, value="normal", gets=gets)
    assert len(posts) == len(set_posts) == 1
    assert code == set_code == 0
    # The wait read the table once and saw the release there; a submit that reported success without
    # ever reading the table (the old gap) would show no GET after the POST.
    assert len(gets) == 1
    assert out["committed"] is True and out["outcome"] == "applied", json.dumps(out)
    # submit reports `verified` only when a re-read ran; here the wait itself saw the release, so it is
    # absent (like a submit IPv4 reservation) and never false. set reports the same outcome and exit.
    assert out.get("verified") is not False and set_out["verified"] is True
    assert {k: out.get(k) for k in ("committed", "outcome")} == {k: set_out.get(k) for k in ("committed", "outcome")}
    assert not out.get("mismatches") and "<absent>" not in json.dumps(out)


@pytest.mark.parametrize(
    ("reread", "code", "verified", "text"),
    [
        (RELEASED, 0, True, None),
        (STILL_FIXED, 1, False, f"Fixed Allocation row {MAC} -> 192.168.1.99"),
        (WITHOUT_ROW, 1, False, "the release could not be confirmed"),
    ],
)
def test_submit_release_still_fixed_in_the_wait_is_decided_by_the_reread(
    tmp_env, clock, monkeypatch, capsys, reread, code, verified, text
):
    got, out, posts = _run(monkeypatch, capsys, clock, "F", reread, verb=SUBMIT, value="normal")
    assert len(posts) == 1
    assert out["committed"] is True
    assert got == code and out["verified"] is verified, json.dumps(out)
    if text:
        assert text in json.dumps(out["mismatches"])
    else:
        assert not out.get("mismatches")


def test_submit_release_never_reports_success_for_a_still_fixed_row(tmp_env, clock, monkeypatch, capsys):
    code, out, posts = _run(monkeypatch, capsys, clock, "F", STILL_FIXED, verb=SUBMIT, value="normal")
    assert len(posts) == 1 and code != 0 and out.get("verified") is not True


def test_submit_release_unreadable_to_the_deadline_is_exit_two(tmp_env, clock, monkeypatch, capsys):
    code, out, posts = _run(monkeypatch, capsys, clock, "W", PLEASE_WAIT, verb=SUBMIT, value="normal")
    assert len(posts) == 1
    assert code == 2 and out["committed"] is True and out.get("verified") is None
    # The warning is the re-read's verdict: the wait's own deadline text is replaced on this path.
    assert "could not re-read ipalloc" in out["warning"]


@pytest.mark.parametrize("verb", [("set", "ipalloc"), SUBMIT], ids=["set", "submit"])
def test_never_acknowledged_release_wait_names_the_release_in_its_deadline(tmp_env, clock, monkeypatch, capsys, verb):
    # No acknowledgement ever arrives, so no re-read runs and the wait's own deadline text reaches the user.
    code, out, posts = _run(
        monkeypatch, capsys, clock, "W", PLEASE_WAIT, verb=verb, value="normal", ack_body=PLEASE_WAIT
    )
    assert len(posts) == 1
    assert code == 2 and out["committed"] is False and out["outcome"] == "failed"
    assert "Changes saved and allocation release on ipalloc" in out["warning"]
    assert "fixed allocation" not in out["warning"]


def test_set_release_seen_in_the_wait_needs_no_reread(tmp_env, clock, monkeypatch, capsys):
    gets: list = []
    code, out, posts = _run(
        monkeypatch, capsys, clock, "", STILL_FIXED, value="normal", gets=gets, ack_body=SAVED_RED + RELEASED
    )
    assert len(posts) == 1
    assert code == 0 and out["verified"] is True
    assert gets == []  # the acknowledgement body already showed the release; a re-read would see STILL_FIXED


def test_release_with_an_unreadable_reread_is_still_exit_two(tmp_env, clock, monkeypatch, capsys):
    code, out, posts = _run(monkeypatch, capsys, clock, "", PLEASE_WAIT, value="normal")
    assert len(posts) == 1
    assert code == 2 and out.get("verified") is None and out["outcome"] == "applied"


def test_reread_showing_the_row_verifies_the_reservation(tmp_env, clock, monkeypatch, capsys):
    code, out, posts = _run(monkeypatch, capsys, clock, "W", HOLDS)
    assert len(posts) == 1
    assert code == 0 and out["outcome"] == "applied" and out["verified"] is True
    assert not out.get("mismatches")


def test_readable_reread_without_the_row_is_a_row_based_mismatch(tmp_env, clock, monkeypatch, capsys):
    code, out, posts = _run(monkeypatch, capsys, clock, "W", WITHOUT_ROW)
    assert len(posts) == 1
    assert code == 1 and out["verified"] is False
    live = json.dumps(out["mismatches"])
    assert MAC in live and IP in live
    assert "<absent>" not in live


def test_reread_with_the_mac_on_another_ip_names_that_row(tmp_env, clock, monkeypatch, capsys):
    code, out, posts = _run(monkeypatch, capsys, clock, "W", WRONG_IP)
    assert len(posts) == 1
    assert code == 1 and out["verified"] is False
    live = json.dumps(out["mismatches"])
    assert "192.168.1.99" in live and "<absent>" not in live


def test_unreadable_reread_is_unverifiable_exit_two(tmp_env, clock, monkeypatch, capsys):
    code, out, posts = _run(monkeypatch, capsys, clock, "W", PLEASE_WAIT)
    assert len(posts) == 1
    assert code == 2 and out.get("verified") is None and out["outcome"] == "applied"
    assert "could not re-read ipalloc" in json.dumps(out)
    assert out["acknowledgementObserved"] is True


def test_control_less_reread_is_unverifiable(tmp_env, clock, monkeypatch, capsys):
    controlless = (
        '<html><body><form action="/cgi-bin/ipalloc.ha"><input type="hidden" name="nonce" value="n">'
        "</form></body></html>"
    )
    code, out, posts = _run(monkeypatch, capsys, clock, "W", controlless)
    assert len(posts) == 1
    assert code == 2 and out.get("verified") is None


def test_reread_with_a_control_but_no_table_header_is_unverifiable_not_an_empty_table(
    tmp_env, clock, monkeypatch, capsys
):
    headerless = (
        '<html><body><form action="/cgi-bin/ipalloc.ha"><input type="hidden" name="nonce" value="n">'
        '<input type="submit" name="Save" value="Save"></form></body></html>'
    )
    code, out, posts = _run(monkeypatch, capsys, clock, "W", headerless)
    assert len(posts) == 1
    assert code == 2 and out.get("verified") is None
    assert not out.get("mismatches")
    assert "not read as an empty section" in json.dumps(out)


@pytest.mark.parametrize("verb", [("set", "ipalloc"), ("submit", "ipalloc", "Save")], ids=["set", "submit"])
def test_header_only_reread_with_controls_is_a_readable_empty_table_mismatch(
    tmp_env, clock, monkeypatch, capsys, verb
):
    # The table header is visible (a legitimate empty section) and the editor controls are rendered,
    # but no data row exists: the reservation is readably absent, not an unreadable table.
    header_only_page = (
        '<html><head><title>IP Allocation</title></head><body><form action="/cgi-bin/ipalloc.ha">'
        '<input type="hidden" name="nonce" value="n">'
        f"<table>{HEADER}</table>"
        f'<select id="alloc" name="alloc_{OTHER_MAC}" size="8"><option value="normal" selected>'
        "Address from DHCP pool</option></select>"
        '<input type="submit" name="Save" value="Save"></form></body></html>'
    )
    code, out, posts = _run(monkeypatch, capsys, clock, "W", header_only_page, verb=verb)
    assert len(posts) == 1
    assert out["committed"] is True and out["outcome"] == "applied"
    assert code == 1 and out["verified"] is False
    assert f"no Fixed Allocation row for {MAC}" in json.dumps(out["mismatches"])


@pytest.mark.parametrize("polls", ["W", "MW", "WM", "M"], ids=["allW", "M-then-W", "W-then-M", "allM"])
@pytest.mark.parametrize(
    ("reread", "expected"), [(HOLDS, 0), (PLEASE_WAIT, 2)], ids=["row-present", "unreadable"]
)
def test_probe_shapes_never_give_a_bogus_select_mismatch(
    tmp_env, clock, monkeypatch, capsys, polls, reread, expected
):
    code, out, posts = _run(monkeypatch, capsys, clock, polls, reread)
    assert len(posts) == 1
    assert code == expected
    assert "<absent>" not in json.dumps(out.get("mismatches") or {})


def test_readable_state_path_exits_zero_without_a_reread(tmp_env, clock, monkeypatch, capsys):
    gets_after_post: list[str] = []
    state = {"posted": False}

    def handler(request, _n):
        if request.method == "POST":
            state["posted"] = True
            return html(allocation_saved_html(MAC, IP))
        if state["posted"]:
            gets_after_post.append(request.url)
            return html(allocation_saved_html(MAC, IP))
        return html(entry_page_html(MAC, [IP]).replace('name="nonce" value="n"', 'name="nonce" value="ab12"'))

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["set", "ipalloc", f"alloc_{MAC}={IP}", "--commit", "--confirm", "IPALLOC", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["verified"] is True
    # The save wait's own state observation verifies it, so no verification re-read GET follows.
    assert gets_after_post == []
