"""`action clear-device-list` posts the button the live devices.ha form renders (fw 6.35.8, captured
2026-11-23): `<input type="submit" name="Clear" value="Clear and Rescan for Devices">`. The action used
to send "Clear Device List", a value the page does not render."""

import json
from urllib.parse import parse_qs

from save_helpers import client_with, html

from bgwcli import cli

DEVICES_FORM = (
    '<form method="post" action="/cgi-bin/devices.ha"><input type="hidden" name="nonce" value="abc123" />'
    '<input type="submit" name="Clear" class="cssbtn largebtn" value="Clear and Rescan for Devices"  />'
    "</form>"
)


def test_clear_device_list_posts_the_button_value_the_live_devices_form_renders(
    tmp_env, clock, monkeypatch, capsys
):
    def handler(request, n):
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/devices.ha"})
        return html(DEVICES_FORM)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["action", "clear-device-list", "--commit", "--confirm", "CLEAR-DEVICES", "--json"])
    json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and "/login.ha" not in r.url]

    assert code == 0 and len(posts) == 1
    sent = parse_qs(posts[0].body.decode())
    assert sent["Clear"] == ["Clear and Rescan for Devices"]
    assert sent["nonce"] == ["abc123"]
