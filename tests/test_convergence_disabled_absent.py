"""Dumped controls the live page does not render, or renders disabled, must not hold diff, restore or
autorestore open forever, and a disabled live control is never awaited after a save."""

import json
from dataclasses import replace

import pytest
import test_autorestore as T
from integration_html import EMPTY_SECTION_TABLES
from save_helpers import client_with, html, install_clock

from bgwcli import autorestore, cli
from bgwcli.autorestore import AutorestoreOptions, detect_factory_reset, run_autorestore
from bgwcli.parser import parse_page
from bgwcli.recovery_state import RecoveryCheckpoint
from bgwcli.restore import RestoreOptions, build_restore_plan, restore_converged
from bgwcli.snapshot import UNCHECKED, extract_snapshot
from bgwcli.snapshot_diff import diff_snapshots

# The hidden nonce alone is not a form control; the readable page needs one real input.
EMPTY = (
    '<form action="/cgi-bin/x.ha"><input name="nonce" value="abc123"><input name="note" value=""></form>'
    + EMPTY_SECTION_TABLES
)


def dos(extra=""):
    return (
        '<form action="/cgi-bin/dosprotect.ha"><input name="nonce" value="abc123">'
        f'<input type="text" name="bar" value="0">{extra}'
        '<input type="submit" name="Save" value="Save"></form>'
    )


DISABLED = '<input type="checkbox" name="box" disabled><input type="text" name="txt" value="b" disabled>'


def dump_file(directory, form):
    data = {
        "meta": {"schema": 2, "firmware": "", "ts": "", "routerHost": "router.local"},
        "services": [], "forwards": [], "reservations": [], "forms": {"dosprotect": form}, "tables": {},
    }
    path = directory / "dump.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def env(monkeypatch, tmp_env):
    install_clock(monkeypatch)
    monkeypatch.setattr(autorestore, "_sleep", lambda s: None, raising=False)
    return tmp_env


def run(monkeypatch, capsys, argv, page_html):
    client, wire = client_with(lambda request, n: html(page_html if "dosprotect" in request.url else EMPTY))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--json"])
    out = capsys.readouterr().out
    posts = sum(r.method == "POST" and "login" not in r.url for r in wire.requests)
    return code, json.loads(out), posts


def live(page_html):
    pages = {"dosprotect": parse_page("dosprotect", page_html, include_secrets=True)}
    return pages, extract_snapshot(pages, ts="", router_host="")


def dump_of(form):
    _, base = live(dos())
    return replace(base, forms={"dosprotect": form})


# --- an <unchecked> dump value for a control the live page lacks or renders disabled ---------------


@pytest.mark.parametrize("page_html,name", [(dos(), "ghostbox"), (dos(DISABLED), "box")])
def test_unchecked_dump_value_for_unrendered_or_disabled_control_is_no_difference(page_html, name):
    pages, current = live(page_html)
    dump = dump_of({"bar": "0", name: UNCHECKED})
    diff = diff_snapshots(dump, current)
    assert diff.identical
    assert restore_converged(diff, False)
    assert [s for s in build_restore_plan(diff, dump, pages, RestoreOptions()) if s.page == "dosprotect"] == []


def test_checked_dump_value_for_unrendered_control_is_reported_but_does_not_hold_convergence():
    _, current = live(dos())
    dump = dump_of({"bar": "0", "ghostbox": "on"})
    diff = diff_snapshots(dump, current)
    assert not diff.identical  # still reported: the dump has a value the live page cannot show
    assert restore_converged(diff, False)
    assert detect_factory_reset(diff, dump, on_any_diff=True) == (False, "nothing restore can act on differs")


# --- a dumped value for a control the live page does not render at all ------------------------------


def test_unrendered_dumped_field_is_noted_and_the_rest_of_the_page_is_still_planned():
    pages, current = live(dos())
    dump = dump_of({"bar": "1", "ghost": "x"})
    diff = diff_snapshots(dump, current)
    assert not restore_converged(diff, False)  # bar still differs
    steps = [s for s in build_restore_plan(diff, dump, pages, RestoreOptions()) if s.page == "dosprotect"]
    note = [s for s in steps if s.kind == "skip"]
    form = [s for s in steps if s.kind == "form"]
    assert len(note) == 1 and "ghost" in note[0].description and "does not render" in note[0].description
    assert len(form) == 1 and form[0].blocked is None
    assert form[0].raw_payload == {"bar": "1", "Save": "Save"}
    assert "ghost" not in form[0].description
    assert form[0].postcondition.form_fields == ("bar",)


