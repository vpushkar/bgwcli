import pytest

from bgwcli.fixture_sanitize import contains_sensitive_fixture_value, sanitize_router_fixture, sensitive_control_residue


def test_sanitizer_removes_values_embedded_in_labels_and_identity_rows():
    sanitized = sanitize_router_fixture(
        """
    <th>Network Name (SSID) Default: PrivateNetwork</th>
    <th>Password Default: private-password</th>
    <tr><th>Serial Number</th><td>device-serial</td></tr>
    <input name="WPSPIN5" value="12345670">
  """
    )

    assert "PrivateNetwork" not in sanitized
    assert "private-password" not in sanitized
    assert "device-serial" not in sanitized
    assert "12345670" not in sanitized
    assert contains_sensitive_fixture_value(sanitized) is False


def test_sanitizer_redacts_mac_ipv4_and_ipv6_addresses():
    sanitized = sanitize_router_fixture(
        "<td>aa:bb:cc:dd:ee:ff</td><td>192.168.1.254</td>"
        "<td>fe80::1a2b:3c4d:5e6f:7a8b</td><td>2600:1700:abcd:1234:0:0:0:1</td><td>::1</td><td>fe80::</td>"
    )
    # Exact TS output (the bare "fe80::" prefix and "::1" survive in TS too; parity over perfection).
    assert sanitized == (
        "<td>[redacted-mac]</td><td>[redacted-ip]</td>"
        "<td>fe80::[redacted-ipv6]</td><td>[redacted-ipv6]</td><td>::1</td><td>fe80::</td>"
    )
    # version-like numbers are not IPv4 addresses
    assert sanitize_router_fixture("<td>3.18.2</td>") == "<td>3.18.2</td>"


def test_sanitizer_redacts_named_inputs_in_either_attribute_order():
    sanitized = sanitize_router_fixture(
        '<input name="nonce" value="abc123">'
        '<input value="s3cret" type="password" name="hashpassword">'
        "<input name='SSID_5G' value='MyWifi'>"
        '<input name="target" value="example.com">'
    )
    assert "abc123" not in sanitized
    assert "s3cret" not in sanitized
    assert "MyWifi" not in sanitized
    assert 'name="target" value="example.com"' in sanitized
    assert sanitized.count("[redacted]") == 3


def test_sanitizer_redacts_identity_table_cells_and_device_labels():
    sanitized = sanitize_router_fixture(
        "<tr><th>Serial Number</th><td>ABC123XYZ</td></tr>"
        "<tr><td>Phone Number</td><td>\n555-0100\n</td></tr>"
        "<tr><td>Vendor SN</td><td>VS-1</td></tr>"
        "<td>192.168.1.10 / my-laptop</td>"
        "<td>my-phone/aa:bb:cc:dd:ee:01</td>"
    )
    assert "ABC123XYZ" not in sanitized
    assert "555-0100" not in sanitized
    assert "VS-1" not in sanitized
    assert "my-laptop" not in sanitized
    assert "my-phone" not in sanitized
    assert "[redacted-ip] / [redacted-name]" in sanitized
    assert "[redacted-name]/[redacted-mac]" in sanitized


def test_contains_sensitive_fixture_value_detects_residue():
    assert contains_sensitive_fixture_value('{"value":"secret","sensitive":true}') is True
    assert contains_sensitive_fixture_value('{"value":"[redacted]","sensitive":true}') is False
    assert contains_sensitive_fixture_value("Password Default: hunter2") is True
    assert contains_sensitive_fixture_value("Password Default: [redacted]") is False
    assert contains_sensitive_fixture_value("0123456789abcdef0123456789abcdef") is True
    assert contains_sensitive_fixture_value("00:11:22:33:44:55") is True
    assert contains_sensitive_fixture_value("<td>[redacted-mac]</td>") is False


def test_sanitizer_redacts_controls_found_by_parsing_not_just_quoted_name_value_pairs():
    from bgwcli.parser import parse_page

    raw = (
        "<form><input name=wpa_key value=unquoted-secret>"
        '<input id="password" type="password" value="id-only-secret">'
        "<input name=\"ssidname11\" value=\"Bob's Net\">"
        "<input type=PASSWORD name=entry value='apostrophe-free'>"
        '<textarea name="passphrase">\nline one\nline two</textarea>'
        '<input name="target" value="example.com"></form>'
    )
    sanitized = sanitize_router_fixture(raw)
    for secret in ("unquoted-secret", "id-only-secret", "Bob", "Net", "apostrophe-free", "line one"):
        assert secret not in sanitized, secret
    assert 'name="target" value="example.com"' in sanitized
    revealed = parse_page("x", sanitized, include_secrets=True)
    assert {f.name: f.value for f in revealed.fields} == {
        "wpa_key": "[redacted]", "password": "[redacted]", "ssidname11": "[redacted]", "entry": "[redacted]",
        "target": "example.com",
    }
    assert revealed.textareas[0].value == "[redacted]"
    assert sensitive_control_residue(revealed) == []


