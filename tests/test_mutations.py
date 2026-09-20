"""Ported from tests/mutations.test.ts (diagnostics tests live with diagnostics.py)."""

import pytest
from page_builders import button, checkbox, field, hidden, page, radio, select, textarea

from bgwcli.errors import UsageError
from bgwcli.mutations import (
    DANGEROUS_PAGES,
    base_payload,
    build_mutation_plan,
    build_submit_plan,
    confirm_token_for_page,
    parse_assignments,
)
from bgwcli.snapshot import UNCHECKED, extract_snapshot

WIFI_PAGE = page(
    "wconfig_unified",
    title="Wi-Fi",
    heading="Wi-Fi",
    fields=[
        hidden("nonce", "abc"),
        checkbox("enable", "1", checked=True),
        checkbox("unused", "1"),
        field("Save", "submit", "Save"),
    ],
    selects=[select("mode", ["auto", "manual"])],
)


def test_parse_assignments_parses_key_value_pairs():
    assert parse_assignments(["ssid=Home", "mode=manual"]) == {"ssid": "Home", "mode": "manual"}


def test_parse_assignments_keeps_equals_in_value_and_rejects_bad_pairs():
    assert parse_assignments(["a=b=c"]) == {"a": "b=c"}
    with pytest.raises(UsageError, match="Invalid assignment 'novalue'"):
        parse_assignments(["novalue"])
    with pytest.raises(UsageError, match="KEY=VALUE"):
        parse_assignments(["=x"])


def test_build_mutation_plan_keeps_checked_fields_and_applies_changes():
    plan = build_mutation_plan("wconfig_unified", WIFI_PAGE, ["mode=manual"])
    assert plan.blocked is False
    assert plan.reason is None
    assert plan.raw_payload == {"enable": "1", "mode": "manual"}
    assert plan.display_payload == {"enable": "1", "mode": "manual"}
    assert plan.display_changes == {"mode": "manual"}


def test_build_mutation_plan_blocks_dangerous_pages():
    plan = build_mutation_plan("restart", WIFI_PAGE, ["Restart=Restart Device"])
    assert plan.blocked is True
    assert "dangerous" in (plan.reason or "")
    assert plan.raw_payload["Restart"] == "Restart Device"


def test_dangerous_pages_are_fixed():
    assert frozenset({"reset", "restart", "routerpasswd", "update"}) == DANGEROUS_PAGES


def test_build_mutation_plan_allow_dangerous_dry_run_unblocks():
    plan = build_mutation_plan("restart", WIFI_PAGE, [], allow_dangerous_dry_run=True)
    assert plan.blocked is False


def test_confirm_token_for_page_creates_stable_confirmation_token():
    assert confirm_token_for_page("wconfig_unified") == "WCONFIG-UNIFIED"
    assert confirm_token_for_page("ip alloc/x") == "IP-ALLOC-X"


def test_confirm_token_resolves_tab_names_via_pages():
    pytest.importorskip("bgwcli.pages")
    assert confirm_token_for_page("Home Network/Wi-Fi") == "WCONFIG-UNIFIED"


def test_build_submit_plan_adds_selected_button_to_payload():
    with_button = page("diag", fields=WIFI_PAGE.fields, selects=WIFI_PAGE.selects, buttons=[button("Ping", "Ping")])
    plan = build_submit_plan("diag", with_button, "Ping", ["Address=example.com"])
    assert plan.raw_payload["Ping"] == "Ping"
    assert plan.raw_payload["Address"] == "example.com"
    assert plan.button is not None and plan.button.name == "Ping"
    assert plan.blocked is False


def test_build_submit_plan_matches_button_by_normalized_name_value_or_label():
    with_button = page("diag", buttons=[button("btn_go", "Run Test")])
    assert build_submit_plan("diag", with_button, "run-test", []).raw_payload == {"btn_go": "Run Test"}
    assert build_submit_plan("diag", with_button, "BTN GO", []).button is not None


def test_build_submit_plan_raises_when_button_is_missing():
    with pytest.raises(UsageError, match="Button 'Nope' was not found on diag"):
        build_submit_plan("diag", WIFI_PAGE, "Nope", [])


def test_build_submit_plan_allows_dangerous_page_dry_run():
    with_button = page("restart", buttons=[button("Restart", "Restart Device")])
    plan = build_submit_plan("restart", with_button, "Restart", [])
    assert plan.blocked is False
    assert plan.raw_payload == {"Restart": "Restart Device"}


def test_display_payload_redacts_secrets_unless_include_secrets():
    secret_page = page("wconfig", fields=[field("key11", "text", "topsecret")])
    plan = build_mutation_plan("wconfig", secret_page, ["key11=newsecret"])
    assert plan.raw_payload == {"key11": "newsecret"}
    assert plan.display_payload == {"key11": "[redacted]"}
    assert plan.display_changes == {"key11": "[redacted]"}
    shown = build_mutation_plan("wconfig", secret_page, ["key11=newsecret"], include_secrets=True)
    assert shown.display_payload == {"key11": "newsecret"}