def test_cli_restore_posts_the_rendered_siblings_once_and_converges_despite_an_unrendered_field(
    env, monkeypatch, capsys
):
    path = dump_file(env, {"bar": "1", "ghost": "x"})
    state = {"bar": "0", "saved": False}

    def handler(request, n):
        if "dosprotect" not in request.url:
            return html(EMPTY)
        if request.method == "POST":
            state["bar"], state["saved"] = "1", True
            return html("", status=302, headers={"location": "/cgi-bin/dosprotect.ha"})
        banner = SAVED if state["saved"] else ""
        return html(banner + dos().replace('value="0"', f'value="{state["bar"]}"'))

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["restore", str(path), "--commit", "--confirm", "RESTORE", "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and "login" not in r.url]
    assert len(posts) == 1
    assert b"bar=1" in posts[0].body and b"ghost" not in posts[0].body
    statuses = [(s["page"], s["kind"], s["status"]) for s in out["execution"]["steps"]]
    assert ("dosprotect", "form", "applied") in statuses
    assert ("dosprotect", "skip", "skipped") in statuses
    assert code == 0


@pytest.mark.parametrize("page_html,name", [(dos(), "ghostbox"), (dos(DISABLED), "box")])
def test_cli_diff_and_restore_converge_on_unrendered_unchecked_control(env, monkeypatch, capsys, page_html, name):
    path = dump_file(env, {"bar": "0", name: UNCHECKED})
    code, out, posts = run(monkeypatch, capsys, ["diff", str(path)], page_html)
    assert (code, posts) == (0, 0)
    assert out["identical"] is True
    code, out, posts = run(monkeypatch, capsys, ["restore", str(path), "--commit", "--confirm", "RESTORE"], page_html)
    assert (code, posts) == (0, 0)
    assert out["diff"]["identical"] is True


def test_autorestore_on_any_diff_is_no_reset_for_unrendered_unchecked_control(env, monkeypatch, capsys):
    path = dump_file(env, {"bar": "0", "ghostbox": UNCHECKED})
    code, out, posts = run(
        monkeypatch, capsys,
        ["autorestore", str(path), "--commit", "--confirm", "RESTORE", "--on-any-diff", "--max-passes", "2",
         "--wait", "0"],
        dos(),
    )
    assert (code, posts) == (0, 0)
    assert out["status"] == "no-reset"


def test_autorestore_recovery_with_unrendered_unchecked_control_converges_and_clears_intent(tmp_path, monkeypatch):
    install_clock(monkeypatch)
    base = T.make_dump()
    dump = replace(base, forms={**base.forms, "dosprotect": {**base.forms["dosprotect"], "ghostbox": UNCHECKED}})
    checkpoint = RecoveryCheckpoint("http://router.local", dump, None, root=tmp_path / "state" / "bgw" / "recovery")
    router = T.FakeRouter()
    result = run_autorestore(
        lambda: (router, False), dump, AutorestoreOptions(commit=True, max_passes=3, wait_seconds=120),
        fetch_pages=T.Fetcher(T.reset_pages(), T.full_pages()), sleep=lambda s: None, log=lambda line: None,
        checkpoint=checkpoint,
    )
    assert result.status == "converged"
    assert not checkpoint.is_active()
    assert len(result.passes) == 1
    posts = len(router.posts)
    again = run_autorestore(
        lambda: (router, False), dump, AutorestoreOptions(commit=True, max_passes=3, wait_seconds=120),
        fetch_pages=T.Fetcher(T.full_pages()), sleep=lambda s: None, log=lambda line: None, checkpoint=checkpoint,
    )
    assert again.status == "no-reset"
    assert len(router.posts) == posts


# --- a dumped value for a control the live page renders disabled (no enabled change) ---------------


def test_disabled_live_text_field_gets_a_blocked_step_and_does_not_hold_convergence():
    pages, current = live(dos(DISABLED))
    dump = dump_of({"bar": "0", "txt": "a"})
    diff = diff_snapshots(dump, current)
    assert not diff.identical  # still reported: the dump's value is not what the page shows
    assert restore_converged(diff, False)
    steps = [s for s in build_restore_plan(diff, dump, pages, RestoreOptions()) if s.page == "dosprotect"]
    assert len(steps) == 1
    assert steps[0].blocked is not None and "disabled" in steps[0].blocked and "txt" in steps[0].blocked
    assert steps[0].raw_payload is None
    assert detect_factory_reset(diff, dump, on_any_diff=True) == (False, "nothing restore can act on differs")


def test_cli_restore_with_only_a_disabled_live_difference_posts_nothing_and_exits_0(env, monkeypatch, capsys):
    path = dump_file(env, {"bar": "0", "txt": "a"})
    argv = ["restore", str(path), "--commit", "--confirm", "RESTORE"]
    code, out, posts = run(monkeypatch, capsys, argv, dos(DISABLED))
    assert (code, posts) == (0, 0)
    statuses = [(s["page"], s["status"]) for s in out["execution"]["steps"]]
    assert ("dosprotect", "blocked") in statuses
    code, out, posts = run(monkeypatch, capsys, ["diff", str(path)], dos(DISABLED))
    assert (code, posts) == (1, 0)


# --- a live-disabled dumped field posted together with an enabled change ---------------------------

SAVED = '<div id="error-message-text" style="color: red">Changes saved</div>'
WC = (
    '<form action="/cgi-bin/wconfig.ha"><input name="nonce" value="abc123">'
    '<input type="text" name="ssid" value="{v}"><input type="submit" name="Save" value="Save"></form>'
)


def dos_txt(bar, txt="b", disabled=True, banner=""):
    state = " disabled" if disabled else ""
    return (
        f'{banner}<form action="/cgi-bin/dosprotect.ha"><input name="nonce" value="abc123">'
        f'<input type="text" name="bar" value="{bar}"><input type="text" name="txt" value="{txt}"{state}>'
        '<input type="submit" name="Save" value="Save"></form>'
    )


def test_disabled_dumped_field_is_posted_but_not_awaited_and_later_steps_run(monkeypatch, tmp_env, capsys):
    clock = install_clock(monkeypatch, timeout=60.0, poll=1.0)
    data = {
        "meta": {"schema": 2, "firmware": "", "ts": "", "routerHost": "r"}, "services": [], "forwards": [],
        "reservations": [], "forms": {"dosprotect": {"bar": "1", "txt": "a"}, "wconfig": {"ssid": "home"}},
        "tables": {},
    }
    path = tmp_env / "d.json"
    path.write_text(json.dumps(data))
    state = {"bar": "0", "saved": False, "ssid": "def"}

    def handle(request, n):
        if request.method == "POST":
            if "dosprotect" in request.url:
                state["bar"], state["saved"] = "1", True
                return html("", status=302, headers={"location": "/cgi-bin/dosprotect.ha"})
            state["ssid"] = "home"
            return html(SAVED + WC.format(v="home"))
        if "dosprotect" in request.url:
            return html(dos_txt(state["bar"], banner=SAVED if state["saved"] else ""))
        if "wconfig" in request.url:
            return html(WC.format(v=state["ssid"]))
        return html(EMPTY)

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["restore", str(path), "--commit", "--confirm", "RESTORE", "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST"]
    assert len(posts) == 2
    assert b"txt=a" in posts[0].body  # still posted: the same save may re-enable it
    statuses = {s["page"]: s["status"] for s in out["execution"]["steps"]}
    assert statuses["dosprotect"] == "applied" and statuses["wconfig"] == "applied"
    assert sum(clock.sleeps) < 60  # never waited out the acknowledgement timeout
    assert code == 0


@pytest.mark.parametrize("after,expected", [
    (dos_txt("1", "b", disabled=True), True),  # still disabled: cannot be read back, not awaited
    (dos_txt("1", "a", disabled=False), True),  # re-enabled with the dumped value: verified
    (dos_txt("1", "b", disabled=False), False),  # re-enabled with another value: not the requested state
])
def test_postcondition_verifies_a_disabled_dumped_field_only_when_it_reads_back_enabled(after, expected):
    from bgwcli.restore import _postcondition_matches

    pages, current = live(dos_txt("0"))
    dump = dump_of({"bar": "1", "txt": "a"})
    step = next(
        s for s in build_restore_plan(diff_snapshots(dump, current), dump, pages, RestoreOptions())
        if s.page == "dosprotect"
    )
    assert step.raw_payload["txt"] == "a"
    assert _postcondition_matches(step, after) is expected


# --- a healthy timer run is one journal line --------------------------------------------------------


def test_healthy_autorestore_text_run_logs_a_single_no_reset_line(env, monkeypatch, capsys):
    path = dump_file(env, {"bar": "0"})
    client, wire = client_with(lambda request, n: html(dos() if "dosprotect" in request.url else EMPTY))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["autorestore", str(path), "--commit", "--confirm", "RESTORE"])
    captured = capsys.readouterr()
    lines = [line for line in (captured.out + captured.err).splitlines() if line.strip()]
    assert code == 0
    assert lines == ["no-reset: no differences"]
    assert sum(r.method == "POST" for r in wire.requests) == 0