def test_sensitive_control_residue_reads_the_unredacted_parse():
    from bgwcli.parser import parse_page

    leaked = parse_page(
        "x",
        '<input type="password" name="entry" value="hunter2"><input name="ssidname11" value="Net">'
        '<textarea name="passphrase">pp</textarea><input type="checkbox" name="wpa_key_enabled" checked>'
        '<input name="nonce" value=""><input name="target" value="example.com">',
        include_secrets=True,
    )
    assert sensitive_control_residue(leaked) == ["entry", "ssidname11", "passphrase"]


def test_sanitizer_redacts_sensitive_select_values_and_option_labels():
    from bgwcli.parser import parse_page

    raw = (
        '<form><select name="wpakey"><option value="s3cr3t" selected>s3cr3t</option>'
        "<option>labelonly</option></select>"
        '<select name="security11"><option value="wpa" selected>WPA</option></select></form>'
    )
    sanitized = sanitize_router_fixture(raw)
    assert "s3cr3t" not in sanitized and "labelonly" not in sanitized
    assert '<option value="wpa" selected>WPA</option>' in sanitized, "non-secret selects are left alone"
    assert sensitive_control_residue(parse_page("wconfig", sanitized, include_secrets=True)) == []


def test_residue_check_flags_an_unsanitized_sensitive_select():
    from bgwcli.parser import parse_page

    raw = '<form><select name="wpakey"><option value="s3cr3t" selected>s3cr3t</option></select></form>'
    assert sensitive_control_residue(parse_page("wconfig", raw, include_secrets=True)) == ["wpakey"]
    other = '<form><select name="wpakey"><option value="[redacted]" selected>leak</option></select></form>'
    assert sensitive_control_residue(parse_page("wconfig", other, include_secrets=True)) == ["wpakey"]


@pytest.mark.parametrize(
    "raw",
    [
        "<input type=password name=pin value=it'sS3KRIT>",
        "<input type=password name=pin value=a=bS3KRIT>",
        '<input type=password name=pin value=a"bS3KRIT>',
        "<input name=wpakey value=p`wS3KRIT>",
        "<input name=ssid11 value=my'netS3KRIT>",
    ],
)
def test_sanitizer_redacts_whole_unquoted_values_containing_quotes_equals_or_backticks(raw):
    from bgwcli.audit import _assert_fixture_safe, _parse_page

    sanitized = sanitize_router_fixture(raw)
    assert "S3KRIT" not in sanitized
    assert 'value="[redacted]">' in sanitized
    _assert_fixture_safe("x", sanitized, _parse_page("x", sanitized))


def test_nameless_password_input_is_sanitized_and_its_residue_detected():
    from bgwcli.audit import _assert_fixture_safe, _parse_page

    raw = '<form><input type="password" value="S3KRIT"><input type=PASSWORD value=S3KRIT2></form>'
    assert contains_sensitive_fixture_value(raw) is True, "residue check sees a nameless password value"
    sanitized = sanitize_router_fixture(raw)
    assert "S3KRIT" not in sanitized
    assert sanitized.count('value="[redacted]"') == 2
    assert contains_sensitive_fixture_value(sanitized) is False
    _assert_fixture_safe("x", sanitized, _parse_page("x", sanitized))
    assert contains_sensitive_fixture_value('<input type="password" value="">') is False


@pytest.mark.parametrize(
    "raw",
    [
        "<table><tr><td>Wi-Fi Password</td><td>S3KRIT</td></tr></table>",
        "<table><tr><td>Device Access Code:</td><td>\n<b>S3KRIT</b>\n</td></tr></table>",
        "<table><tr><th>Network Key</th><td>S3KRIT</td></tr></table>",
        "<table><tr><th>Name</th><th>Passphrase</th><th>x</th></tr><tr><td>a</td><td>S3KRIT</td><td>b</td></tr></table>",
        "<TABLE><TR><TD>Wi-Fi Password<TD>S3KRIT</TABLE>",
    ],
)
def test_sanitizer_redacts_labelled_table_values_the_parser_treats_as_secret(raw):
    from bgwcli.audit import _assert_fixture_safe, _parse_page

    sanitized = sanitize_router_fixture(raw)
    assert "S3KRIT" not in sanitized
    _assert_fixture_safe("x", sanitized, _parse_page("x", sanitized))


