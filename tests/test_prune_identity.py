"""A prune removal is bound to its entry, not to the Remove_<n> position it had at planning.

Remove_<n> buttons are positional. Restore planned Remove_1 for custom_ssh; a browser removed custom_ssh
first, so Mosh moved to row 1; the page read for the write nonce showed Mosh at Remove_1 and restore
posted it, deleting Mosh. The removal is now re-resolved by identity on that same page: an entry already
gone is skipped (nothing sent), a moved entry gets its current button, an ambiguous page is refused.
"""

from urllib.parse import parse_qs

from save_helpers import client_with, html

from bgwcli.parser import parse_page
from bgwcli.restore import RestoreOptions, _remove_step, execute_restore

_ROW = (
    "<tr><td>{name}</td><td>{ports}</td><td>{base}</td><td>{proto}</td>"
    '<td><input type="submit" name="Remove_{n}" value="Remove"></td></tr>'
)
_ROWS = {
    "custom_ssh": ("2483-2483", "22", "TCP"),
    "Mosh": ("60001-60010", "60001", "UDP"),
    "Stale": ("1-1", "1", "TCP"),
}


def services_html(*names: str) -> str:
    rows = "".join(
        _ROW.format(name=name, ports=_ROWS[name][0], base=_ROWS[name][1], proto=_ROWS[name][2], n=index)
        for index, name in enumerate(names, start=1)
    )
    return (
        '<html><body><form method="post" action="/cgi-bin/services.ha">'
        '<input type="hidden" name="nonce" value="abc123"><table>'
        "<tr><th>Service Name</th><th>Global Port Range</th><th>Base Host Port</th><th>Protocol</th><th></th></tr>"
        f"{rows}</table>"
        '<input type="text" name="Service" value=""><input type="text" name="extMinPort" value="">'
        '<input type="text" name="extMaxPort" value=""><input type="text" name="intStartPort" value="">'
        '<select name="protocol"><option value="TCP" selected>TCP</option><option value="UDP">UDP</option></select>'
        '<input type="submit" name="Add" value="Add"></form></body></html>'
    )


def _plan(name: str, *planned_rows: str):
    live = {"services": parse_page("services", services_html(*planned_rows), include_secrets=True)}
    step = _remove_step("services", "remove-service", name, live, RestoreOptions(prune=True))
    assert step.blocked is None, step.blocked
    return step.__class__(**{**step.__dict__, "order": 1})


def _run(step, *live_rows: str, after=None):
    state = {"rows": list(live_rows)}

    def handler(request, n):
        if request.method == "POST":
            removed = next(k for k in parse_qs(request.body.decode()) if k.startswith("Remove_"))
            index = int(removed.split("_")[1]) - 1
            state["rows"].pop(index)
            return html("", 302, {"location": "/cgi-bin/services.ha"})
        return html(services_html(*state["rows"]))

    client, wire = client_with(handler)
    execution = execute_restore(client, [step])
    posts = [parse_qs(r.body.decode()) for r in wire.requests if r.method == "POST"]
    return execution.steps[0], posts, state["rows"]


def test_the_planned_button_is_posted_when_the_page_is_unchanged(clock):
    step = _plan("Stale", "custom_ssh", "Stale")
    result, posts, rows = _run(step, "custom_ssh", "Stale")
    assert [k for p in posts for k in p if k.startswith("Remove_")] == ["Remove_2"]
    assert rows == ["custom_ssh"]


def test_a_target_already_gone_is_skipped_and_the_shifted_row_is_kept(clock):
    # Codex's scenario: planned Remove_1 for custom_ssh; a browser removed it; Mosh is now Remove_1.
    step = _plan("custom_ssh", "custom_ssh", "Mosh")
    result, posts, rows = _run(step, "Mosh")
    assert posts == [], "nothing is posted"
    assert rows == ["Mosh"], "Mosh is not deleted"
    assert result.status == "skipped" and result.write_attempted is False
    assert "custom_ssh" in (result.warning or "")


def test_a_target_that_moved_is_removed_with_its_current_button(clock):
    step = _plan("Stale", "custom_ssh", "Stale")  # planned Remove_2
    result, posts, rows = _run(step, "Stale")  # custom_ssh removed meanwhile: Stale is Remove_1 now
    assert [k for p in posts for k in p if k.startswith("Remove_")] == ["Remove_1"]
    assert rows == []


def test_an_ambiguous_page_is_refused_and_nothing_is_posted(clock):
    step = _plan("Stale", "custom_ssh", "Stale")
    result, posts, rows = _run(step, "Stale", "Stale")
    assert posts == []
    assert rows == ["Stale", "Stale"]
    assert result.status == "failed" and result.write_attempted is False
