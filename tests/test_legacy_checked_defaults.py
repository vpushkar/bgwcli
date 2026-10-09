"""Old schema-2 files retain browser default/on semantics through restore."""

import json
from dataclasses import replace

import pytest
from integration_html import CHANGES_SAVED_HTML
from test_integration_restore import FakeRouter, Post

from bgwcli.dumpfile import dump_json_text, read_dump_file
from bgwcli.parser import parse_page
from bgwcli.restore import RestoreOptions, build_restore_plan, execute_restore, restore_converged
from bgwcli.snapshot import UNCHECKED, extract_snapshot
from bgwcli.snapshot_diff import diff_snapshots
from bgwcli.types import to_json_dict


def load_old_dump(tmp_path, value=""):
    # The pre-bfcc6f0 parser recorded checked inputs without value as an empty string.
    path = tmp_path / "pre-default-on-schema2.json"
    path.write_text(json.dumps({
        "meta": {"schema": 2, "firmware": "", "ts": "2026-09-20", "routerHost": "synthetic.invalid"},
        "services": [], "forwards": [], "reservations": [],
        "forms": {"dosprotect": {"setting": value}}, "tables": {},
    }))
    return read_dump_file(path)


def document(controls):
    return ('<form method="post" action="/cgi-bin/dosprotect.ha">' + controls
            + '<input type="submit" name="Save" value="Save"></form>')


def current(controls):
    pages = {"dosprotect": parse_page("dosprotect", document(controls), include_secrets=True)}
    return pages, extract_snapshot(pages, ts="", router_host="")


def make_plan(dump, controls):
    pages, live = current(controls)
    delta = diff_snapshots(dump, live, pages=("dosprotect",))
    return delta, build_restore_plan(delta, dump, pages, RestoreOptions(pages=("dosprotect",)))


@pytest.mark.parametrize("kind", ["checkbox", "radio"])
def test_old_checked_default_already_matches_without_post(tmp_path, kind):
    dump = load_old_dump(tmp_path)
    delta, plan = make_plan(dump, f'<input type="{kind}" name="setting" checked>')
    assert delta.identical
    assert plan == []
    router = FakeRouter()
    assert execute_restore(router, plan).steps == []
    assert router.posted == []
    assert dump.forms["dosprotect"]["setting"] == ""  # Never mutate the file's meaning globally.


@pytest.mark.parametrize("kind", ["checkbox", "radio"])
def test_old_checked_default_restores_canonical_payload_and_converges(tmp_path, kind):
    dump = load_old_dump(tmp_path)
    before = f'<input type="{kind}" name="setting">'
    after = f'<input type="{kind}" name="setting" checked>'
    if kind == "radio":
        before += '<input type="radio" name="setting" value="other" checked>'
        after += '<input type="radio" name="setting" value="other">'
    delta, plan = make_plan(dump, before)
    assert delta.forms["dosprotect"][0].dump == "on"
    assert len(plan) == 1 and plan[0].blocked is None
    assert plan[0].raw_payload["setting"] == "on"
    router = FakeRouter(pages={"dosprotect": document(after)}, answer=lambda *_: Post(200, body=CHANGES_SAVED_HTML))
    result = execute_restore(router, plan)
    assert [s.status for s in result.steps] == ["applied"]
    assert router.posted == [("dosprotect", {"setting": "on", "Save": "Save"})]
    assert router.gets == ["dosprotect"]
    _, final = current(after)
    assert restore_converged(diff_snapshots(dump, final, pages=("dosprotect",)), False)


@pytest.mark.parametrize("controls", [
    '<input type="hidden" name="setting" value="">',
    '<input type="text" name="setting" value="">',
    '<input type="checkbox" name="setting" value="" checked>',
    '<input type="radio" name="setting" value="" checked><input type="radio" name="setting">',
])
def test_explicit_empty_and_noncheckable_values_remain_valid(tmp_path, controls):
    delta, plan = make_plan(load_old_dump(tmp_path), controls)
    assert delta.identical and plan == []


def test_explicit_empty_radio_choice_remains_restorable(tmp_path):
    dump = load_old_dump(tmp_path)
    delta, plan = make_plan(dump, '<input type="radio" name="setting" value="">'
                            '<input type="radio" name="setting" checked>')
    assert not delta.identical
    assert plan[0].raw_payload["setting"] == ""


