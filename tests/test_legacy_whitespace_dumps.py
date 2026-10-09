"""Dumps written before form data kept textarea text raw and U+00A0 in attribute values stored the
textarea text whitespace-normalised and U+00A0 as a plain space. Such a dump must not diff (or plan
a restore) against the unchanged router forever."""

import json
from dataclasses import replace

from integration_html import EMPTY_SECTION_TABLES
from save_helpers import client_with, html, install_clock

from bgwcli import cli
from bgwcli.html import normalize_whitespace
from bgwcli.parser import parse_page
from bgwcli.restore import RestoreOptions, build_restore_plan
from bgwcli.snapshot import extract_snapshot
from bgwcli.snapshot_diff import diff_snapshots

NBSP = "\xa0"


def dos(note="\nline1\n  two  words\n", label=f"a{NBSP}b", choice=f"x{NBSP}y"):
    return (
        '<form action="/cgi-bin/dosprotect.ha"><input name="nonce" value="abc123">'
        f'<textarea name="note">{note}</textarea><input type="text" name="label" value="{label}">'
        f'<select name="choice"><option value="{choice}" selected>X</option><option value="z">Z</option></select>'
        '<input type="submit" name="Save" value="Save"></form>'
    )


def live(page_html=None):
    pages = {"dosprotect": parse_page("dosprotect", page_html or dos(), include_secrets=True)}
    return pages, extract_snapshot(pages, ts="", router_host="")


def legacy_dump(**overrides):
    _, current = live()
    form = dict(current.forms["dosprotect"])
    # What the earlier parser stored for the same page.
    form["note"] = normalize_whitespace(form["note"])
    form["label"] = form["label"].replace(NBSP, " ")
    form["choice"] = form["choice"].replace(NBSP, " ")
    form.update(overrides)
    return replace(current, forms={"dosprotect": form})


def test_live_values_really_differ_from_the_legacy_spelling():
    _, current = live()
    assert current.forms["dosprotect"]["note"] == "line1\n  two  words\n"
    assert current.forms["dosprotect"]["label"] == f"a{NBSP}b"


def test_legacy_canonicalised_dump_is_identical_to_the_unchanged_router():
    pages, current = live()
    dump = legacy_dump()
    diff = diff_snapshots(dump, current)
    assert diff.identical
    assert [s for s in build_restore_plan(diff, dump, pages, RestoreOptions()) if s.page == "dosprotect"] == []


def test_a_real_change_still_differs_after_legacy_canonicalisation():
    _, current = live()
    diff = diff_snapshots(legacy_dump(note="line1 other words", label="a c"), current)
    assert [c.field for c in diff.forms["dosprotect"]] == ["label", "note"]


def test_whitespace_collapse_is_only_forgiven_for_textareas():
    # A text input never had its whitespace collapsed; " a  b " vs "a b" is a real edit.
    _, current = live(dos(label=" a  b "))
    diff = diff_snapshots(legacy_dump(label="a b"), current)
    assert [c.field for c in diff.forms["dosprotect"]] == ["label"]


def test_cli_diff_of_a_legacy_dump_exits_0_without_posting(monkeypatch, tmp_env, capsys):
    from bgwcli.dumpfile import write_dump_file

    install_clock(monkeypatch)
    path = tmp_env / "legacy.json"
    write_dump_file(path, legacy_dump())
    empty = (
        '<form action="/cgi-bin/x.ha"><input name="nonce" value="abc123"><input name="note" value=""></form>'
        + EMPTY_SECTION_TABLES
    )
    client, wire = client_with(lambda request, n: html(dos() if "dosprotect" in request.url else empty))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["diff", str(path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["identical"] is True
    assert sum(r.method == "POST" for r in wire.requests) == 0
