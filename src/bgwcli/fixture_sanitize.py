"""Sanitize captured router HTML/JSON before it is written as a test fixture.

Redacts MAC addresses, IPv4/IPv6 addresses, secret-bearing form values (nonce, hashes,
passwords, keys, SSIDs, WPS PINs, serials, hostnames), identity table cells and device
labels. `contains_sensitive_fixture_value` is the fail-closed residue check.
"""

from __future__ import annotations

import re

_SENSITIVE_INPUT_NAMES = (
    r"(?:nonce|hashpassword|password|.*?pass.*?|.*?key.*?|.*?ssid.*?|wps.?pin.*?"
    r"|serial(?:number)?|hostname|device.?name)"
)

_MAC = re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.IGNORECASE)
_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])\.){3}(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])\b")
_IPV6_FULL = re.compile(r"\b(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}\b", re.IGNORECASE)
_IPV6_COMPRESSED = re.compile(r"\b(?=[0-9a-f:]*::)(?:[0-9a-f]{0,4}:){1,7}[0-9a-f]{0,4}\b", re.IGNORECASE)
_NAME_THEN_VALUE = re.compile(
    r"(name=[\"']" + _SENSITIVE_INPUT_NAMES + r"[\"'][^>]*value=[\"'])[^\"']*([\"'])",
    re.IGNORECASE,
)
_VALUE_THEN_NAME = re.compile(
    r"(value=[\"'])[^\"']*([\"'][^>]*name=[\"']" + _SENSITIVE_INPUT_NAMES + r"[\"'])",
    re.IGNORECASE,
)
_LABEL_DEFAULT = re.compile(
    r"((?:Network Name \(SSID\)|Guest Network Name|Password)\s+Default:\s*)[^<\r\n]+",
    re.IGNORECASE,
)
_IDENTITY_CELL = re.compile(
    r"(<t[dh]\b[^>]*>\s*(?:Serial Number|Vendor SN|Phone Number|Far-End Caller Information)\s*</t[dh]>\s*"
    r"<t[dh]\b[^>]*>).*?(</t[dh]>)",
    re.IGNORECASE | re.DOTALL,
)
_IP_SLASH_NAME = re.compile(r"(\[redacted-ip\]\s*/\s*)[^<\r\n]+", re.IGNORECASE)
_NAME_SLASH_MAC = re.compile(r"[^<>\r\n/]+(/\[redacted-mac\])")

_RESIDUE_SENSITIVE_JSON = re.compile(r'"value":"(?!\[redacted\])[^"]+"[^{}]{0,160}"sensitive":true', re.IGNORECASE)
_RESIDUE_LABEL_DEFAULT = re.compile(
    r"(?:Network Name \(SSID\)|Guest Network Name|Password)\s+Default:(?!\s*\[redacted\])\s*\S",
    re.IGNORECASE,
)
_RESIDUE_HASH = re.compile(r"\b[a-f0-9]{32}\b", re.IGNORECASE)


def sanitize_router_fixture(value: str) -> str:
    value = _MAC.sub("[redacted-mac]", value)
    value = _IPV4.sub("[redacted-ip]", value)
    value = _IPV6_FULL.sub("[redacted-ipv6]", value)
    value = _IPV6_COMPRESSED.sub("[redacted-ipv6]", value)
    value = _NAME_THEN_VALUE.sub(r"\1[redacted]\2", value)
    value = _VALUE_THEN_NAME.sub(r"\1[redacted]\2", value)
    value = _LABEL_DEFAULT.sub(r"\1[redacted]", value)
    value = _IDENTITY_CELL.sub(r"\1[redacted]\2", value)
    value = _IP_SLASH_NAME.sub(r"\1[redacted-name]", value)
    value = _NAME_SLASH_MAC.sub(r"[redacted-name]\1", value)
    return value


def contains_sensitive_fixture_value(value: str) -> bool:
    return bool(
        _RESIDUE_SENSITIVE_JSON.search(value)
        or _RESIDUE_LABEL_DEFAULT.search(value)
        or _RESIDUE_HASH.search(value)
        or _MAC.search(value)
    )
