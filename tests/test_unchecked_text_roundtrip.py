"""A literal "<unchecked>" text value survives dump -> load -> diff -> restore; only a checkbox/radio
that was off carries the marker meaning."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from page_builders import checkbox, dosprotect_page, field

from bgwcli.dumpfile import read_dump_file, write_dump_file
from bgwcli.errors import DumpFileError
from bgwcli.restore import RestoreOptions, build_restore_plan
from bgwcli.snapshot import UNCHECKED, extract_snapshot
from bgwcli.snapshot_diff import diff_snapshots

OPTIONS = RestoreOptions(prune=False, include_secrets=False, pages=("dosprotect",))


def _pages(fields):
    return {"dosprotect": dosprotect_page(fields=fields)}


def _capture(fields):
    pages = _pages(fields)
    return pages, extract_snapshot(pages, ts="t", router_host="r")


def _round_trip(snapshot, tmp_path):
    path = tmp_path / "dump.json"
    write_dump_file(path, snapshot)
    return read_dump_file(path), path


def _plan(dump, live_pages):
    live = extract_snapshot(live_pages, ts="t", router_host="r")
    diff = diff_snapshots(dump, live, pages=OPTIONS.pages)
    return diff, build_restore_plan(diff, dump, live_pages, OPTIONS)


def _posts(steps):
    return [s for s in steps if s.kind == "form" and s.raw_payload is not None and s.blocked is None]


def test_unchecked_checkbox_still_posts_the_box_absent(tmp_path):
    _, snap = _capture([field("label", "text", "Home"), checkbox("reflexive", "on")])
    dump, _ = _round_trip(snap, tmp_path)
    live_pages = _pages([field("label", "text", "Home"), checkbox("reflexive", "on", checked=True)])
    _, steps = _plan(dump, live_pages)
    posts = _posts(steps)
    assert len(posts) == 1
    assert "reflexive" not in posts[0].raw_payload
    assert posts[0].raw_payload["label"] == "Home"


def test_text_control_with_literal_unchecked_value_is_posted_as_text(tmp_path):
    _, snap = _capture([field("ssid", "text", UNCHECKED), checkbox("reflexive", "on")])
    dump, path = _round_trip(snap, tmp_path)
    assert json.loads(path.read_text())["formUncheckedText"] == {"dosprotect": ["ssid"]}
    _, steps = _plan(dump, _pages([field("ssid", "text", "Other"), checkbox("reflexive", "on")]))
    posts = _posts(steps)
    assert len(posts) == 1
    assert posts[0].raw_payload["ssid"] == UNCHECKED
    assert not any(s.kind == "skip" for s in steps)


def test_literal_text_dump_against_unchanged_live_text_is_identical(tmp_path):
    pages, snap = _capture([field("ssid", "text", UNCHECKED)])
    dump, _ = _round_trip(snap, tmp_path)
    diff, steps = _plan(dump, pages)
    assert diff.identical and steps == []


def test_diff_tells_a_literal_text_value_from_an_unchecked_box(tmp_path):
    # Same stored string, different kinds: the dump held text, the router now has an off checkbox.
    _, snap = _capture([field("ssid", "text", UNCHECKED)])
    dump, _ = _round_trip(snap, tmp_path)
    diff, _ = _plan(dump, _pages([checkbox("ssid", "on")]))
    assert [c.field for c in diff.forms.get("dosprotect", [])] == ["ssid"]


def test_legacy_dump_without_type_info_skips_a_text_field_with_a_warning(tmp_path):
    _, snap = _capture([field("ssid", "text", UNCHECKED)])
    _, path = _round_trip(snap, tmp_path)
    data = json.loads(path.read_text())
    data.pop("formUncheckedText")
    path.write_text(json.dumps(data))
    legacy = read_dump_file(path)
    assert legacy.form_unchecked_text == {}
    _, steps = _plan(legacy, _pages([field("ssid", "text", "Other")]))
    assert _posts(steps) == []
    skips = [s for s in steps if s.kind == "skip"]
    assert skips and "ssid" in skips[0].description
    assert skips[0].warning and "ssid" in skips[0].warning


@pytest.mark.parametrize("name", ["key11", "ssid"])
def test_legacy_skip_warning_never_prints_the_field_value(tmp_path, name):
    import io

    from bgwcli.format import print_restore_plan
    from bgwcli.types import to_json_dict

    _, snap = _capture([field(name, "text", UNCHECKED)])
    _, path = _round_trip(snap, tmp_path)
    data = json.loads(path.read_text())
    data.pop("formUncheckedText")
    path.write_text(json.dumps(data))
    legacy = read_dump_file(path)
    _, steps = _plan(legacy, _pages([field(name, "text", "Other")]))
    assert _posts(steps) == []
    skips = [s for s in steps if s.kind == "skip"]
    assert skips and skips[0].warning
    warning = skips[0].warning
    assert name in warning
    assert "holds <unchecked>" not in warning
    assert UNCHECKED not in warning
    assert "unchecked-marker text" in warning
    out = io.StringIO()
    print_restore_plan(steps, out)
    assert f"warning: {warning}" in out.getvalue()
    rendered = json.dumps(to_json_dict(skips[0]))
    assert "holds <unchecked>" not in rendered
    assert name in rendered


def test_a_dump_whose_only_unchecked_values_are_off_boxes_keeps_the_old_file_shape(tmp_path):
    _, snap = _capture([field("ssid", "text", "Home"), checkbox("reflexive", "on")])
    assert snap.forms["dosprotect"]["reflexive"] == UNCHECKED
    _, path = _round_trip(snap, tmp_path)
    assert "formUncheckedText" not in json.loads(path.read_text())


def test_malformed_form_unchecked_text_is_refused(tmp_path):
    _, snap = _capture([checkbox("reflexive", "on")])
    _, path = _round_trip(snap, tmp_path)
    data = json.loads(path.read_text())
    data["formUncheckedText"] = {"dosprotect": "ssid"}
    path.write_text(json.dumps(data))
    with pytest.raises(DumpFileError, match="formUncheckedText"):
        read_dump_file(path)


def test_capture_records_text_fields_holding_the_string_and_not_off_boxes():
    _, snap = _capture([field("ssid", "text", UNCHECKED), checkbox("reflexive", "on")])
    assert snap.form_unchecked_text == {"dosprotect": ["ssid"]}
    assert replace(snap, form_unchecked_text={}).form_unchecked_text == {}


def test_legacy_dump_against_unchanged_literal_text_router_stays_identical(tmp_path):
    pages, snap = _capture([field("ssid", "text", UNCHECKED)])
    legacy = replace(snap, form_unchecked_text={})
    diff, _ = _plan(legacy, pages)
    assert diff.identical