def test_sanitizer_leaves_non_secret_table_values_and_control_cells_alone():
    raw = (
        "<table><tr><td>Firmware Version</td><td>6.30.5</td></tr>"
        '<tr><td>Hostname Note</td><td><input type="text" name="note" value="x"></td></tr></table>'
        "<table><tr><th>Name</th><th>Status</th><th>x</th></tr><tr><td>a</td><td>Up</td><td>b</td></tr></table>"
    )
    assert sanitize_router_fixture(raw) == raw


def test_residue_check_flags_labelled_table_values_from_an_include_secrets_parse():
    from bgwcli.parser import parse_page

    two_col = "<table><tr><td>Wi-Fi Password</td><td>S3KRIT</td></tr></table>"
    wide = (
        "<table><tr><th>Name</th><th>Passphrase</th><th>x</th></tr><tr><td>a</td><td>S3KRIT</td><td>b</td></tr></table>"
    )
    assert sensitive_control_residue(parse_page("x", two_col, include_secrets=True)) == ["Wi-Fi Password"]
    assert sensitive_control_residue(parse_page("x", wide, include_secrets=True)) == ["Passphrase"]
    clean = "<table><tr><td>Wi-Fi Password</td><td>[redacted]</td></tr></table>"
    assert sensitive_control_residue(parse_page("x", clean, include_secrets=True)) == []


@pytest.mark.parametrize(
    "raw",
    [
        "<table><tr><th>User</th><th colspan=2>Password</th></tr>"
        "<tr><td>u</td><td>hunter2</td><td>hunter3</td></tr></table>",
        "<table><tr><th>User</th><th>Password</th><th>Note</th></tr>"
        "<tr><td rowspan=2>u</td><td>hunter2</td><td>n</td></tr><tr><td>hunter3</td><td>n2</td></tr></table>",
    ],
)
def test_sanitizer_reads_spanned_tables_the_way_the_parser_does(raw):
    from bgwcli.parser import parse_page

    sanitized = sanitize_router_fixture(raw)
    assert "hunter2" not in sanitized and "hunter3" not in sanitized
    assert sensitive_control_residue(parse_page("x", sanitized, include_secrets=True)) == []


def test_sanitizer_parses_the_markup_once_and_the_html_residue_check_is_html_only(monkeypatch):
    from html.parser import HTMLParser

    feeds = []
    original = HTMLParser.feed

    def counting_feed(self, data):
        feeds.append(type(self).__name__)
        return original(self, data)

    monkeypatch.setattr(HTMLParser, "feed", counting_feed)
    sanitize_router_fixture('<table><tr><td>Password</td><td>x</td></tr></table><input name="wpakey" value="k">')
    assert len(feeds) == 1
    feeds.clear()
    serialized = '{"title":"<input type=password value=abc>"}'
    assert contains_sensitive_fixture_value(serialized, html=False) is False
    assert feeds == []
    assert contains_sensitive_fixture_value(serialized) is True


@pytest.mark.parametrize(
    "raw",
    [
        "<title>Voice Status</title><table><tr><th>Metric</th><th>Line 1</th><th>Line 2</th></tr>"
        "<tr><td>Phone Number</td><td>555-0101</td><td>555-0102</td></tr></table>",
        "<table><tr><td>Caller</td><td>Alice 555-0101</td><td>Bob 555-0102</td><td>x</td></tr></table>",
    ],
)
def test_every_value_cell_of_a_row_with_a_secret_label_is_sanitized(raw):
    from bgwcli.parser import parse_page

    sanitized = sanitize_router_fixture(raw)
    assert "555-0101" not in sanitized and "555-0102" not in sanitized
    for page in ("voiceconfig", "x"):
        assert sensitive_control_residue(parse_page(page, sanitized, include_secrets=True)) == []


