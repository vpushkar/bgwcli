"""Ported from tests/actions.test.ts."""

from __future__ import annotations

from bgwcli.actions import ROUTER_ACTIONS, RouterAction, display_action_payload, get_action, normalize_action
from bgwcli.types import to_json_dict


def test_router_actions_includes_observed_button_style_operations():
    assert [action.name for action in ROUTER_ACTIONS] == [
        "restart",
        "clear-device-list",
        "run-speed-test",
        "run-full-diagnostics",
        "send-diagnostics",
        "diagnostics-ethernet-details",
        "diagnostics-authentication-details",
        "diagnostics-ip-details",
        "diagnostics-dns-details",
        "packet-filter-enable",
        "packet-filter-add-drop-rule",
        "packet-filter-add-pass-rule",
        "reset-ip",
        "reset-connection",
        "restart-from-resets",
        "reset-wifi-config",
        "reset-firewall-config",
        "factory-reset",
        "restart-wifi-2.4",
        "restart-wifi-5",
        "restart-broadband",
        "find-best-channel-5",
    ]


def test_confirm_tokens_pages_payloads_and_dangerous_flags_preserved():
    by_name = {a.name: a for a in ROUTER_ACTIONS}
    expected = {
        "restart": ("restart", "RESTART", {"Restart": "Restart Device"}, True),
        "clear-device-list": ("devices", "CLEAR-DEVICES", {"Clear": "Clear Device List"}, False),
        "run-speed-test": ("speed", "SPEED", {"run": "Run Speed Test"}, False),
        "run-full-diagnostics": ("diag", "DIAG", {"RunFullDiagnostics": "Run Full Diagnostics"}, False),
        "send-diagnostics": ("diag", "SEND-DIAGNOSTICS", {"SendDiagnostics": "Send Diagnostics"}, False),
        "diagnostics-ethernet-details": ("diag", "DIAG", {"EthDetails": "Details"}, False),
        "diagnostics-authentication-details": ("diag", "DIAG", {"AuthDetails": "Details"}, False),
        "diagnostics-ip-details": ("diag", "DIAG", {"IPDetails": "Details"}, False),
        "diagnostics-dns-details": ("diag", "DIAG", {"DNSDetails": "Details"}, False),
        "packet-filter-enable": ("packetfilter", "PACKETFILTER", {"Enable": "Enable Packet Filters"}, False),
        "packet-filter-add-drop-rule": ("packetfilter", "PACKETFILTER", {"AddDropRule": "Add a 'Drop' Rule"}, False),
        "packet-filter-add-pass-rule": ("packetfilter", "PACKETFILTER", {"AddPassRule": "Add a 'Pass' Rule"}, False),
        "reset-ip": ("reset", "RESET-IP", {"ResetIP": "Reset IP"}, True),
        "reset-connection": ("reset", "RESET-CONNECTION", {"ResetConn": "Reset Connection"}, True),
        "restart-from-resets": ("reset", "RESTART", {"Restart": "Restart"}, True),
        "reset-wifi-config": ("reset", "RESET-WIFI-CONFIG", {"WReset": "Reset Wi-Fi Config"}, True),
        "reset-firewall-config": ("reset", "RESET-FIREWALL-CONFIG", {"FReset": "Reset Firewall Config"}, True),
        "factory-reset": ("reset", "FACTORY-RESET", {"Reset": "Reset Device..."}, True),
        "restart-wifi-2.4": ("home", "RESTART-WIFI", {"WRestart1": "Restart"}, False),
        "restart-wifi-5": ("home", "RESTART-WIFI", {"WRestart2": "Restart"}, False),
        "restart-broadband": ("home", "RESTART-BROADBAND", {"Broadband": "Restart"}, True),
        "find-best-channel-5": ("wconfig", "CHANSCAN", {"chanscan5": "Find Best Channel"}, False),
    }
    assert set(by_name) == set(expected)
    for name, (page, token, payload, dangerous) in expected.items():
        action = by_name[name]
        assert (action.page, action.confirm_token, action.payload, action.dangerous) == (
            page,
            token,
            payload,
            dangerous,
        )


