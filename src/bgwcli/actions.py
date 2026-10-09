"""Named router actions: button-style POSTs with a per-action confirmation token.

Port of src/actions.ts. Every confirm token, page, payload and dangerous flag is preserved verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

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
    # The action's effect takes the gateway's web server down (restart, reset family) or the radio
    # this client is connected through (Wi-Fi restart, channel scan): the redirect target that carries
    # the answer banner cannot be read, so it is not read and the POST's own answer stands. Every
    # other action whose answer cannot be read is "no answer".
    drops_web_server: bool = field(default=False, metadata={"serialize": False})
    # The button only opens an editor on the gateway; nothing is saved until the editor's own Save.
    opener: bool = field(default=False, metadata={"serialize": False})


ROUTER_ACTIONS: tuple[RouterAction, ...] = (
    RouterAction(
        name="restart",
        page="restart",
        description="Restart the gateway.",
        confirm_token="RESTART",
        payload={"Restart": "Restart Device"},
        dangerous=True,
        drops_web_server=True,
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
        name="detect-wifi-congestion-2.4",
        page="lanstatistics",
        description="Run 2.4 GHz Wi-Fi congestion detection from LAN Statistics.",
        confirm_token="CONGESTION-2.4",
        payload={"Congestion": "Congestion Detection 2.4 GHz"},
        dangerous=False,
        aliases=("congestion-2.4", "congestion-24"),
        form_button="Congestion",
    ),
    RouterAction(
        name="detect-wifi-congestion-5",
        page="lanstatistics",
        description="Run 5 GHz Wi-Fi congestion detection from LAN Statistics.",
        confirm_token="CONGESTION-5",
        payload={"CongRadio2": "Congestion Detection 5 GHz"},
        dangerous=False,
        aliases=("congestion-5", "congestion-5ghz"),
        form_button="CongRadio2",
    ),
    RouterAction(
        name="clear-connection-statistics",
        page="lanstatistics",
        description="Clear connection statistics counters from LAN Statistics.",
        confirm_token="CLEAR-CONNECTION-STATISTICS",
        payload={"ClearSta": "Clear Connection Statistics"},
        dangerous=True,
        aliases=("clear-connection-stats",),
        form_button="ClearSta",
    ),
    RouterAction(
        name="clear-lan-statistics",
        page="lanstatistics",
        description="Clear LAN statistics counters using the LAN Statistics Clear Statistics button.",
        confirm_token="CLEAR-LAN-STATISTICS",
        payload={"Clear": "Clear Statistics"},
        dangerous=True,
        aliases=("clear-statistics", "clear-lan-stats"),
        form_button="Clear",
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
        opener=True,
    ),
    RouterAction(
        name="packet-filter-add-pass-rule",
        page="packetfilter",
        description="Open/add a packet-filter pass rule.",
        confirm_token="PACKETFILTER",
        payload={"AddPassRule": "Add a 'Pass' Rule"},
        dangerous=False,
        aliases=("add-pass-rule",),
        opener=True,
    ),
    RouterAction(
        name="reset-ip",
        page="reset",
        description="Reset the router IP stack.",
        confirm_token="RESET-IP",
        payload={"ResetIP": "Reset IP"},
        dangerous=True,
        drops_web_server=True,
    ),
    RouterAction(
        name="reset-connection",
        page="reset",
        description="Reset the router broadband connection.",
        confirm_token="RESET-CONNECTION",
        payload={"ResetConn": "Reset Connection"},
        dangerous=True,
        drops_web_server=True,
    ),
    RouterAction(
        name="restart-from-resets",
        page="reset",
        description="Restart from the Diagnostics > Resets page.",
        confirm_token="RESTART",
        payload={"Restart": "Restart"},
        dangerous=True,
        drops_web_server=True,
    ),
    RouterAction(
        name="reset-wifi-config",
        page="reset",
        description="Reset Wi-Fi configuration.",
        confirm_token="RESET-WIFI-CONFIG",
        payload={"WReset": "Reset Wi-Fi Config"},
        dangerous=True,
        drops_web_server=True,
        aliases=("reset-wi-fi-config",),
    ),
    RouterAction(
        name="reset-firewall-config",
        page="reset",
        description="Reset firewall configuration.",
        confirm_token="RESET-FIREWALL-CONFIG",
        payload={"FReset": "Reset Firewall Config"},
        dangerous=True,
        drops_web_server=True,
    ),
    RouterAction(
        name="factory-reset",
        page="reset",
        description="Reset the device to defaults.",
        confirm_token="FACTORY-RESET",
        payload={"Reset": "Reset Device..."},
        dangerous=True,
        drops_web_server=True,
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
        drops_web_server=True,
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
        drops_web_server=True,
        post_path="wrestart.ha?2",
    ),
    RouterAction(
        name="restart-broadband",
        page="home",
        description="Restart the broadband connection (home.ha Restart button; drops WAN for a while).",
        confirm_token="RESTART-BROADBAND",
        payload={"Broadband": "Restart"},
        dangerous=True,
        drops_web_server=True,
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
        drops_web_server=True,
        form_button="chanscan5",
    ),
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


_OPENER_BUTTON = re.compile(r"^allocate_", re.IGNORECASE)
# Packet Filter's add-rule buttons open the rule editor too (the same names the opener actions post).
_OPENER_BUTTON_NAMES: dict[str, frozenset[str]] = {
    "packetfilter": frozenset({"adddroprule", "addpassrule"}),
}


def is_opener_button(page: str, button: str) -> bool:
    """True for a generic form button that only opens an editor: IP Allocation's Allocate_<mac> and
    Packet Filter's add-rule buttons. `button` is the page's resolved button name, never the token
    the user typed (a label or value can resolve to an opener)."""
    if page == "ipalloc":
        return _OPENER_BUTTON.match(button) is not None
    return normalize_action(button) in _OPENER_BUTTON_NAMES.get(page, frozenset())


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