@pytest.mark.parametrize(
    "label",
    [
        "Access Code Default: ProbeSensitiveValue",
        "Password Default: <span>ProbeSensitiveValue</span>",
        "Wi-Fi Key Default:\n<b>ProbeSensitiveValue</b>",
    ],
)
def test_a_secret_label_default_is_sanitized_whatever_its_label_or_markup(label):
    from bgwcli.parser import parse_page

    raw = f"<table><tr><td>{label}</td><td>Current</td></tr></table>"
    sanitized = sanitize_router_fixture(raw)
    assert "ProbeSensitiveValue" not in sanitized
    assert sensitive_control_residue(parse_page("x", sanitized, include_secrets=True)) == []


def test_residue_check_reads_defaults_and_secret_row_metrics():
    from bgwcli.parser import parse_page

    default = "<table><tr><td>Access Code Default: abc</td><td>[redacted]</td></tr></table>"
    assert sensitive_control_residue(parse_page("x", default, include_secrets=True)) == ["Access Code Default"]
    voice = (
        "<table><tr><th>Metric</th><th>Line 1</th><th>Line 2</th></tr>"
        "<tr><td>Phone Number</td><td>[redacted]</td><td>555-0102</td></tr></table>"
    )
    assert sensitive_control_residue(parse_page("voiceconfig", voice, include_secrets=True)) == ["Phone Number"]
    assert sensitive_control_residue(parse_page("x", voice, include_secrets=True)) == ["Phone Number"]


def test_a_secret_row_label_itself_is_not_residue():
    from bgwcli.parser import parse_page

    wide = (
        "<table><tr><th>Name</th><th>A</th><th>B</th></tr>"
        "<tr><td>Password</td><td>[redacted]</td><td></td></tr></table>"
    )
    assert sensitive_control_residue(parse_page("x", wide, include_secrets=True)) == []


SECRET = "alpha987Beta654"
SPAN_LEAKS = {
    "rowspan secret label": (
        f'<table><tr><td rowspan="2">Password</td><td>first-value</td></tr><tr><td>{SECRET}</td></tr></table>'
    ),
    "rowspan secret label in a nested table": (
        "<table><tr><td>Details</td><td><table>"
        f'<tr><td rowspan="2">Password</td><td>one</td></tr><tr><td>{SECRET}</td></tr></table></td></tr></table>'
    ),
    "rowspan label over wide rows": (
        "<table><tr><th>Item</th><th>A</th><th>B</th></tr>"
        f'<tr><td rowspan="2">Password</td><td>1</td><td>2</td></tr><tr><td>{SECRET}</td><td>y</td></tr></table>'
    ),
    "colspan and rowspan value under a secret header": (
        "<table><tr><th>Name</th><th>Password</th><th>Status</th></tr>"
        f'<tr><td colspan="2" rowspan="2">{SECRET}</td><td>first</td></tr><tr><td>second</td></tr></table>'
    ),
    "secret held by the label cell": f"<table><tr><td>Password: {SECRET}</td><td>x</td></tr></table>",
    "secret in a one-cell row": f"<table><tr><td>Access Code: {SECRET}</td></tr></table>",
}


@pytest.mark.parametrize("raw", SPAN_LEAKS.values(), ids=SPAN_LEAKS.keys())
def test_sanitizer_governs_every_cell_the_parser_redacts(raw):
    sanitized = sanitize_router_fixture(raw)
    assert SECRET not in sanitized
    assert contains_sensitive_fixture_value(sanitized) is False
    assert contains_sensitive_fixture_value(raw) is True


def test_residue_check_sees_a_secret_the_page_parse_does_not_surface():
    raw = SPAN_LEAKS["rowspan secret label"]
    assert contains_sensitive_fixture_value(raw) is True
    assert contains_sensitive_fixture_value("<table><tr><td>Model</td><td>BGW320</td></tr></table>") is False


@pytest.mark.parametrize("name", [
    "pwd", "wlanpwd", "ap_pwd", "wlan_pw", "ap_pass", "ssid_pass", "snmp_community", "authkey", "csrftoken", "token",
])
def test_sanitizer_redacts_controls_whose_names_the_parser_treats_as_secret(name):
    for raw in (
        f'<form><input type="text" name="{name}" value="hunter2"></form>',
        f'<form><input type="text" value="hunter2" name="{name}"></form>',
    ):
        assert "hunter2" not in sanitize_router_fixture(raw)


