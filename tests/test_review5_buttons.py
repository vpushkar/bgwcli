"""Fifth review round, buttons and form ownership:

- two enabled submits with one name and different values are ambiguous: nothing is posted;
- a toggle's "already in that state" answer needs the opposite button to be the page's own enabled
  submit (not a reset control, not a button of another form);
- the planner (parse_page) and the nonce-page check (_FormScanner) read a <button> caption and an
  empty comment the same way;
- generic submit posts only the controls the chosen button's form owns (a browser's form owner,
  including controls attached with form="id"), never a sibling form's fields.
"""

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from save_helpers import client_with, html

from bgwcli import cli
from bgwcli.client import _scan_forms
from bgwcli.errors import UsageError
from bgwcli.mutations import build_submit_plan
from bgwcli.parser import parse_page


def _cli(monkeypatch, capsys, argv, handler):
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--json"])
    captured = capsys.readouterr()
    out = json.loads(captured.out) if captured.out.strip() else {}
    posts = [r for r in wire.requests if r.method == "POST" and "/login.ha" not in r.url]
    return code, out, posts, captured


def _serve(page, after=None):
    state = {"posted": False}

    def handler(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html("", 302, {"location": f"/cgi-bin/{urlsplit(request.url).path.rsplit('/', 1)[-1]}"})
        return html(after if state["posted"] and after is not None else page)

    return handler


# --- 1c: ambiguous same-name submits ------------------------------------------------------------------


def test_two_enabled_clear_buttons_with_different_values_post_nothing(tmp_env, clock, monkeypatch, capsys):
    page = (
        '<form method="post" action="/cgi-bin/devices.ha"><input type="hidden" name="nonce" value="abc123" />'
        '<input type="submit" name="Clear" value="Clear and Rescan for Devices" />'
        '<input type="submit" name="Clear" value="Clear Everything" /></form>'
    )
    code, out, posts, captured = _cli(monkeypatch, capsys, ["action", "clear-device-list", "--commit"], _serve(page))
    assert posts == []
    assert code != 0
    assert "more than once" in (captured.err + json.dumps(out))


def test_two_clear_buttons_with_the_same_value_are_one_button(tmp_env, clock, monkeypatch, capsys):
    button = '<input type="submit" name="Clear" value="Clear and Rescan for Devices" />'
    page = (
        '<form method="post" action="/cgi-bin/devices.ha"><input type="hidden" name="nonce" value="abc123" />'
        f"{button}{button}</form>"
    )
    code, _, posts, _ = _cli(monkeypatch, capsys, ["action", "clear-device-list", "--commit"], _serve(page))
    assert code == 0 and len(posts) == 1
    assert parse_qs(posts[0].body.decode()) == {"Clear": ["Clear and Rescan for Devices"], "nonce": ["abc123"]}


def test_a_second_clear_value_on_the_nonce_page_refuses_the_write(tmp_env, clock, monkeypatch, capsys):
    first = (
        '<form method="post" action="/cgi-bin/devices.ha"><input type="hidden" name="nonce" value="abc123" />'
        '<input type="submit" name="Clear" value="Clear and Rescan for Devices" /></form>'
    )
    reread = first.replace("</form>", '<input type="submit" name="Clear" value="Clear Everything" /></form>')
    reads = {"n": 0}

    def handler(request, n):
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/devices.ha"})
        if urlsplit(request.url).path.endswith("devices.ha"):
            reads["n"] += 1
            return html(first if reads["n"] == 1 else reread)
        return html(first)

    code, _, posts, _ = _cli(monkeypatch, capsys, ["action", "clear-device-list", "--commit"], handler)
    assert posts == [] and code != 0


# --- 1d: the opposite toggle must be the page's own enabled submit ----------------------------------


def _pf(inner: str, extra: str = "") -> str:
    return (
        '<form method="post" action="/cgi-bin/packetfilter.ha"><input type="hidden" name="nonce" value="abc123" />'
        f'{inner}<input type="submit" name="Complete_16" value="" title="Enable/Disable Rule" /></form>{extra}'
    )


@pytest.mark.parametrize(
    "page",
    [
        _pf('<input type="reset" name="Disable" value="Disable Packet Filters" />'),
        _pf(
            "",
            '<form method="post" action="/cgi-bin/other.ha"><input type="hidden" name="nonce" value="ff00" />'
            '<input type="submit" name="Disable" value="Disable Packet Filters" /></form>',
        ),
        _pf('<button type="button" name="Disable">Disable Packet Filters</button>'),
    ],
    ids=["reset-control", "foreign-form", "plain-button"],
)
def test_enable_is_not_reported_unchanged_from_a_disable_that_is_no_own_submit(
    tmp_env, clock, monkeypatch, capsys, page
):
    argv = ["action", "packet-filter-enable", "--commit", "--confirm", "PACKETFILTER"]
    code, out, posts, _ = _cli(monkeypatch, capsys, argv, _serve(page))
    assert posts == []
    assert out.get("outcome") != "unchanged"
    assert code != 0


def test_enable_is_unchanged_when_the_own_form_offers_disable(tmp_env, clock, monkeypatch, capsys):
    page = _pf('<input type="submit" name="Disable" value="Disable Packet Filters" />')
    argv = ["action", "packet-filter-enable", "--commit", "--confirm", "PACKETFILTER"]
    code, out, posts, _ = _cli(monkeypatch, capsys, argv, _serve(page))
    assert code == 0 and posts == [] and out.get("outcome") == "unchanged"


def test_a_reset_control_named_like_the_action_button_is_not_posted(tmp_env, clock, monkeypatch, capsys):
    page = _pf('<input type="reset" name="Enable" value="Enable Packet Filters" />')
    argv = ["action", "packet-filter-enable", "--commit", "--confirm", "PACKETFILTER"]
    code, _, posts, _ = _cli(monkeypatch, capsys, argv, _serve(page))
    assert posts == [] and code != 0


# --- 1e: one reading of captions and empty comments --------------------------------------------------


@pytest.mark.parametrize(
    "caption",
    [
        "Clear<br>devices", "Clear<br/>devices", "<b>Clear</b> devices", "Clear\n  devices",
        "Clear<script>x</script> devices",
    ],
)
def test_the_scanner_reads_a_button_caption_like_the_planner(caption):
    page = (
        '<form method="post" action="/cgi-bin/devices.ha"><input name="nonce" value="aa11">'
        f'<button name="Clear">{caption}</button></form>'
    )
    planned = parse_page("devices", page).buttons[0]
    assert _scan_forms(page)[0][3] == {"Clear": planned.value or planned.label}


def test_a_literal_empty_comment_inside_a_quoted_value_is_kept():
    page = (
        '<form method="post" action="/cgi-bin/devices.ha"><input name="nonce" value="aa11">'
        '<input type="submit" name="Clear" value="a<!-->b"></form>'
    )
    planned = parse_page("devices", page).buttons[0]
    assert planned.value == "a<!-->b"
    assert _scan_forms(page)[0][3] == {"Clear": "a<!-->b"}


@pytest.mark.parametrize("empty", ["<!-->", "<!--->"])
def test_the_planner_sees_a_button_after_an_empty_comment(empty):
    page = (
        f'{empty}<form method="post" action="/cgi-bin/devices.ha"><input name="nonce" value="aa11">'
        '<input type="submit" name="Clear" value="Go"></form><!-- later -->'
    )
    assert [b.name for b in parse_page("devices", page).buttons] == ["Clear"]
    assert _scan_forms(page)[0][3] == {"Clear": "Go"}


def test_the_clear_action_posts_a_br_caption_the_nonce_check_accepts(tmp_env, clock, monkeypatch, capsys):
    page = (
        '<form method="post" action="/cgi-bin/devices.ha"><input type="hidden" name="nonce" value="abc123" />'
        '<button name="Clear">Clear and Rescan<br>for Devices</button></form>'
    )
    code, _, posts, _ = _cli(monkeypatch, capsys, ["action", "clear-device-list", "--commit"], _serve(page))
    assert code == 0 and len(posts) == 1
    assert parse_qs(posts[0].body.decode())["Clear"] == ["Clear and Rescan for Devices"]


# --- 2b: generic submit posts only the button's own form ----------------------------------------------


def _plan(page_html, button="Refresh", page="etherlan"):
    return build_submit_plan(page, parse_page(page, page_html, include_secrets=True), button, [])


def test_a_sibling_forms_fields_are_never_posted():
    page = (
        '<form method="post" action="/cgi-bin/etherlan.ha"><input type="hidden" name="nonce" value="abc123" />'
        '<input type="text" name="mode" value="one" /><input type="submit" name="Refresh" value="Refresh" /></form>'
        '<form method="post" action="/cgi-bin/other.ha"><input type="hidden" name="nonce" value="ff00" />'
        '<input type="hidden" name="danger" value="1" /><select name="pick"><option>x</option></select>'
        '<textarea name="note">t</textarea></form>'
    )
    assert _plan(page).raw_payload == {"mode": "one", "Refresh": "Refresh"}


def test_controls_attached_by_form_id_are_posted_and_reassigned_ones_are_not():
    page = (
        '<form id="s" method="post" action="/cgi-bin/etherlan.ha"><input type="hidden" name="nonce" value="abc123" />'
        '<input type="text" name="away" form="o" value="x" />'
        '<input type="submit" name="Refresh" value="Refresh" /></form>'
        '<form id="o" method="post" action="/cgi-bin/other.ha"><input type="hidden" name="nonce" value="ff00" />'
        '<input type="text" name="theirs" value="y" /><input type="text" name="ours" form="s" value="z" /></form>'
        '<input type="text" name="outside" form="s" value="w" /><input type="text" name="loose" value="v" />'
    )
    assert _plan(page).raw_payload == {"ours": "z", "outside": "w", "Refresh": "Refresh"}


def test_a_form_attribute_naming_no_form_leaves_the_control_ownerless():
    page = (
        '<div id="s"></div><form id="s" method="post" action="/cgi-bin/etherlan.ha">'
        '<input type="hidden" name="nonce" value="abc123" />'
        '<input type="text" name="mine" value="1" /><input type="text" name="lost" form="s" value="2" />'
        '<input type="submit" name="Refresh" value="Refresh" /></form>'
    )
    # getElementById finds the <div> first: "lost" has no form owner, so a browser never posts it.
    assert _plan(page).raw_payload == {"mine": "1", "Refresh": "Refresh"}


def test_a_nested_form_tag_is_ignored_and_its_end_tag_closes_the_outer_form():
    page = (
        '<form method="post" action="/cgi-bin/etherlan.ha"><input type="hidden" name="nonce" value="abc123" />'
        '<input type="text" name="a" value="1" /><form action="/cgi-bin/other.ha">'
        '<input type="text" name="b" value="2" /><input type="submit" name="Refresh" value="Refresh" /></form>'
        '<input type="text" name="c" value="3" /></form>'
    )
    # As in a browser: the inner <form> is ignored (b and Refresh belong to the outer form) and the
    # first </form> ends the outer form (c belongs to no form).
    assert _plan(page).raw_payload == {"a": "1", "b": "2", "Refresh": "Refresh"}


def test_a_form_less_page_still_posts_its_controls():
    page = (
        '<input type="hidden" name="nonce" value="abc123" /><input type="text" name="a" value="1" />'
        '<input type="submit" name="Refresh" value="Refresh" />'
    )
    assert _plan(page).raw_payload == {"a": "1", "Refresh": "Refresh"}


def test_ownership_is_not_part_of_the_json_model():
    page = '<form id="s" action="/cgi-bin/etherlan.ha"><input name="a" value="1"><button name="b">B</button></form>'
    from bgwcli.types import to_json_dict

    data = to_json_dict(parse_page("etherlan", page))
    assert set(data["fields"][0]) == {"name", "type", "value", "checked", "sensitive"}
    assert set(data["buttons"][0]) == {"name", "type", "value", "label", "sensitive"}
    assert set(data) == {
        "page", "title", "heading", "values", "tables", "fields", "selects", "textareas", "buttons", "forms",
        "valueEntries", "links",
    }


def test_generic_submit_never_sends_a_sibling_forms_field(tmp_env, clock, monkeypatch, capsys):
    page = (
        '<form method="post" action="/cgi-bin/etherlan.ha"><input type="hidden" name="nonce" value="abc123" />'
        '<input type="submit" name="Refresh" value="Refresh" /></form>'
        '<form method="post" action="/cgi-bin/other.ha"><input type="hidden" name="nonce" value="ff00" />'
        '<input type="hidden" name="danger" value="1" /></form>'
    )
    argv = ["submit", "etherlan", "Refresh", "--commit", "--confirm", "ETHERLAN"]
    _, _, posts, _ = _cli(monkeypatch, capsys, argv, _serve(page))
    assert len(posts) == 1
    assert "danger" not in parse_qs(posts[0].body.decode())


def test_a_button_reassigned_to_a_foreign_form_is_refused_by_generic_submit():
    page = (
        '<form method="post" action="/cgi-bin/etherlan.ha"><input type="hidden" name="nonce" value="abc123" />'
        '<input type="text" name="a" value="1" /><input type="submit" name="Refresh" form="o" value="Refresh" />'
        "</form>"
        '<form id="o" method="post" action="/cgi-bin/other.ha"><input type="hidden" name="nonce" value="ff00" /></form>'
    )
    with pytest.raises(UsageError, match="another form"):
        _plan(page)
