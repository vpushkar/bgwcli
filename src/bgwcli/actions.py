"""Named router actions: button-style POSTs with a per-action confirmation token.

Port of src/actions.ts. Every confirm token, page, payload and dangerous flag is preserved verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .redact import redact_value


@dataclass(frozen=True)
class RouterAction:
    name: str
    page: str
    description: str
    confirm_token: str
    payload: dict[str, str]
    dangerous: bool
    aliases: tuple[str, ...] | None = None
    # CGI path (relative to /cgi-bin/, may carry a query string) the form posts to when it is not
    # the page itself; the nonce is still read from `page`. None = post to `<page>.ha`.
    post_path: str | None = None
    # Submit button INSIDE a page's main form: the action posts the live form's base payload plus
    # this button (like `submit <page> <button>`), never the button alone.
    form_button: str | None = None


ROUTER_ACTIONS: tuple[RouterAction, ...] = (
    RouterAction(
        name="restart",
        page="restart",
        description="Restart the gateway.",
        confirm_token="RESTART",
        payload={"Restart": "Restart Device"},
        dangerous=True,
        aliases=("restart-device", "reboot"),
    ),
    RouterAction(
        name="clear-device-list",
        page="devices",
        description="Clear inactive devices from the device list.",
        confirm_token="CLEAR-DEVICES",
        payload={"Clear": "Clear Device List"},
        dangerous=False,
    ),
    RouterAction(
        name="run-speed-test",
        page="speed",
        description="Run the router speed test.",
        confirm_token="SPEED",
        payload={"run": "Run Speed Test"},
        dangerous=False,
        aliases=("speed-test",),
    ),
    RouterAction(
        name="run-full-diagnostics",
        page="diag",
        description="Run the router's full diagnostic test suite.",
        confirm_token="DIAG",
        payload={"RunFullDiagnostics": "Run Full Diagnostics"},
        dangerous=False,
        aliases=("full-diagnostics",),
    ),
    RouterAction(
        name="send-diagnostics",
        page="diag",
        description="Send the router diagnostics report to AT&T.",
        confirm_token="SEND-DIAGNOSTICS",
        payload={"SendDiagnostics": "Send Diagnostics"},
        dangerous=False,
        aliases=("send-diagnostic-report",),
    ),
    RouterAction(
        name="diagnostics-ethernet-details",
        page="diag",
        description="Show Ethernet diagnostic details.",
        confirm_token="DIAG",
        payload={"EthDetails": "Details"},
        dangerous=False,
        aliases=("ethernet-details",),
    ),
    RouterAction(
        name="diagnostics-authentication-details",
        page="diag",
        description="Show authentication diagnostic details.",
        confirm_token="DIAG",
        payload={"AuthDetails": "Details"},
        dangerous=False,
        aliases=("authentication-details",),
    ),
    RouterAction(
        name="diagnostics-ip-details",
        page="diag",
        description="Show IP diagnostic details.",
        confirm_token="DIAG",
        payload={"IPDetails": "Details"},
        dangerous=False,
        aliases=("ip-details",),
    ),
    RouterAction(
        name="diagnostics-dns-details",
        page="diag",
        description="Show DNS diagnostic details.",
        confirm_token="DIAG",
        payload={"DNSDetails": "Details"},
        dangerous=False,
        aliases=("dns-details",),
    ),
    RouterAction(
        name="packet-filter-enable",
        page="packetfilter",
        description="Enable packet filters.",
        confirm_token="PACKETFILTER",
        payload={"Enable": "Enable Packet Filters"},
        dangerous=False,
        aliases=("enable-packet-filter", "enable-packet-filters"),
    ),
    RouterAction(
        name="packet-filter-add-drop-rule",
        page="packetfilter",
        description="Open/add a packet-filter drop rule.",
        confirm_token="PACKETFILTER",
        payload={"AddDropRule": "Add a 'Drop' Rule"},
        dangerous=False,
        aliases=("add-drop-rule",),
    ),
    RouterAction(
        name="packet-filter-add-pass-rule",
        page="packetfilter",
        description="Open/add a packet-filter pass rule.",
        confirm_token="PACKETFILTER",
        payload={"AddPassRule": "Add a 'Pass' Rule"},
        dangerous=False,
        aliases=("add-pass-rule",),
    ),
    RouterAction(
        name="reset-ip",
        page="reset",
        description="Reset the router IP stack.",
        confirm_token="RESET-IP",
        payload={"ResetIP": "Reset IP"},
        dangerous=True,
    ),
    RouterAction(
        name="reset-connection",
        page="reset",
        description="Reset the router broadband connection.",
        confirm_token="RESET-CONNECTION",
        payload={"ResetConn": "Reset Connection"},
        dangerous=True,
    ),
    RouterAction(
        name="restart-from-resets",
        page="reset",
        description="Restart from the Diagnostics > Resets page.",
        confirm_token="RESTART",
        payload={"Restart": "Restart"},
        dangerous=True,
    ),
    RouterAction(
        name="reset-wifi-config",
        page="reset",
        description="Reset Wi-Fi configuration.",
        confirm_token="RESET-WIFI-CONFIG",
        payload={"WReset": "Reset Wi-Fi Config"},
        dangerous=True,
        aliases=("reset-wi-fi-config",),
    ),
    RouterAction(
        name="reset-firewall-config",
        page="reset",
        description="Reset firewall configuration.",
        confirm_token="RESET-FIREWALL-CONFIG",
        payload={"FReset": "Reset Firewall Config"},
        dangerous=True,
    ),
    RouterAction(
        name="factory-reset",
        page="reset",
        description="Reset the device to defaults.",
        confirm_token="FACTORY-RESET",
        payload={"Reset": "Reset Device..."},
        dangerous=True,
        aliases=("reset-device",),
    ),
    RouterAction(
        name="restart-wifi-2.4",
        page="home",
        description="Restart the 2.4 GHz Wi-Fi radio (home.ha Restart button; clients on 2.4 GHz drop briefly).",
        confirm_token="RESTART-WIFI",
        payload={"WRestart1": "Restart"},
        dangerous=False,
        aliases=("restart-wifi-24", "restart-2.4ghz", "restart-wifi-2-4"),
        post_path="wrestart.ha?1",
    ),
    RouterAction(
        name="restart-wifi-5",
        page="home",
        description="Restart the 5 GHz Wi-Fi radio (home.ha Restart button; clients on 5 GHz drop briefly).",
        confirm_token="RESTART-WIFI",
        payload={"WRestart2": "Restart"},
        dangerous=False,
        aliases=("restart-wifi-5ghz", "restart-5ghz"),
        post_path="wrestart.ha?2",
    ),
    RouterAction(
        name="restart-broadband",
        page="home",
        description="Restart the broadband connection (home.ha Restart button; drops WAN for a while).",
        confirm_token="RESTART-BROADBAND",
        payload={"Broadband": "Restart"},
        dangerous=True,
        aliases=("restart-wan",),
        post_path="crestart.ha?1",
    ),
    RouterAction(
        name="find-best-channel-5",
        page="wconfig",
        description="Run the 5 GHz 'Find Best Channel' scan on Advanced Wi-Fi (5 GHz clients drop briefly).",
        confirm_token="CHANSCAN",
        payload={"chanscan5": "Find Best Channel"},
        dangerous=False,
        aliases=("chanscan5", "find-best-channel", "scan-5ghz-channel"),
        form_button="chanscan5",
    ),
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_action(value: str) -> str:
    """Lower-case and strip everything that is not a-z0-9 so 'Speed-Test' == 'speedtest'."""
    return _NON_ALNUM.sub("", value.lower())


def get_action(name: str) -> RouterAction | None:
    """Look an action up by name or alias (punctuation- and case-insensitive)."""
    wanted = normalize_action(name)
    for action in ROUTER_ACTIONS:
        candidates = (action.name, *(action.aliases or ()))
        if any(normalize_action(candidate) == wanted for candidate in candidates):
            return action
    return None


def display_action_payload(action: RouterAction, include_secrets: bool = False) -> dict[str, str]:
    """The action payload as shown to the user: sensitive field names redacted unless asked."""
    return {name: redact_value(name, value, include_secrets) for name, value in action.payload.items()}
