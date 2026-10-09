"""Browser default/on values through parsing, submission and snapshot restore."""

import pytest

from bgwcli.mutations import base_payload, build_submit_plan
from bgwcli.parser import parse_page
from bgwcli.restore import RestoreOptions, _postcondition_matches, build_restore_plan
from bgwcli.snapshot import UNCHECKED, extract_snapshot
from bgwcli.snapshot_diff import diff_snapshots


@pytest.mark.parametrize("kind", ["checkbox", "radio"])
@pytest.mark.parametrize("attribute, expected", [("", "on"), (' value=""', ""), (' value="yes"', "yes")])
def test_checkable_value_defaults_only_when_attribute_is_absent(kind, attribute, expected):
    parsed = parse_page("dosprotect", f'<input type="{kind}" name="enabled" checked{attribute}>')
    assert parsed.fields[0].value == expected
    assert base_payload(parsed) == {"enabled": expected}


@pytest.mark.parametrize("kind", ["checkbox", "radio"])
def test_default_value_does_not_change_checked_disabled_or_redaction_rules(kind):
    parsed = parse_page(
        "dosprotect",
        f'<input type="{kind}" name="unchecked">'
        f'<input type="{kind}" name="disabled" checked disabled>'
        f'<input type="{kind}" name="wpa_key" checked>',
    )
    assert [(f.value, f.checked, bool(f.disabled)) for f in parsed.fields] == [
        ("on", False, False), ("on", True, True), ("[redacted]", True, False),
    ]
    assert base_payload(parsed) == {"wpa_key": "[redacted]"}
    revealed = parse_page("dosprotect", f'<input type="{kind}" name="wpa_key" checked>', include_secrets=True)
    assert base_payload(revealed) == {"wpa_key": "on"}


def test_other_input_types_keep_empty_default_values():
    parsed = parse_page("dosprotect", '<input name="plain"><input type="hidden" name="hidden">')
    assert base_payload(parsed) == {"plain": "", "hidden": ""}


def test_mac_filter_ssid_checkboxes_use_browser_values_and_stay_out_of_snapshots():
    # Minimal non-secret fragment from the gateway's MAC Filtering form.
    parsed = parse_page(
        "wmacauth",
        '<form method="post" action="/cgi-bin/wmacauth.ha">'
        '<select name="wmacr1user" disabled><option value="none" selected>Disabled</option></select>'
        '<input id="ssid11" type="checkbox" name="ssid11" checked="checked" />'
        '<input id="ssid12" type="checkbox" name="ssid12" checked="checked" />'
        '<input id="ssid21" type="checkbox" name="ssid21" checked="checked" />'
        '<input type="submit" name="Save" value="Save"></form>',
    )
    assert build_submit_plan("wmacauth", parsed, "Save", []).raw_payload == {
        "ssid11": "on", "ssid12": "on", "ssid21": "on", "Save": "Save",
    }
    snapshot = extract_snapshot({"wmacauth": parsed}, ts="t", router_host="r", include=("wmacauth",))
    assert snapshot.forms["wmacauth"] == {}


@pytest.mark.parametrize("kind", ["checkbox", "radio"])
@pytest.mark.parametrize("attribute, expected", [("", "on"), (' value=""', "")])
def test_snapshot_restore_and_postcondition_keep_checkable_submit_values(kind, attribute, expected):
    def html(checked):
        return (
            f'<input type="{kind}" name="enabled"{attribute}{" checked" if checked else ""}>'
            '<input type="submit" name="Save" value="Save">'
        )

    checked_page = parse_page("dosprotect", html(True))
    unchecked_page = parse_page("dosprotect", html(False))
    checked = extract_snapshot({"dosprotect": checked_page}, ts="t", router_host="r")
    unchecked = extract_snapshot({"dosprotect": unchecked_page}, ts="t", router_host="r")
    assert checked.forms["dosprotect"] == {"enabled": expected}
    assert unchecked.forms["dosprotect"] == {"enabled": UNCHECKED}

    turn_on = build_restore_plan(
        diff_snapshots(checked, unchecked), checked, {"dosprotect": unchecked_page},
        RestoreOptions(pages=("dosprotect",)),
    )
    step = next(step for step in turn_on if step.kind == "form")
    assert step.raw_payload == {"enabled": expected, "Save": "Save"}
    assert _postcondition_matches(step, html(True))
    assert not _postcondition_matches(step, html(False))

    turn_off = build_restore_plan(
        diff_snapshots(unchecked, checked), unchecked, {"dosprotect": checked_page},
        RestoreOptions(pages=("dosprotect",)),
    )
    step = next(step for step in turn_off if step.kind == "form")
    assert step.raw_payload == {"Save": "Save"}
    assert _postcondition_matches(step, html(False))
    assert not _postcondition_matches(step, html(True))