def test_get_action_resolves_aliases():
    assert get_action("reboot").name == "restart"
    assert get_action("speed-test").name == "run-speed-test"
    assert get_action("full-diagnostics").name == "run-full-diagnostics"
    assert get_action("send-diagnostic-report").name == "send-diagnostics"
    assert get_action("reset-device").name == "factory-reset"
    assert get_action("restart-device").name == "restart"
    assert get_action("reset-wi-fi-config").name == "reset-wifi-config"
    assert get_action("ethernet-details").name == "diagnostics-ethernet-details"
    assert get_action("enable-packet-filters").name == "packet-filter-enable"


def test_get_action_normalizes_case_and_punctuation():
    assert normalize_action("Run_Speed Test!") == "runspeedtest"
    assert get_action("RUN SPEED TEST").name == "run-speed-test"
    assert get_action("FactoryReset").name == "factory-reset"
    assert get_action("nonexistent") is None


def test_display_action_payload_redacts_sensitive_names():
    action = RouterAction(
        name="example",
        page="routerpasswd",
        description="example",
        confirm_token="EXAMPLE",
        payload={"password": "secret"},
        dangerous=True,
    )
    assert display_action_payload(action) == {"password": "[redacted]"}
    assert display_action_payload(action, include_secrets=True) == {"password": "secret"}
    assert display_action_payload(get_action("restart")) == {"Restart": "Restart Device"}


def test_router_action_json_shape_matches_typescript():
    restart = to_json_dict(get_action("restart"))
    assert restart == {
        "name": "restart",
        "page": "restart",
        "description": "Restart the gateway.",
        "confirmToken": "RESTART",
        "payload": {"Restart": "Restart Device"},
        "dangerous": True,
        "aliases": ["restart-device", "reboot"],
    }
    # Actions without aliases omit the key, as the TS object literal does.
    assert "aliases" not in to_json_dict(get_action("reset-ip"))


def test_radio_and_broadband_restart_actions_post_to_their_own_form_actions():
    """Live home.ha 2026-09-20: 2.4 GHz -> /cgi-bin/wrestart.ha?1 (WRestart1), 5 GHz -> wrestart.ha?2
    (WRestart2), broadband -> crestart.ha?1 (Broadband). The nonce is read from home.ha."""
    from bgwcli.actions import ROUTER_ACTIONS, get_action

    wifi24 = get_action("restart-wifi-2.4")
    assert wifi24 is not None and wifi24.page == "home" and wifi24.post_path == "wrestart.ha?1"
    assert wifi24.payload == {"WRestart1": "Restart"} and wifi24.dangerous is False
    assert wifi24.confirm_token == "RESTART-WIFI"
    assert get_action("restart-wifi-24") is wifi24 and get_action("restart-2.4ghz") is wifi24
    wifi5 = get_action("restart-wifi-5")
    assert wifi5 is not None and wifi5.post_path == "wrestart.ha?2" and wifi5.payload == {"WRestart2": "Restart"}
    assert wifi5.confirm_token == "RESTART-WIFI"
    bb = get_action("restart-broadband")
    assert bb is not None and bb.post_path == "crestart.ha?1" and bb.payload == {"Broadband": "Restart"}
    assert bb.dangerous is True and bb.confirm_token == "RESTART-BROADBAND"
    # every pre-existing action keeps posting to its page
    new_names = {"restart-wifi-2.4", "restart-wifi-5", "restart-broadband", "find-best-channel-5"}
    assert all(a.post_path is None for a in ROUTER_ACTIONS if a.name not in new_names)


def test_find_best_channel_5_is_a_form_button_action_on_wconfig():
    """Advanced Wi-Fi's 'Find Best Channel' (chanscan5) is a submit button INSIDE the big wconfig
    form, so the action must post the live form's base payload plus that button — never the
    button alone — or the gateway would treat every missing field as a change."""
    from bgwcli.actions import get_action

    a = get_action("find-best-channel-5")
    assert a is not None and a.page == "wconfig" and a.form_button == "chanscan5"
    assert a.payload == {"chanscan5": "Find Best Channel"} and a.dangerous is False
    assert a.confirm_token == "CHANSCAN" and a.post_path is None
    assert get_action("chanscan5") is a and get_action("find-best-channel") is a