def test_drifted_no_reset_text_run_still_prints_the_drift(env, monkeypatch, capsys):
    path = dump_file(env, {"bar": "1"})
    data = json.loads(path.read_text())
    data["forms"]["wconfig"] = {}  # one of two form pages differs: ordinary drift, not a reset
    path.write_text(json.dumps(data))
    client, wire = client_with(lambda request, n: html(dos() if "dosprotect" in request.url else EMPTY))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["autorestore", str(path), "--commit", "--confirm", "RESTORE"])
    captured = capsys.readouterr()
    assert code == 0
    assert "~ bar: 0 -> 1" in captured.out
    assert sum(r.method == "POST" for r in wire.requests) == 0


def test_a_box_the_save_disables_satisfies_the_unchecked_post_save_check(env, monkeypatch, capsys):
    # The dump has box off; the save changes mode, which makes the page render box disabled (the
    # form extractor then omits it). A box that is no longer rendered enabled is not on.
    from save_helpers import SAVED_RED

    def page(mode, box, banner=""):
        options = "".join(f'<option value="{v}"{" selected" if v == mode else ""}>{v}</option>' for v in "ab")
        return (
            f'{banner}<form action="/cgi-bin/dosprotect.ha"><input name="nonce" value="abc123">'
            f'<select name="mode">{options}</select><input type="checkbox" name="box" {box}>'
            '<input type="submit" name="Save" value="Save"></form>'
        )

    path = dump_file(env, {"mode": "b", "box": UNCHECKED})
    state = {"saved": False}

    def handler(request, n):
        if "dosprotect" not in request.url:
            return html(EMPTY)
        if request.method == "POST":
            state["saved"] = True
        return html(page("b", "disabled", SAVED_RED) if state["saved"] else page("a", "checked"))

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["restore", str(path), "--commit", "--confirm", "RESTORE", "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and "login" not in r.url]
    assert len(posts) == 1
    assert ("dosprotect", "applied") in [(s["page"], s["status"]) for s in out["execution"]["steps"]]
    assert code == 0


# --- a radio group with one disabled member is not a disabled field ---------------------------------

RADIO_ONE_DISABLED = (
    '<form action="/cgi-bin/dosprotect.ha"><input name="nonce" value="abc123">'
    '<input type="radio" name="mode" value="a" checked><input type="radio" name="mode" value="b">'
    '<input type="radio" name="mode" value="c" disabled>'
    '<input type="submit" name="Save" value="Save"></form>'
)


def test_radio_group_with_one_disabled_member_is_planned_as_an_enabled_field():
    pages, current = live(RADIO_ONE_DISABLED)
    dump = dump_of({"mode": "b"})
    diff = diff_snapshots(dump, current)
    steps = [s for s in build_restore_plan(diff, dump, pages, RestoreOptions()) if s.page == "dosprotect"]
    assert len(steps) == 1 and steps[0].blocked is None
    assert steps[0].raw_payload == {"mode": "b", "Save": "Save"}
    assert steps[0].postcondition.form_fields == ("mode",)
    assert steps[0].postcondition.if_enabled_fields == ()