def _page_with_disabled():
    return page(
        "wconfig_unified",
        title="Wi-Fi",
        heading="Wi-Fi",
        fields=[field("ssid", "text", "home"), field("guest_ssid", "text", "guest", disabled=True)],
        selects=[select("channel", ["auto"]), select("guest_channel", ["auto"], disabled=True)],
        textareas=[textarea("note", "x", disabled=True)],
    )


def test_base_payload_omits_disabled_fields_selects_and_textareas():
    plan = build_mutation_plan("wconfig_unified", _page_with_disabled(), ["ssid=new"], True)
    assert plan.raw_payload == {"ssid": "new", "channel": "auto"}
    assert "guest_ssid" not in plan.raw_payload
    assert "guest_channel" not in plan.raw_payload
    assert "note" not in plan.raw_payload


# --- Fix round 2, finding I6 ------------------------------------------------------------------
# base_payload in mutations.py and form values in snapshot.py are a deliberate mirror: a dump must
# contain exactly the fields a restore POST would send, or restore would either drop router state
# or diff on fields it can never write. This walks every rule in both functions on one page.
def test_i6_snapshot_form_values_and_mutation_base_payload_stay_in_sync():
    parsed = page(
        "dosprotect",
        fields=[
            hidden("nonce", "abc"),
            hidden("hashpassword", "deadbeef"),
            field("plain", "text", "1"),
            field("disabled_text", "text", "2", disabled=True),
            field("btn_button", "button", "Button"),
            field("btn_submit", "submit", "Submit"),
            field("btn_reset", "reset", "Reset"),
            field("btn_image", "image", "Image"),
            checkbox("box_on", "on", checked=True),
            checkbox("box_off", "on"),
            radio("rad_on", "yes", checked=True),
            radio("rad_off", "no"),
        ],
        selects=[select("sel_on", ["a", "b"]), select("sel_off", ["x"], disabled=True)],
        textareas=[textarea("area_on", "text"), textarea("area_off", "other", disabled=True)],
        buttons=[button("Save", "Save")],
    )
    payload_keys = sorted(base_payload(parsed))
    form = extract_snapshot({"dosprotect": parsed}, ts="t", router_host="r").forms["dosprotect"]
    posted_keys = sorted(k for k, v in form.items() if v != UNCHECKED)
    unchecked_keys = sorted(k for k, v in form.items() if v == UNCHECKED)
    assert posted_keys == payload_keys
    assert unchecked_keys == ["box_off", "rad_off"]
    assert payload_keys == ["area_on", "box_on", "plain", "rad_on", "sel_on"]


# --- Fix round 1 ------------------------------------------------------------------------------
# WPS PIN submit fields are the one deliberate divergence from the I6 mirror above: the dump drops
# them (they're an action, not configuration) but base_payload still sends their live value on
# restore, exactly as a browser submitting the page would.
def test_wconfig_parity_wps_pin_is_the_only_divergence_between_dump_and_restore_payload():
    parsed = page(
        "wconfig",
        fields=[
            hidden("nonce", "abc"),
            field("maxclients", "text", "80"),
            field("ssidname12", "text", "Guest", disabled=True),
            field("WPSPIN5", "text", ""),
        ],
        selects=[select("wl80211on", [("on", "On"), ("off", "Off")])],
        buttons=[button("Update", "Update"), button("Save", "Save...")],
    )
    payload = set(base_payload(parsed))
    dumped = set(extract_snapshot({"wconfig": parsed}, ts="t", router_host="r").forms["wconfig"])
    assert payload == dumped | {"WPSPIN5"}


def test_build_mutation_plan_includes_the_pages_save_button_so_the_router_applies_the_form():
    """Live gateway 2026-09-20: posting form fields without the Save submit value answers 302 and
    silently discards the change. `set` must post the page's Save button like a browser does."""
    from page_builders import dosprotect_page, select

    dos = dosprotect_page(selects=[select("icmp_downstream_echo_rqst_drop_wan", ["off", "on"], selected="off")])
    plan = build_mutation_plan("dosprotect", dos, ["icmp_downstream_echo_rqst_drop_wan=on"])
    assert plan.blocked is False
    assert plan.raw_payload["icmp_downstream_echo_rqst_drop_wan"] == "on"
    assert plan.raw_payload["Save"] == "Save"
    assert plan.button is not None and plan.button.name == "Save"
    assert plan.warning is None


def test_build_mutation_plan_warns_when_the_page_has_no_save_button():
    plan = build_mutation_plan("wconfig_unified", WIFI_PAGE, ["mode=manual"])
    assert plan.raw_payload == {"enable": "1", "mode": "manual"}
    assert plan.button is None
    assert plan.warning is not None and "no Save button" in plan.warning
