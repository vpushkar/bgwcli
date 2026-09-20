"""Integration: real parser -> mutations (submit/mutation plans), and the mutations <-> snapshot mirror.

Re-derives the tests/mutations.test.ts cases that fed inline HTML through parsePage (the I6 mirror
and the wconfig WPS-PIN parity test), and adds plan-building checks on the real router page
constants so build_submit_plan / build_mutation_plan are proven against parser output rather than
hand-built ParsedPage objects.
"""

from __future__ import annotations

import pytest
from integration_html import (
    APPHOSTING_HTML,
    DOSPROTECT_HTML,
    I6_MIRROR_HTML,
    IPALLOC_STICKY_HTML,
    SERVICES_HTML,
    WCONFIG_HTML,
    WCONFIG_PARITY_HTML,
)

from bgwcli.errors import UsageError
from bgwcli.mutations import (
    DANGEROUS_PAGES,
    base_payload,
    build_mutation_plan,
    build_submit_plan,
    confirm_token_for_page,
)
from bgwcli.parser import parse_page
from bgwcli.snapshot import UNCHECKED, extract_snapshot


def parsed(page: str, html: str):
    return parse_page(page, html, include_secrets=True)


def test_i6_snapshot_form_values_and_mutation_base_payload_stay_in_sync():
    # base_payload (mutations) and _form_values (snapshot) are a deliberate mirror: a dump must
    # contain exactly the fields a restore POST would send. Unchecked checkables are the one
    # divergence here (dump records UNCHECKED; the browser-style payload omits them).
    page = parsed("dosprotect", I6_MIRROR_HTML)
    payload_keys = sorted(base_payload(page))
    form = extract_snapshot({"dosprotect": page}, ts="t", router_host="r").forms.get("dosprotect", {})
    posted_keys = sorted(k for k, v in form.items() if v != UNCHECKED)
    unchecked_keys = sorted(k for k, v in form.items() if v == UNCHECKED)
    assert posted_keys == payload_keys
    assert unchecked_keys == ["box_off", "rad_off"]
    assert payload_keys == ["area_on", "box_on", "plain", "rad_on", "sel_on"]


def test_wconfig_parity_wps_pin_is_the_only_deliberate_divergence_between_dump_and_restore_payload():
    page = parsed("wconfig", WCONFIG_PARITY_HTML)
    payload_keys = set(base_payload(page))
    dump_keys = set(extract_snapshot({"wconfig": page}, ts="t", router_host="r").forms["wconfig"])
    assert payload_keys == dump_keys | {"WPSPIN5"}


def test_base_payload_of_the_real_wconfig_page_mirrors_the_parser_flags():
    payload = base_payload(parsed("wconfig", WCONFIG_HTML))
    # disabled text + disabled select omitted; nonce omitted; WPS PIN kept; Save/Update/Cancel are buttons.
    assert payload == {
        "ssidname11": "EXAMPLE-NET",
        "key11": "topsecret",
        "maxclients": "80",
        "WPSPIN5": "",
        "wl80211on": "on",
    }


def test_build_submit_plan_add_on_the_real_services_page_carries_the_form_fields_and_the_button():
    plan = build_submit_plan(
        "services",
        parsed("services", SERVICES_HTML),
        "Add",
        ["Service=Extra", "extMinPort=7000", "extMaxPort=7000", "intStartPort=7000", "protocol=UDP"],
        include_secrets=True,
    )
    assert plan.blocked is False
    assert plan.button is not None and plan.button.name == "Add"
    assert plan.raw_payload == {
        "Service": "Extra",
        "extMinPort": "7000",
        "extMaxPort": "7000",
        "intStartPort": "7000",
        "protocol": "UDP",
        "Add": "Add",
    }
    assert plan.display_changes == {
        "Service": "Extra",
        "extMinPort": "7000",
        "extMaxPort": "7000",
        "intStartPort": "7000",
        "protocol": "UDP",
    }


