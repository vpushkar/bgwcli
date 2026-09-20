"""Shared data model. Field names are snake_case renderings of the BGW320-CLI TypeScript types;
JSON output uses the same camelCase keys as the TS CLI via to_json_dict()."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Literal

HttpMethod = Literal["GET", "POST"]
LinkKind = Literal["router-page", "external", "other"]


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def to_json_dict(value: Any) -> Any:
    """Recursively convert dataclasses to dicts with camelCase keys, dropping None values."""
    if is_dataclass(value) and not isinstance(value, type):
        out: dict[str, Any] = {}
        for f in fields(value):
            v = getattr(value, f.name)
            if v is None:
                continue
            out[_camel(f.name)] = to_json_dict(v)
        return out
    if isinstance(value, dict):
        return {k: to_json_dict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json_dict(v) for v in value]
    return value


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    status_message: str
    headers: dict[str, str | list[str]]
    body: str
    url: str


@dataclass(frozen=True)
class RouterSessionSnapshot:
    origin: str
    authenticated: bool
    cookies: dict[str, str]


@dataclass(frozen=True)
class SitemapEntry:
    page: str
    label: str
    href: str


@dataclass(frozen=True)
class ParsedField:
    name: str
    type: str
    value: str
    checked: bool
    sensitive: bool
    disabled: bool | None = None
    read_only: bool | None = None


@dataclass(frozen=True)
class SelectOption:
    value: str
    label: str
    selected: bool
    disabled: bool


@dataclass(frozen=True)
class ParsedSelect:
    name: str
    value: str
    options: list[str]
    sensitive: bool
    option_details: list[SelectOption] | None = None
    disabled: bool | None = None


@dataclass(frozen=True)
class ParsedTextarea:
    name: str
    value: str
    sensitive: bool
    disabled: bool | None = None
    read_only: bool | None = None


@dataclass(frozen=True)
class ParsedButton:
    name: str
    type: str
    value: str
    label: str
    sensitive: bool
    disabled: bool | None = None


@dataclass(frozen=True)
class ParsedForm:
    method: str
    action: str
    field_names: list[str]
    select_names: list[str]
    textarea_names: list[str]
    button_names: list[str]


@dataclass(frozen=True)
class ParsedValueEntry:
    section: str
    label: str
    value: str
    default_value: str | None = None


@dataclass(frozen=True)
class ParsedLink:
    label: str
    href: str
    kind: LinkKind
    page: str | None = None


@dataclass
class ParsedPage:
    page: str
    title: str
    heading: str
    values: dict[str, str] = field(default_factory=dict)
    tables: list[dict[str, str]] = field(default_factory=list)
    fields: list[ParsedField] = field(default_factory=list)
    selects: list[ParsedSelect] = field(default_factory=list)
    textareas: list[ParsedTextarea] = field(default_factory=list)
    buttons: list[ParsedButton] = field(default_factory=list)
    forms: list[ParsedForm] = field(default_factory=list)
    value_entries: list[ParsedValueEntry] | None = None
    links: list[ParsedLink] | None = None


@dataclass(frozen=True)
class Device:
    status: str
    name: str
    ip: str
    mac: str
    connection: str
    connection_speed: str | None = None
    allocation: str | None = None
    last_activity: str | None = None
    mesh_client: str | None = None
    ipv6: str | None = None
    device_type: str | None = None
    valid_lifetime: str | None = None
    preferred_lifetime: str | None = None


@dataclass(frozen=True)
class LogEntry:
    id: str
    time: str
    source: str
    destination: str
    protocol: str
    reason: str
