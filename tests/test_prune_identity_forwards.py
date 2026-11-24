"""Forward prune removals are bound to their (service, device) entry, like services (test_prune_identity).

apphosting.ha (NAT/Gaming) lists forwards as Service | Needed by Device with positional Remove_<n>
buttons. The removal is re-resolved on the page read for the write nonce: an entry already gone is
skipped (nothing sent), a moved entry gets its current button, an ambiguous page is refused, and the
device label is part of the identity (the same service forwarded to two devices is two entries).
"""

from urllib.parse import parse_qs

from save_helpers import client_with, html

from bgwcli.parser import parse_page
from bgwcli.restore import RestoreOptions, _remove_step, execute_restore

_ROW = '<tr><td>{service}</td><td>{device}</td><td><input type="submit" name="Remove_{n}" value="Remove"></td></tr>'


def apphosting_html(*rows: tuple[str, str]) -> str:
    body = "".join(_ROW.format(service=s, device=d, n=i) for i, (s, d) in enumerate(rows, start=1))
    return (
        '<html><body><form method="post" action="/cgi-bin/apphosting.ha">'
        '<input type="hidden" name="nonce" value="abc123"><table>'
        "<tr><th>Service</th><th>Needed by Device</th><th></th></tr>"
        f"{body}</table>"
        '<select name="service"><option value="custom_ssh">custom_ssh</option>'
        '<option value="Mosh">Mosh</option></select>'
        '<select name="device"><option value="aa:bb:cc:dd:ee:01">host-b</option>'
        '<option value="aa:bb:cc:dd:ee:02">host-a</option></select>'
        '<input type="submit" name="Add" value="Add"></form></body></html>'
    )


def _plan(target: tuple[str, str], *planned_rows: tuple[str, str]):
    live = {"apphosting": parse_page("apphosting", apphosting_html(*planned_rows), include_secrets=True)}
    step = _remove_step("apphosting", "remove-forward", target[0], live, RestoreOptions(prune=True), target[1])
    assert step.blocked is None, step.blocked
    return step.__class__(**{**step.__dict__, "order": 1})


def _run(step, *live_rows: tuple[str, str]):
    state = {"rows": list(live_rows)}

    def handler(request, n):
        if request.method == "POST":
            removed = next(k for k in parse_qs(request.body.decode()) if k.startswith("Remove_"))
            state["rows"].pop(int(removed.split("_")[1]) - 1)
            return html("", 302, {"location": "/cgi-bin/apphosting.ha"})
        return html(apphosting_html(*state["rows"]))

    client, wire = client_with(handler)
    execution = execute_restore(client, [step])
    posts = [parse_qs(r.body.decode()) for r in wire.requests if r.method == "POST"]
    return execution.steps[0], [k for p in posts for k in p if k.startswith("Remove_")], state["rows"]


SSH_B, MOSH_A, SSH_A = ("custom_ssh", "host-b"), ("Mosh", "host-a"), ("custom_ssh", "host-a")


def test_the_planned_button_is_posted_when_the_page_is_unchanged(clock):
    result, removed, rows = _run(_plan(MOSH_A, SSH_B, MOSH_A), SSH_B, MOSH_A)
    assert removed == ["Remove_2"] and rows == [SSH_B]


def test_a_forward_already_gone_is_skipped_and_the_shifted_row_is_kept(clock):
    result, removed, rows = _run(_plan(SSH_B, SSH_B, MOSH_A), MOSH_A)
    assert removed == [] and rows == [MOSH_A]
    assert result.status == "skipped" and result.write_attempted is False


def test_a_forward_that_moved_is_removed_with_its_current_button(clock):
    result, removed, rows = _run(_plan(MOSH_A, SSH_B, MOSH_A), MOSH_A)
    assert removed == ["Remove_1"] and rows == []


def test_an_ambiguous_page_is_refused_and_nothing_is_posted(clock):
    result, removed, rows = _run(_plan(MOSH_A, SSH_B, MOSH_A), MOSH_A, MOSH_A)
    assert removed == [] and rows == [MOSH_A, MOSH_A]
    assert result.status == "failed" and result.write_attempted is False


def test_the_device_label_is_part_of_the_identity(clock):
    # The same service forwarded to two devices: removing (custom_ssh, host-a) never takes host-b's row,
    # even after host-a's row moved to where host-b's used to be.
    result, removed, rows = _run(_plan(SSH_A, SSH_B, SSH_A), SSH_A, SSH_B)
    assert removed == ["Remove_1"] and rows == [SSH_B]


def test_a_forward_gone_while_the_same_service_remains_for_another_device_is_skipped(clock):
    result, removed, rows = _run(_plan(SSH_A, SSH_B, SSH_A), SSH_B)
    assert removed == [] and rows == [SSH_B]
    assert result.status == "skipped"
