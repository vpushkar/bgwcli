"""Secret redaction by field name, plus the names a live page marks secret by control type."""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import ParsedPage

# `wpa` and `key` are anchored: a WPA key/PSK/passphrase, a WEP/network/(pre-)shared/security key, a
# passkey, or `key` as a whole token is secret: key, key11, homeSSID_key, the camel-case ssidKey /
# wifiKey / encryptionKey, where a lower-to-upper case change (`dK`) is the token boundary, and
# all-caps compounds such as SSIDKEY or APIKEY (an uppercase run ending in KEY), and the lowercase
# compounds of a known key holder: ssidkey, wifikey, wlankey, encryptionkey, apikey, radiuskey.
# A key token followed by another word, directly or after `_`, `-` or `.`, names a setting about
# the key, not the key: `wpakeyrotation`, `wpa_key_interval`, `WEPKeyIndex`, `key_id`, `keyId`.
# `WPA version`, `wpaversion`, `keyrotation`, `keyRotation`, `monkey` and `valueKeys` are not secret.
_KEY_END = r"(?![a-z]|[_.\-][a-z])"
# Words that name what a key protects: glued to `key` in any case (ssidkey, wifikey, apikey) the
# compound is the key itself. `monkey`/`turkey` carry no such prefix and stay plain.
_KEY_HOLDERS = "wep|network|shared|pre.?shared|security|ssid|wifi|wlan|encryption|api|radius|priv(?:ate)?|ike|ssh"
# `pw` and `pass` are short enough to sit inside ordinary words (power, bypass, passive), so they count
# only as a whole token: at the start of the name, after `_`/`-`/`.`/a digit, or at a lower-to-upper case
# change (ap_pw, wlan-pass, apPass), and not when another word follows (`pass_through`, `passive`).
# `token` counts glued to a prefix as well (csrftoken, authToken) but not inside a longer word
# (tokenizer, tokens_per_second) or as a token about a token (`token_id`).
# `pw` and `pass` also count glued to a lowercase prefix (newpw, oldpass, adminpass, pppoepw), except where
# the letters end an ordinary word: bypass, compass, overpass, surpass, trespass, underpass.
_GLUED_WORDS_END = r"(?<!by)(?<!com)(?<!over)(?<!sur)(?<!tres)(?<!under)"
_SENSITIVE = re.compile(
    r"(password|passwd|passphrase|secret|access.?code"
    rf"|wpa.?(?:key{_KEY_END}|psk|pass)|(?:{_KEY_HOLDERS}).?key{_KEY_END}|passkey"
    rf"|(?:(?<![a-z])k|(?<=(?-i:[a-z]))(?-i:K))ey{_KEY_END}|(?<=(?-i:[A-Z]))(?-i:KEY){_KEY_END}"
    rf"|{_GLUED_WORDS_END}(?:pw|pass){_KEY_END}|pass.?code|token{_KEY_END}"
    rf"|snmp(?:v\d)?[_.\-]?(?:auth|priv){_KEY_END}"
    r"|pwd|community|authkey"
    r"|psk|ssidpwd|hashpassword|nonce|wps.?pin|phone.?number|caller)",
    re.IGNORECASE,
)
REDACTED = "[redacted]"


def is_sensitive_name(name: str) -> bool:
    return bool(_SENSITIVE.search(name))


def redact_value(name: str, value: str, include_secrets: bool, sensitive_names: Collection[str] = ()) -> str:
    """`sensitive_names` adds names that are secret for a reason the name does not show (a
    type="password" control); see page_sensitive_names and Snapshot.form_secrets."""
    if include_secrets or not value:
        return value
    return REDACTED if name in sensitive_names or is_sensitive_name(name) else value


def page_sensitive_names(parsed: ParsedPage | None) -> frozenset[str]:
    """Every control name the parser flagged sensitive on this page: by name, or by being a
    type="password" input. Callers that parse with include_secrets=True hold the real values, so
    this set is what keeps a password control redacted in their output."""
    if parsed is None:
        return frozenset()
    names = {f.name for f in parsed.fields if f.sensitive}
    names.update(s.name for s in parsed.selects if s.sensitive)
    names.update(t.name for t in parsed.textareas if t.sensitive)
    return frozenset(names)
