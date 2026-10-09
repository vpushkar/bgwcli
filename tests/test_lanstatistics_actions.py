"""LAN statistics buttons use guarded actions and a fresh nonce, with synthetic HTTP only."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from bgwcli import cli
from bgwcli.actions import ROUTER_ACTIONS, get_action, normalize_action
from bgwcli.client import BGW320Client, RawResponse
from bgwcli.config import GlobalOptions
from bgwcli.errors import UsageError

CASES = [
    ("detect-wifi-congestion-2.4", "Congestion", "Congestion Detection 2.4 GHz", "CONGESTION-2.4",
     ("congestion-2.4", "congestion-24"), False),
    ("detect-wifi-congestion-5", "CongRadio2", "Congestion Detection 5 GHz", "CONGESTION-5",
     ("congestion-5", "congestion-5ghz"), False),
    ("clear-connection-statistics", "ClearSta", "Clear Connection Statistics", "CLEAR-CONNECTION-STATISTICS",
     ("clear-connection-stats",), True),
    ("clear-lan-statistics", "Clear", "Clear Statistics", "CLEAR-LAN-STATISTICS",
     ("clear-statistics", "clear-lan-stats"), True),
]


def statistics_html(nonce, *, override=None, missing=None):
    buttons = "".join(
        f'<input type="submit" name="{button}" value="{override or value}"'
        '>'
        for _name, button, value, _token, _aliases, _dangerous in CASES if button != missing
    )
    return (
        '<html><body><form method="post" action="/cgi-bin/lanstatistics.ha">'
        f'<input type="hidden" name="nonce" value="{nonce}">{buttons}</form></body></html>'
    )


def client_with_transport(*, override=None, missing=None):
    requests = []

    def transport(request):
        requests.append(request)
        assert urlsplit(request.url).hostname == "synthetic.invalid"
        assert urlsplit(request.url).path == "/cgi-bin/lanstatistics.ha"
        body = statistics_html(f"a{len(requests)}", override=override, missing=missing)
        return RawResponse(200, "OK", [], body.encode())

    client = BGW320Client("synthetic.invalid", transport=transport)
    client.import_session({
        "origin": "https://synthetic.invalid", "authenticated": True, "cookies": {"synthetic": "synthetic"},
    })
    return client, requests


def command(name, *, commit=False, confirm=None):
    return cli.Command("action", [name], GlobalOptions(json=True, host="synthetic.invalid"),
                       commit=commit, confirm=confirm)


@pytest.mark.parametrize("name,button,value,token,aliases,dangerous", CASES)
def test_lan_action_contract(name, button, value, token, aliases, dangerous):
    action = get_action(name)
    assert action is not None
    assert (action.page, action.form_button, action.payload, action.confirm_token, action.dangerous) == (
        "lanstatistics", button, {button: value}, token, dangerous,
    )
    for alias in aliases:
        assert get_action(alias.upper().replace("-", " ")) is action


def test_action_names_and_aliases_are_unambiguous():
    owners = {}
    for action in ROUTER_ACTIONS:
        for name in (action.name, *(action.aliases or ())):
            normalized = normalize_action(name)
            assert normalized not in owners or owners[normalized] == action.name
            owners[normalized] = action.name


@pytest.mark.parametrize("name,button,value,token,aliases,dangerous", CASES)
def test_lan_actions_dry_run_and_confirmation_gate_no_http(
    tmp_env, capsys, name, button, value, token, aliases, dangerous,
):
    client, requests = client_with_transport()
    assert cli.run(command(aliases[0]), client) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["dryRun"] is True and output["committed"] is False
    assert output["payload"] == {button: value}
    assert output["commitCommand"] == f"action {name} --commit --confirm {token}"
    for confirmation in (None, "WRONG-TOKEN"):
        with pytest.raises(UsageError, match=token):
            cli.run(command(name, commit=True, confirm=confirmation), client)
    assert requests == []


@pytest.mark.parametrize("name,button,value,token,aliases,dangerous", CASES)
@pytest.mark.parametrize("live_value", [None, "Current firmware button label"])
def test_lan_actions_post_only_selected_live_button_and_fresh_nonce(
    tmp_env, capsys, name, button, value, token, aliases, dangerous, live_value,
):
    client, requests = client_with_transport(override=live_value)
    assert cli.run(command(aliases[0], commit=True, confirm=token), client) == 0
    assert [request.method for request in requests] == ["GET", "GET", "POST"]
    assert parse_qs(requests[-1].body.decode()) == {button: [live_value or value], "nonce": ["a2"]}
    output = json.loads(capsys.readouterr().out)
    assert output["committed"] is True and output["action"] == name


@pytest.mark.parametrize("name,button,value,token,aliases,dangerous", CASES)
def test_lan_action_refuses_missing_button(
    tmp_env, name, button, value, token, aliases, dangerous,
):
    client, requests = client_with_transport(missing=button)
    with pytest.raises(UsageError):
        cli.run(command(name, commit=True, confirm=token), client)
    assert [request.method for request in requests] == ["GET"]
