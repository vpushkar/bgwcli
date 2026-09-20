"""Secret redaction by field name."""

import re

_SENSITIVE = re.compile(
    r"(password|passwd|passphrase|key|secret|access.?code|wpa|psk|ssidpwd|hashpassword|nonce|wps.?pin|phone.?number|caller)",
    re.IGNORECASE,
)
REDACTED = "[redacted]"


def is_sensitive_name(name: str) -> bool:
    return bool(_SENSITIVE.search(name))


def redact_value(name: str, value: str, include_secrets: bool) -> str:
    if include_secrets or not value:
        return value
    return REDACTED if is_sensitive_name(name) else value
