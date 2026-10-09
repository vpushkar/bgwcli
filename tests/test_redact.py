"""Secret detection by control name: real secret names stay redacted; names that merely contain
`wpa` or `key` as part of another word (WPA version, key rotation) are not secrets."""

import pytest

from bgwcli.redact import REDACTED, is_sensitive_name, redact_value

SECRET_NAMES = [
    "password", "new_password", "confirm_password", "old_password", "passwd", "ADM_PASSWORD", "passphrase",
    "hashpassword", "nonce", "secret", "access_code", "accesscode", "ssidpwd", "psk", "wpa_key", "wpakey",
    "wpa-psk", "WPAPSK", "wpa_passphrase", "wpapass", "wepkey", "wep_key1", "network_key", "networkkey",
    "shared_key", "pre-shared-key", "presharedkey", "key", "Key", "key11", "key21", "homeSSID_key",
    "WPSPIN5", "wps_pin", "phone_number", "PhoneNumber", "caller_id",
    # camel-case key names: a lower->upper case change is a word boundary
    "ssidKey", "wifiKey", "wlKey", "encryptionKey", "ssidKey11", "homeSsidKEY", "securitykey", "SecurityKey",
    "passkey", "PassKey", "WPAKey",
    # all-caps compounds: an uppercase run ending in KEY
    "SSIDKEY", "WIFIKEY", "APIKEY", "RADIUSKEY",
    # all-lowercase compounds: a known key-holder prefix glued to `key`
    "ssidkey", "wifikey", "wlankey", "encryptionkey", "sharedkey", "apikey", "radiuskey", "ssidkey11",
    "homessidkey", "wlan_ssidkey5",
    # password / pass / pw spellings, whole word or suffix
    "pwd", "PWD", "wlanpwd", "ap_pwd", "wlan_pw", "pw", "ap-pw", "apPw", "wlanPW", "ssid_pass", "pass", "ap_pass",
    "wlan-pass", "apPass", "pass_1",
    # SNMP community, auth keys, tokens, api keys
    "community", "snmp_community", "snmpcommunity", "ro_community", "authkey", "authKey", "auth_key", "AUTHKEY",
    "token", "csrftoken", "authToken", "session_token", "token5", "TOKEN", "apikey", "api_key", "psk", "ap_psk",
    # pw / pass / passcode / psk and key compounds glued to a lowercase prefix
    "newpw", "oldpass", "adminpass", "pppoepw", "userpass", "authpass", "privpass", "wlpw", "dynpass",
    "privatekey", "privkey", "ikekey", "sshkey", "snmpv3auth", "snmp_priv", "passcode", "wifi_passcode",
    "PPPOEPW", "NewPass", "ikepsk", "snmpv3priv", "private_key", "ssh_key",
]
PLAIN_NAMES = [
    "WPA version", "wpaversion", "wpaversion2", "wpaversion_5", "keyrotation",
    "keyinterval", "monkey", "turkey", "hotkeys", "maxclients", "security11", "defwpa", "valueKeys",
    "keyRotation", "ssidKeyRotation", "Monkey", "hotKeys",
    # a key-like token followed by another word (directly or after a separator) names a setting
    "wpakeyrotation", "wpaKeyRotation", "wpa_key_interval", "wpa_key_enabled", "wepkeyindex", "wep_key_index",
    "WEPKeyIndex", "key_rotation", "ssid_key_index", "key_id", "keyId", "SSIDKEYINDEX",
    "keyindex", "ssidkeyrotation", "wifikeyindex", "apikeyid",
    # `pass`, `pw` and `token` inside another word are not secrets
    "bypass", "passthrough", "pass_through", "passive", "passive_mode", "compass", "ipPassthrough", "pwr", "power",
    "pwm", "pwmode", "tokenizer", "token_id", "tokens_per_second",
    # glued compounds that only contain the letters
    "keyboard", "keyword", "hockey", "overpass", "surpass", "trespass", "underpass", "BYPASS", "snmp_auth_protocol",
    "snmpv3_priv_protocol", "privilege", "sshd_enabled", "ikeversion",
]


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_secret_names_are_sensitive_and_redacted(name):
    assert is_sensitive_name(name) is True
    assert redact_value(name, "s3cret", False) == REDACTED


@pytest.mark.parametrize("name", PLAIN_NAMES)
def test_names_that_only_contain_wpa_or_key_inside_another_word_are_not_sensitive(name):
    assert is_sensitive_name(name) is False
    assert redact_value(name, "WPA2", False) == "WPA2"