LABELLED_CONTROLS = (
    "<form><table>"
    '<tr><td>Wi-Fi Password</td><td><input name="x1" value="S3KRIT"></td></tr>'
    '<tr><td>Network Key</td><td><select name="x2"><option value="S3KRIT-a" selected>S3KRIT-label</option>'
    '<option value="S3KRIT-b">b</option></select></td></tr>'
    '<tr><td>Access Code</td><td><textarea name="x3">S3KRIT</textarea></td></tr>'
    '<tr><td>Note</td><td><input name="x4" value="plain-note"></td></tr>'
    "</table></form>"
)


def test_sanitizer_redacts_controls_in_cells_under_a_secret_label():
    from bgwcli.audit import _assert_fixture_safe, _parse_page
    from bgwcli.parser import parse_page

    sanitized = sanitize_router_fixture(LABELLED_CONTROLS)
    assert "S3KRIT" not in sanitized
    assert 'name="x4" value="plain-note"' in sanitized
    assert sensitive_control_residue(parse_page("x", sanitized, include_secrets=True)) == []
    _assert_fixture_safe("x", sanitized, _parse_page("x", sanitized))


def test_residue_check_flags_a_control_under_a_secret_label_that_still_holds_a_value():
    from bgwcli.parser import parse_page

    residue = sensitive_control_residue(parse_page("x", LABELLED_CONTROLS, include_secrets=True))
    assert sorted(residue) == ["x1", "x2", "x3"]
    assert contains_sensitive_fixture_value(LABELLED_CONTROLS) is True


HOSTILE_100KB = {
    "name with a long run of pass": 'name="' + "pass" * 25000,
    "name with a long run of key": 'name="' + "key" * 33000,
    "colons": ":" * 100000,
    "hex groups": "a:" * 50000,
    "label default then spaces": "Password Default:" + " " * 100000,
    "label then spaces without default": ("Password" + " " * 5000) * 20,
    "name slash mac prefix": "x" * 100000 + "/[redacted-mac]",
    "name slash many": "x/[redacted-mac]" * 6000,
    "repeated value attributes": 'value="' * 14000,
    "ip slash name": "[redacted-ip]" + " " * 100000,
    "identity cell never closed": "<td>Serial Number</td><td" * 4000,
}


@pytest.mark.parametrize("raw", HOSTILE_100KB.values(), ids=list(HOSTILE_100KB))
def test_hostile_100kb_inputs_sanitize_in_bounded_time(raw):
    import time

    started = time.perf_counter()
    sanitize_router_fixture(raw)
    contains_sensitive_fixture_value(raw)
    # Quadratic or worse took tens of seconds; a floor generous enough for a slow board.
    assert time.perf_counter() - started < 10


def test_bounding_the_expressions_keeps_every_redaction_a_real_page_needs():
    raw = (
        '<input type="text" name="wpa_passphrase_24" value="S3KRIT">'
        '<input value="S3KRIT2" name="guest_psk_key">'
        "<td>Password Default: S3KRIT3</td>"
        "<td>fe80::1a2b:3c4d:5e6f:7a8b</td>"
        "<td>office-printer/aa:bb:cc:dd:ee:ff</td>"
    )
    sanitized = sanitize_router_fixture(raw)
    for secret in ("S3KRIT", "S3KRIT2", "S3KRIT3", "1a2b", "office-printer", "aa:bb"):
        assert secret not in sanitized
    assert "[redacted-name]/[redacted-mac]" in sanitized


SECRET_NAMES = [
    "pwd", "wlanpwd", "ap_pwd", "pw", "ap_pw", "pass", "wlan_pass", "apPass", "passphrase", "psk", "psk5",
    "community", "snmp_community", "authkey", "token", "csrftoken", "authToken", "secret", "client_secret", "apikey",
]


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_sanitizer_redacts_every_name_the_parser_treats_as_secret(name):
    from bgwcli.audit import _assert_fixture_safe, _parse_page
    from bgwcli.fixture_sanitize import _NAME_THEN_VALUE, _VALUE_THEN_NAME
    from bgwcli.parser import parse_page

    raw = f'<form><input type="text" name="{name}" value="S3KRIT"><input value="S3KRIT" name="{name}"></form>'
    assert _NAME_THEN_VALUE.search(raw), "the attribute-order patterns know the name too"
    assert _VALUE_THEN_NAME.search(raw)
    sanitized = sanitize_router_fixture(raw)
    assert "S3KRIT" not in sanitized
    assert sensitive_control_residue(parse_page("x", sanitized, include_secrets=True)) == []
    _assert_fixture_safe("x", sanitized, _parse_page("x", sanitized))
    assert sensitive_control_residue(parse_page("x", raw, include_secrets=True)) == [name]
