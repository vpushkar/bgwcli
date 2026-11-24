"""Packet-filter enable/disable post the page-level toggle button the live packetfilter.ha form renders.

fw 6.35.8 (captured 2026-11-23, filters enabled): the form shows only
`<input type="submit" name="Disable" value="Disable Packet Filters">`; the Enable button appears only
while filters are disabled. Both actions read their button from the live form, so asking for the
state the gateway is already in is refused before anything is posted (a blind POST would be answered
302 and reported as committed while the gateway ignored it).
"""

import json
from urllib.parse import parse_qs

import pytest
from save_helpers import client_with, html

from bgwcli import cli
from bgwcli.actions import get_action

_RULE_ROW = (
    '<input type="submit" class="upbutton" name="uporder_16" value="" title="Up" />'
    '<input type="submit" name="Complete_16" class="checkbutton" value="" title="Enable/Disable Rule" />'
    '<input type="submit" name="DeleteRule_16" value="Delete" class="cssbtn smallbtn" />'
)


def _page(toggle: str) -> str:
    return (
        '<form method="post" action="/cgi-bin/packetfilter.ha">'
        '<input type="hidden" name="nonce" value="abc123" />'
        f"{toggle}{_RULE_ROW}</form>"
    )


ENABLED = _page('<input type="submit" name="Disable" class="cssbtn largebtn" value="Disable Packet Filters" />')
DISABLED = _page('<input type="submit" name="Enable" class="cssbtn largebtn" value="Enable Packet Filters" />')


def _run(monkeypatch, capsys, name, page):
    def handler(request, n):
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/packetfilter.ha"})
        return html(page)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["action", name, "--commit", "--confirm", get_action(name).confirm_token, "--json"])
    captured = capsys.readouterr()
    posts = [r for r in wire.requests if r.method == "POST" and "/login.ha" not in r.url]
    return code, captured, posts


@pytest.mark.parametrize(
    ("name", "page", "button", "value"),
    [
        ("packet-filter-enable", DISABLED, "Enable", "Enable Packet Filters"),
        ("packet-filter-disable", ENABLED, "Disable", "Disable Packet Filters"),
    ],
)
def test_toggle_posts_the_live_button_and_no_rule_button(
    tmp_env, clock, monkeypatch, capsys, name, page, button, value
):
    code, captured, posts = _run(monkeypatch, capsys, name, page)

    assert code == 0 and len(posts) == 1
    sent = parse_qs(posts[0].body.decode())
    assert sent[button] == [value]
    assert sent["nonce"] == ["abc123"]
    for other in ("Enable", "Disable", "Complete_16", "DeleteRule_16", "uporder_16"):
        if other != button:
            assert other not in sent
    assert json.loads(captured.out)["committed"] is True


@pytest.mark.parametrize(
    ("name", "page", "button"),
    [("packet-filter-enable", ENABLED, "Enable"), ("packet-filter-disable", DISABLED, "Disable")],
)
def test_toggle_to_the_state_the_gateway_is_already_in_posts_nothing(
    tmp_env, clock, monkeypatch, capsys, name, page, button
):
    code, captured, posts = _run(monkeypatch, capsys, name, page)

    # Only the opposite toggle is on the page: already in that state, reported as unchanged (exit 0).
    out = json.loads(captured.out)
    assert code == 0 and posts == []
    assert out["outcome"] == "unchanged" and "already" in out["result"]


def test_disable_is_a_guarded_dangerous_action_with_aliases():
    action = get_action("disable-packet-filters")
    assert action.name == "packet-filter-disable"
    assert action.page == "packetfilter" and action.dangerous is True
    assert action.confirm_token == "PACKETFILTER"