def test_build_submit_plan_remove_on_the_real_services_page_posts_the_positional_button_with_its_rendered_value():
    plan = build_submit_plan("services", parsed("services", SERVICES_HTML), "Remove_2", [], include_secrets=True)
    # Base payload (empty Add-form inputs + protocol select) plus the chosen Remove button's value.
    assert plan.raw_payload == {
        "Service": "",
        "extMinPort": "",
        "extMaxPort": "",
        "intStartPort": "",
        "protocol": "TCP",
        "Remove_2": "Remove",
    }
    assert plan.button is not None and plan.button.label == "Remove"


def test_build_submit_plan_matches_the_button_by_name_value_or_label_case_insensitively():
    page = parsed("services", SERVICES_HTML)
    assert build_submit_plan("services", page, "add", []).button is not None
    assert build_submit_plan("services", page, "remove_1", []).button is not None
    # "Remove" (the value) is shared by two buttons; the first in document order wins, as in TS.
    assert build_submit_plan("services", page, "Remove", []).button.name == "Remove_1"  # type: ignore[union-attr]


def test_build_submit_plan_raises_usage_error_for_a_button_the_page_does_not_render():
    with pytest.raises(UsageError, match="Button 'Apply' was not found on dosprotect"):
        build_submit_plan("dosprotect", parsed("dosprotect", DOSPROTECT_HTML), "Apply", [])


def test_build_mutation_plan_on_the_real_dosprotect_page_keeps_checked_and_omits_disabled_and_unchecked():
    plan = build_mutation_plan("dosprotect", parsed("dosprotect", DOSPROTECT_HTML), ["flood_protect=off"], True)
    assert plan.blocked is False
    assert plan.raw_payload == {"reflexive": "on", "flood_protect": "off", "Save": "Save"}  # Save button included
    assert plan.button is not None and plan.button.name == "Save"
    assert plan.display_payload == plan.raw_payload
    assert plan.display_changes == {"flood_protect": "off"}


def test_build_mutation_plan_redacts_sensitive_values_in_display_but_not_raw_payload():
    plan = build_mutation_plan("wconfig", parsed("wconfig", WCONFIG_HTML), ["key11=newsecret"])
    assert plan.raw_payload["key11"] == "newsecret"
    assert plan.display_payload["key11"] != "newsecret"
    assert plan.display_changes["key11"] != "newsecret"
    assert plan.display_payload["ssidname11"] == "EXAMPLE-NET"


def test_build_mutation_plan_blocks_dangerous_pages_even_with_a_real_parsed_page():
    page = parsed("restart", DOSPROTECT_HTML)
    for dangerous in sorted(DANGEROUS_PAGES):
        plan = build_mutation_plan(dangerous, page, ["Restart=Restart Device"])
        assert plan.blocked is True
        assert plan.reason is not None and "dangerous" in plan.reason
        assert plan.raw_payload["Restart"] == "Restart Device"


def test_apphosting_add_plan_uses_select_values_the_parser_recorded():
    page = parsed("apphosting", APPHOSTING_HTML)
    plan = build_submit_plan(
        "apphosting", page, "Add", ["service=Mosh", "device=aa:bb:cc:dd:ee:02"], include_secrets=True
    )
    assert plan.raw_payload == {"service": "Mosh", "device": "aa:bb:cc:dd:ee:02", "Add": "Add"}


def test_base_payload_of_a_sticky_ipalloc_page_would_carry_alloc_normal_which_is_why_reserve_bypasses_it():
    # Documents the hazard restore._reserve_step avoids: the sticky entry block's select is part of
    # the browser-equivalent payload and would release another device's reservation on Allocate.
    payload = base_payload(parsed("ipalloc", IPALLOC_STICKY_HTML))
    assert payload == {"alloc_aa:bb:cc:dd:ee:ff": "normal"}


def test_confirm_token_for_page_uses_the_resolved_page_name():
    assert confirm_token_for_page("services") == "SERVICES"
    assert confirm_token_for_page("wconfig_unified") == "WCONFIG-UNIFIED"
    assert confirm_token_for_page("Home Network/Wi-Fi") == "WCONFIG-UNIFIED"
