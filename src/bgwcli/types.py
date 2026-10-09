"""Shared data model. Field names are snake_case renderings of the BGW320-CLI TypeScript types;
JSON output uses the same camelCase keys as the TS CLI via to_json_dict()."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Literal

HttpMethod = Literal["GET", "POST"]
LinkKind = Literal["router-page", "external", "other"]


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def to_json_dict(value: Any, *, null_fields: Iterable[str] = ()) -> Any:
    """Convert dataclasses to camelCase JSON, respecting private fields and explicit nulls."""
    keep = frozenset(null_fields)

    def convert(item: Any) -> Any:
        if is_dataclass(item) and not isinstance(item, type):
            out: dict[str, Any] = {}
            for f in fields(item):
                if not f.metadata.get("serialize", True):
                    continue
                v = getattr(item, f.name)
                if v is None and f.name not in keep:
                    continue
                out[_camel(f.name)] = convert(v)
            return out
        if isinstance(item, dict):
            return {k: convert(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [convert(v) for v in item]
        return item

    return convert(value)


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
    # Live HTML provenance only; old dumps cannot distinguish absent and explicit empty values.
    _value_omitted: bool = field(default=False, repr=False, compare=False, metadata={"serialize": False})


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
    # True when a parser bound cut the page: more than 150000 elements (the rest is unread), a table grid
    # past 250000 cells (its later rows are unread), a colspan/rowspan above 64 (read as 64), tables or
    # headings nested past 256 (the deeper markup is text), or an element's text past 5000 pieces /
    # 100000 characters. Nothing past a cut is ever shown unredacted. Absent otherwise.
    truncated: bool | None = None


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