def test_unchecked_sentinel_is_not_normalized(tmp_path):
    dump = load_old_dump(tmp_path, UNCHECKED)
    delta, plan = make_plan(dump, '<input type="checkbox" name="setting" checked>')
    assert delta.forms["dosprotect"][0].dump == UNCHECKED
    assert "setting" not in plan[0].raw_payload
    _, final = current('<input type="checkbox" name="setting">')
    assert diff_snapshots(dump, final, pages=("dosprotect",)).identical


def test_missing_live_control_does_not_invent_compatibility(tmp_path):
    delta, plan = make_plan(load_old_dump(tmp_path), '<input type="text" name="unrelated" value="x">')
    assert not delta.identical
    assert delta.forms["dosprotect"][0].dump == ""
    assert delta.forms["dosprotect"][0].live is None
    # Still reported, but a control the live page does not render can never be posted or read back,
    # so it does not hold restore convergence open.
    assert restore_converged(delta, False)


def test_provenance_stays_out_of_schema2_and_page_json():
    pages, live = current('<input type="checkbox" name="setting" checked>')
    dumped = json.loads(dump_json_text(live))
    assert dumped["forms"] == {"dosprotect": {"setting": "on"}}
    assert set(dumped) == {"meta", "services", "forwards", "reservations", "forms", "tables"}
    assert set(to_json_dict(live)) == set(dumped)
    control = to_json_dict(pages["dosprotect"])["fields"][0]
    assert set(control) == {"name", "type", "value", "checked", "sensitive"}


@pytest.mark.parametrize("control", [
    '<input type="checkbox" name="setting" value="on" checked>',
    '<input type="checkbox" name="setting" checked><input type="hidden" name="setting" value="other">',
    '<input type="radio" name="setting" checked><input type="radio" name="setting" value="">',
])
def test_no_legacy_normalization_without_unambiguous_implicit_control(tmp_path, control):
    delta, _ = make_plan(load_old_dump(tmp_path), control)
    assert not delta.identical
    assert delta.forms["dosprotect"][0].dump == ""


def test_live_metadata_does_not_survive_dump_roundtrip(tmp_path):
    _, live = current('<input type="checkbox" name="setting" checked>')
    path = tmp_path / "current.json"
    path.write_text(dump_json_text(live))
    disk_snapshot = read_dump_file(path)
    # A file alone contains no control-type evidence; no inferred migration offline.
    delta = diff_snapshots(load_old_dump(tmp_path), disk_snapshot, pages=("dosprotect",))
    assert not delta.identical
    assert delta.forms["dosprotect"][0].dump == ""


@pytest.mark.parametrize("copy_form", [dict, lambda form: form.copy(),
                                      lambda form: {name: value for name, value in form.items() if name == "setting"}],
                         ids=["dict", "copy", "projection"])
@pytest.mark.parametrize("checked", [False, True])
def test_copied_live_forms_preserve_legacy_diff_restore_and_verification(tmp_path, copy_form, checked):
    dump = load_old_dump(tmp_path)
    controls = '<input type="checkbox" name="setting"' + (" checked>" if checked else ">")
    pages, live = current(controls)
    copied = replace(live, forms={page: copy_form(form) for page, form in live.forms.items()})
    delta = diff_snapshots(dump, copied, pages=("dosprotect",))
    plan = build_restore_plan(delta, dump, pages, RestoreOptions(pages=("dosprotect",)))
    if checked:
        assert delta.identical
        assert plan == []
    else:
        assert delta.forms["dosprotect"][0].dump == "on"
        assert len(plan) == 1 and plan[0].blocked is None
        after = document('<input type="checkbox" name="setting" checked>')
        router = FakeRouter(pages={"dosprotect": after}, answer=lambda *_: Post(200, body=CHANGES_SAVED_HTML))
        result = execute_restore(router, plan)
        assert [step.status for step in result.steps] == ["applied"]
        assert router.posted == [("dosprotect", {"setting": "on", "Save": "Save"})]
        final = extract_snapshot({"dosprotect": parse_page("dosprotect", after)}, ts="", router_host="")
        final = replace(final, forms={page: copy_form(form) for page, form in final.forms.items()})
        assert restore_converged(diff_snapshots(dump, final, pages=("dosprotect",)), False)


def test_projection_removing_live_control_cannot_normalize_missing_value(tmp_path):
    _, live = current('<input type="checkbox" name="setting" checked>')
    projected = replace(live, forms={"dosprotect": {}})
    delta = diff_snapshots(load_old_dump(tmp_path), projected, pages=("dosprotect",))
    assert not delta.identical
    assert delta.forms["dosprotect"][0].dump == ""
    assert delta.forms["dosprotect"][0].live is None
