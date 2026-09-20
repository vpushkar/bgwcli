"""Construct ParsedPage objects directly, shaped exactly as parser.parse_page would shape them.

The TypeScript tests fed inline HTML through parsePage; these helpers produce the same
ParsedPage data from the field/select/button/table facts, so the snapshot/restore tests do not
depend on parser.py. Shape rules mirrored from the TS parser:
  - table rows keep only columns with a non-empty header (first empty header becomes "Metric");
    tables with <= 2 columns are not rows at all;
  - a two-cell table row becomes a `values` entry (label -> value);
  - <input> without a value attribute has value "";
  - a button's label is its value, falling back to its name;
  - a select's value is the selected option, else the first option; option value falls back to label.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from bgwcli.redact import is_sensitive_name
from bgwcli.types import (
    ParsedButton,
    ParsedField,
    ParsedForm,
    ParsedPage,
    ParsedSelect,
    ParsedTextarea,
    SelectOption,
)


def field(
    name: str,
    type: str = "text",
    value: str = "",
    *,
    checked: bool = False,
    disabled: bool | None = None,
) -> ParsedField:
    return ParsedField(
        name=name, type=type, value=value, checked=checked, sensitive=is_sensitive_name(name), disabled=disabled
    )


def hidden(name: str, value: str) -> ParsedField:
    return field(name, "hidden", value)


def checkbox(name: str, value: str = "", *, checked: bool = False, disabled: bool | None = None) -> ParsedField:
    return field(name, "checkbox", value, checked=checked, disabled=disabled)


def radio(name: str, value: str, *, checked: bool = False) -> ParsedField:
    return field(name, "radio", value, checked=checked)


def select(
    name: str,
    options: Sequence[str | tuple[str, str] | tuple[str, str, bool]],
    *,
    selected: str | None = None,
    disabled: bool | None = None,
    disabled_options: Iterable[str] = (),
) -> ParsedSelect:
    """options: value, (value, label) or (value, label, selected). `selected` names a value explicitly."""
    disabled_values = set(disabled_options)
    details: list[SelectOption] = []
    for option in options:
        if isinstance(option, str):
            value, label, flagged = option, option, False
        elif len(option) == 2:
            value, label = option
            flagged = False
        else:
            value, label, flagged = option
        details.append(
            SelectOption(
                value=value or label,
                label=label,
                selected=flagged or (selected is not None and value == selected),
                disabled=value in disabled_values,
            )
        )
    chosen = next((o for o in details if o.selected), details[0] if details else None)
    return ParsedSelect(
        name=name,
        value=chosen.value if chosen else "",
        options=[o.value for o in details],
        sensitive=is_sensitive_name(name),
        option_details=details,
        disabled=disabled,
    )


def textarea(name: str, value: str = "", *, disabled: bool | None = None) -> ParsedTextarea:
    return ParsedTextarea(name=name, value=value, sensitive=is_sensitive_name(name), disabled=disabled)


def button(name: str, value: str | None = None, *, type: str = "submit", disabled: bool | None = None) -> ParsedButton:
    rendered = name if value is None else value
    return ParsedButton(
        name=name,
        type=type,
        value=rendered,
        label=rendered or name,
        sensitive=is_sensitive_name(name),
        disabled=disabled,
    )


def form(
    action: str,
    *,
    method: str = "POST",
    fields: Iterable[str] = (),
    selects: Iterable[str] = (),
    textareas: Iterable[str] = (),
    buttons: Iterable[str] = (),
) -> ParsedForm:
    return ParsedForm(
        method=method,
        action=action,
        field_names=list(fields),
        select_names=list(selects),
        textarea_names=list(textareas),
        button_names=list(buttons),
    )


def table(headers: Sequence[str], *rows: Sequence[str]) -> list[dict[str, str]]:
    """Flatten a >= 3-column table into parser-style rows (empty-header columns dropped)."""
    out: list[dict[str, str]] = []
    for row in rows:
        record: dict[str, str] = {}
        for i, header in enumerate(headers):
            key = header.strip() or ("Metric" if i == 0 else "")
            if key:
                record[key] = row[i].strip()
        if record:
            out.append(record)
    return out


def page(
    name: str,
    *,
    title: str = "",
    heading: str = "",
    values: dict[str, str] | None = None,
    tables: Sequence[dict[str, str]] = (),
    fields: Sequence[ParsedField] = (),
    selects: Sequence[ParsedSelect] = (),
    textareas: Sequence[ParsedTextarea] = (),
    buttons: Sequence[ParsedButton] = (),
    forms: Sequence[ParsedForm] = (),
) -> ParsedPage:
    return ParsedPage(
        page=name,
        title=title,
        heading=heading,
        values=dict(values or {}),
        tables=list(tables),
        fields=list(fields),
        selects=list(selects),
        textareas=list(textareas),
        buttons=list(buttons),
        forms=list(forms),
    )


# --- Router page fixtures shared by the snapshot and restore tests -------------------------------
# Each mirrors one inline-HTML constant of the TS test files.

SERVICES_HEADERS = ("Service Name", "Global Port Range", "Base Host Port", "Protocol", "")
ServiceRow = tuple[str, str, str, str]


def services_page(
    rows: Sequence[ServiceRow] = (("custom_ssh", "2483-2483", "22", "TCP"), ("Mosh", "60001-60010", "60001", "UDP")),
    *,
    headers: Sequence[str] = SERVICES_HEADERS,
    protocol_options: Sequence[str | tuple[str, str]] = ("TCP", "UDP"),
    remove_buttons: Sequence[str] | None = None,
    extra_tables: Sequence[dict[str, str]] = (),
) -> ParsedPage:
    """services.ha: a table of custom services with Remove_<n> buttons and the Add form."""
    buttons = [button(n, "Remove") for n in (remove_buttons if remove_buttons is not None else [])]
    if remove_buttons is None:
        buttons = [button(f"Remove_{i + 1}", "Remove") for i in range(len(rows))]
    return page(
        "services",
        title="Custom Services",
        tables=[*extra_tables, *table(headers, *[(*r, "") for r in rows])],
        fields=[
            hidden("nonce", "abc"),
            field("Service"),
            field("extMinPort"),
            field("extMaxPort"),
            field("intStartPort"),
        ],
        selects=[select("protocol", protocol_options)],
        buttons=[*buttons, button("Add", "Add")],
        forms=[form("/cgi-bin/services.ha")],
    )


APPHOSTING_HEADERS = ("Service", "Needed by Device", "")
DEVICE_OPTIONS = (
    ("aa:bb:cc:dd:ee:01", "host-b"),
    ("aa:bb:cc:dd:ee:02", "host-a"),
    ("aa:bb:cc:dd:ee:03", "watch"),
    ("aa:bb:cc:dd:ee:04", "watch"),
)


def apphosting_page(
    rows: Sequence[tuple[str, str]] = (("custom_ssh", "host-b"), ("Mosh", "host-a")),
    *,
    headers: Sequence[str] = APPHOSTING_HEADERS,
    service_options: Sequence[str | tuple[str, str]] = ("custom_ssh", "Mosh"),
    device_options: Sequence[tuple[str, str]] = DEVICE_OPTIONS,
) -> ParsedPage:
    """apphosting.ha (NAT/Gaming): forwards table, service + device dropdowns, Add button."""
    return page(
        "apphosting",
        title="NAT/Gaming",
        tables=table(headers, *[(*r, "") for r in rows]),
        fields=[hidden("nonce", "abc")],
        selects=[select("service", service_options), select("device", list(device_options))],
        buttons=[*[button(f"Remove_{i + 1}", "Remove") for i in range(len(rows))], button("Add", "Add")],
        forms=[form("/cgi-bin/apphosting.ha")],
    )


IPALLOC_HEADERS = ("IPv4 Address / Name", "MAC Address", "Status", "Allocation", "Action")
IPALLOC_ROWS = (
    ("192.168.1.64", "02:0A:0B:0C:0D:02", "on", "Fixed Allocation"),
    ("watch", "02:0a:0b:0c:0d:04", "off", "DHCP Allocation"),
    ("192.168.1.70", "02:0a:0b:0c:0d:03", "on", "Fixed Allocation"),
)


def ipalloc_page(
    rows: Sequence[tuple[str, str, str, str]] = IPALLOC_ROWS,
    *,
    headers: Sequence[str] = IPALLOC_HEADERS,
    extra_selects: Sequence[ParsedSelect] = (),
    extra_buttons: Sequence[ParsedButton] = (),
) -> ParsedPage:
    """ipalloc.ha: allocation table; each row carries an Allocate_<mac> button (mac lower-cased)."""
    return page(
        "ipalloc",
        title="IP Allocation",
        tables=table(headers, *[(*r, "") for r in rows]),
        fields=[hidden("nonce", "abc")],
        selects=list(extra_selects),
        buttons=[*[button(f"Allocate_{r[1].lower()}", "Allocate") for r in rows], *extra_buttons],
        forms=[form("/cgi-bin/ipalloc.ha")],
    )


def dosprotect_page(
    *,
    fields: Sequence[ParsedField] = (),
    selects: Sequence[ParsedSelect] = (),
    save_button: str | None = "Save",
) -> ParsedPage:
    buttons = [button(save_button, save_button)] if save_button else []
    return page(
        "dosprotect",
        title="Firewall Advanced",
        fields=[hidden("nonce", "abc"), *fields],
        selects=list(selects),
        buttons=buttons,
        forms=[form("/cgi-bin/dosprotect.ha")],
    )


def sysinfo_page(version: str = "4.27.7") -> ParsedPage:
    return page("sysinfo", title="System Information", values={"Software Version": version})
