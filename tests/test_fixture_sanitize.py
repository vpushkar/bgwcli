from bgwcli.fixture_sanitize import contains_sensitive_fixture_value, sanitize_router_fixture


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
