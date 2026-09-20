"""Router page parsers built on a light DOM produced by ``html.parser.HTMLParser``.

The TS CLI scanned raw HTML with regular expressions; this module builds a small element tree
instead and derives the same ``ParsedPage`` shape from it (same fields, selects, buttons, forms,
tables, values, value entries and links for the same markup). Behavior is pinned by
tests/test_parser.py, which carries every expectation the TS test-suite had for the parser.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .html import normalize_whitespace
from .redact import is_sensitive_name, redact_value
from .types import (
    Device,
    LogEntry,
    ParsedButton,
    ParsedField,
    ParsedForm,
    ParsedLink,
    ParsedPage,
    ParsedSelect,
    ParsedTextarea,
    ParsedValueEntry,
    SelectOption,
    SitemapEntry,
)

Record = dict[str, str]

BUTTON_INPUT_TYPES = frozenset({"submit", "button", "reset", "image"})
_CONTROL_TAGS = frozenset({"input", "select", "textarea", "button"})
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_TABLE_SECTION_TAGS = frozenset({"table", "thead", "tbody", "tfoot"})
_P_CLOSERS = frozenset(
    {"p", "div", "table", "form", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "pre", "blockquote", "hr"}
)
_SKIP_TEXT_TAGS = frozenset({"script", "style"})

_CGI_PAGE = re.compile(r"/cgi-bin/([^/?#]+)\.ha", re.IGNORECASE)
_SITEMAP_HREF = re.compile(r".*/cgi-bin/(.+?)\.ha", re.IGNORECASE)
_CURRENTLY = re.compile(r"\s+Currently\s+")
_CURRENTLY_I = re.compile(r"\s+Currently\s+", re.IGNORECASE)
_DEFAULT_LABEL = re.compile(r"^(.*?\bDefault):\s*(.+)$", re.IGNORECASE)
_DESC_CLASS = re.compile(r"\bdesc\b")
_BAND_HEADING = re.compile(r"^((?:2\.4|5)\s*GHz)", re.IGNORECASE)
_CHANNEL_WIDTH = re.compile(r"(\d+)\s*\(([^)]+)\)")
_HTTP_URL = re.compile(r"^https?://", re.IGNORECASE)
_LOGIN_ACTION = re.compile(r"/cgi-bin/login\.ha$", re.IGNORECASE)
_ACCESS_CODE_REQUIRED = re.compile(r"Access Code Required", re.IGNORECASE)
_LOGIN_TITLE = re.compile(r"^Login$", re.IGNORECASE)


# --- DOM-lite -----------------------------------------------------------------------------------


@dataclass(eq=False)
class Element:
    """One HTML element. ``start``/``end`` are document-order counters (start tag / end tag events)."""

    tag: str
    attrs: dict[str, str]
    start: int
    end: int = -1
    children: list[Element | str] = field(default_factory=list)

    def attr(self, name: str) -> str | None:
        return self.attrs.get(name)

    def has(self, name: str) -> bool:
        return name in self.attrs

    def iter_elements(self, *, stop: frozenset[str] = frozenset()) -> Iterator[Element]:
        """Depth-first descendants in document order; subtrees rooted at a ``stop`` tag are yielded but not entered."""
        for child in self.children:
            if isinstance(child, Element):
                yield child
                if child.tag not in stop:
                    yield from child.iter_elements(stop=stop)

    def find_all(self, tag: str, *, stop: frozenset[str] = frozenset()) -> list[Element]:
        return [el for el in self.iter_elements(stop=stop) if el.tag == tag]

    def text(self) -> str:
        """Text content with every tag boundary rendered as a space, whitespace normalized (TS stripTags)."""
        parts: list[str] = []
        self._collect_text(parts)
        return normalize_whitespace("".join(parts))

    def _collect_text(self, parts: list[str]) -> None:
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
                continue
            parts.append(" ")
            if child.tag not in _SKIP_TEXT_TAGS:
                child._collect_text(parts)
            parts.append(" ")


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#document", {}, 0)
        self.elements: list[Element] = []
        self._stack: list[Element] = [self.root]
        self._counter = 0

    @property
    def _current(self) -> Element:
        return self._stack[-1]

    def _tick(self) -> int:
        self._counter += 1
        return self._counter

    def _close_nearest(self, targets: frozenset[str], barriers: frozenset[str]) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            tag = self._stack[index].tag
            if tag in targets:
                self._pop_to(index)
                return
            if tag in barriers:
                return

    def _pop_to(self, index: int) -> None:
        while len(self._stack) > index:
            self._stack.pop().end = self._tick()

    def _implicit_close(self, tag: str) -> None:
        if tag in ("td", "th"):
            self._close_nearest(frozenset({"td", "th"}), frozenset({"tr", "table"}))
        elif tag == "tr":
            self._close_nearest(frozenset({"tr"}), _TABLE_SECTION_TAGS)
        elif tag == "option":
            self._close_nearest(frozenset({"option"}), frozenset({"select"}))
        elif tag == "li":
            self._close_nearest(frozenset({"li"}), frozenset({"ul", "ol"}))
        if tag in _P_CLOSERS and self._current.tag == "p":
            self._pop_to(len(self._stack) - 1)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._implicit_close(tag)
        element = Element(tag, {name: (value or "").replace("\xa0", " ") for name, value in attrs}, self._tick())
        self._current.children.append(element)
        self.elements.append(element)
        if tag in _VOID_TAGS:
            element.end = element.start
        else:
            self._stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self._pop_to(len(self._stack) - 1)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                self._pop_to(index)
                return

    def handle_data(self, data: str) -> None:
        self._current.children.append(data)

    def finish(self) -> Element:
        self.close()
        self._pop_to(1)
        self.root.end = self._tick()
        return self.root


def parse_document(html: str) -> Element:
    """Parse HTML into an element tree rooted at a synthetic ``#document`` element."""
    builder = _TreeBuilder()
    builder.feed(html)
    return builder.finish()


# --- table helpers --------------------------------------------------------------------------------


def _table_rows(table: Element) -> list[list[Element]]:
    """Cells of each row of ``table`` (nested tables excluded); rows without cells are dropped."""
    rows: list[list[Element]] = []
    for tr in table.find_all("tr", stop=frozenset({"table"})):
        cells = [el for el in tr.iter_elements(stop=frozenset({"table", "tr"})) if el.tag in ("td", "th")]
        if cells:
            rows.append(cells)
    return rows


def _table_texts(table: Element) -> list[list[str]]:
    return [[cell.text() for cell in row] for row in _table_rows(table)]


def extract_tables(html: str) -> list[list[list[str]]]:
    """Every table as rows of cell texts (the TS ``extractTables`` shape)."""
    return [_table_texts(table) for table in parse_document(html).find_all("table")]


def _first_text(root: Element, tag: str) -> str:
    found = root.find_all(tag)
    return found[0].text() if found else ""


def _control_name(el: Element) -> str:
    return el.attr("name") or el.attr("id") or ""


def _input_type(el: Element) -> str:
    return el.attr("type") or "text"


def _is_button_input(el: Element) -> bool:
    return _input_type(el).lower() in BUTTON_INPUT_TYPES


def _flag(el: Element, name: str) -> bool | None:
    return True if el.has(name) else None


def _dedupe_records(records: list[Record]) -> list[Record]:
    seen: set[str] = set()
    output: list[Record] = []
    for record in records:
        key = json.dumps(record, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        output.append(record)
    return output


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


# --- public parsers -------------------------------------------------------------------------------


def parse_sitemap(html: str) -> list[SitemapEntry]:
    """``<a href=".../cgi-bin/<page>.ha">label</a>`` links, de-duplicated and sorted by (page, label)."""
    return _sitemap_entries(parse_document(html))


def _sitemap_entries(root: Element) -> list[SitemapEntry]:
    entries: list[SitemapEntry] = []
    seen: set[tuple[str, str]] = set()
    for anchor in root.find_all("a"):
        href = anchor.attr("href") or ""
        match = _SITEMAP_HREF.fullmatch(href)
        if not match:
            continue
        page = match.group(1)
        label = anchor.text()
        if not page or not label or (page, label) in seen:
            continue
        seen.add((page, label))
        entries.append(SitemapEntry(page=page, label=label, href=href))
    return sorted(entries, key=lambda entry: (entry.page, entry.label))


def parse_page(page: str, html: str, include_secrets: bool = False) -> ParsedPage:
    """Parse one router page into the shared ``ParsedPage`` model, redacting secrets unless asked not to."""
    root = parse_document(html)
    values: Record = {}

    value_entries = _parse_value_entries(root, include_secrets)
    for entry in value_entries:
        if entry.value:
            values[entry.label] = entry.value

    generic_tables = _parse_generic_tables(root, include_secrets)

    for h1 in root.find_all("h1"):
        parts = _CURRENTLY.split(h1.text())
        if len(parts) == 2 and parts[0] and parts[1]:
            values[parts[0].strip()] = redact_value(parts[0], parts[1].strip(), include_secrets)

    for index, description in enumerate(_parse_description_blocks(root)):
        key = "Description" if index == 0 else f"Description {index + 1}"
        values[key] = redact_value("Description", description, include_secrets)

    fields = _parse_fields(root, include_secrets)
    selects = _parse_selects(root, include_secrets)
    textareas = _parse_textareas(root, include_secrets)
    buttons = _parse_buttons(root, include_secrets)
    forms = _parse_forms(root)
    links = _parse_links(root)

    for control in fields:
        if control.type in ("radio", "checkbox") and not control.checked:
            continue
        if control.value and control.name not in ("nonce", "hashpassword"):
            values[f"Field {control.name}"] = control.value
    for select in selects:
        if select.value:
            values[f"Field {select.name}"] = select.value
    for textarea in textareas:
        if textarea.value:
            values[f"Field {textarea.name}"] = textarea.value

    if page == "sitemap":
        parsed_tables = [{"Page": e.page, "Label": e.label, "Href": e.href} for e in _sitemap_entries(root)]
    elif page == "speed":
        parsed_tables = _parse_speed_tables(root)
    elif page == "diag":
        parsed_tables = _parse_diagnostic_tables(root)
    else:
        parsed_tables = _dedupe_records(generic_tables)
    if page in ("etherlan", "fiberstat", "voice", "voiceconfig", "voicestat"):
        parsed_tables = []
    page_tables = _parse_page_specific_tables(page, root, fields, selects, include_secrets)

    return ParsedPage(
        page=page,
        title=_first_text(root, "title"),
        heading=_first_text(root, "h1"),
        values=values,
        value_entries=value_entries,
        tables=_dedupe_records([*parsed_tables, *page_tables]),
        fields=fields,
        selects=selects,
        textareas=textareas,
        buttons=buttons,
        forms=forms,
        links=links,
    )


def parse_devices(html: str) -> list[Device]:
    """Device list rows from either the key/value table shape or the wide table shape."""
    devices: list[Device] = []
    for table in extract_tables(html):
        if not any("mac address" in cell.lower() for row in table for cell in row):
            continue
        if any(row[0] == "MAC Address" and len(row) == 2 for row in table):
            devices.extend(_devices_from_key_value_table(table))
            continue
        for row in table[1:]:
            if len(row) < 4:
                continue
            status, name_ip = row[0], row[1]
            split = name_ip.split("/")
            ip = normalize_whitespace(split[0]) if len(split) > 1 else row[2]
            name = normalize_whitespace("/".join(split[1:])) if len(split) > 1 else name_ip
            devices.append(
                Device(
                    status=status,
                    name=name,
                    ip=ip or "Unknown",
                    mac=row[3],
                    connection=row[4] if len(row) > 4 else "Unknown",
                )
            )
    return devices


def _devices_from_key_value_table(table: list[list[str]]) -> list[Device]:
    devices: list[Device] = []
    current: Record = {}

    def flush() -> None:
        if not current.get("MAC Address"):
            return
        name_ip = current.get("IPv4 Address / Name", current.get("Name", ""))
        split = name_ip.split("/")
        has_ip = len(split) > 1
        devices.append(
            Device(
                status=current.get("Status", ""),
                name=normalize_whitespace("/".join(split[1:])) if has_ip else current.get("Name", name_ip),
                ip=normalize_whitespace(split[0]) if has_ip else "",
                mac=current.get("MAC Address", ""),
                connection=current.get("Connection Type", ""),
                connection_speed=current.get("Connection Speed"),
                allocation=current.get("Allocation"),
                last_activity=current.get("Last Activity"),
                mesh_client=current.get("Mesh Client"),
                ipv6=current.get("IPv6 Address"),
                device_type=current.get("Type"),
                valid_lifetime=current.get("Valid Lifetime"),
                preferred_lifetime=current.get("Preferred Lifetime"),
            )
        )

    for row in table:
        if len(row) < 2 or not row[0]:
            continue
        key, value = row[0], row[1]
        if key == "MAC Address":
            flush()
            current = {}
        current[key] = value
    flush()
    return devices


def parse_logs(html: str) -> list[LogEntry]:
    """Rows of the first table with at least six columns (header row skipped)."""
    tables = extract_tables(html)
    table = tables[0] if tables else []
    return [
        LogEntry(id=row[0], time=row[1], source=row[2], destination=row[3], protocol=row[4], reason=row[5])
        for row in table[1:]
        if len(row) >= 6
    ]


def looks_like_login(html: str) -> bool:
    """True when the router answered with its login page instead of the requested page."""
    root = parse_document(html)
    if _LOGIN_TITLE.match(_first_text(root, "title")):
        return True
    if _ACCESS_CODE_REQUIRED.search(html):
        return True
    login_forms = [f for f in root.find_all("form") if _LOGIN_ACTION.search(f.attr("action") or "")]
    if not login_forms:
        return False
    first_form_start = min(f.start for f in login_forms)
    return any(el.attr("id") == "password" and el.start > first_form_start for el in root.iter_elements())


# --- value entries, descriptions, links ---------------------------------------------------------


def _nearest_heading(root: Element, before: int) -> str:
    heading = ""
    for el in root.iter_elements():
        if el.start >= before:
            break
        if el.tag in ("h1", "h2", "h3"):
            heading = el.text()
    return heading


def _split_default_value(label: str, include_secrets: bool) -> tuple[str, str | None]:
    match = _DEFAULT_LABEL.match(label)
    if not match or not match.group(1) or not match.group(2):
        return label, None
    normalized = normalize_whitespace(match.group(1))
    return normalized, redact_value(normalized, normalize_whitespace(match.group(2)), include_secrets)


def _parse_value_entries(root: Element, include_secrets: bool) -> list[ParsedValueEntry]:
    entries: list[ParsedValueEntry] = []
    for table in root.find_all("table"):
        section = _nearest_heading(root, table.start)
        for cells in _table_rows(table):
            if len(cells) != 2 or any(el.tag in _CONTROL_TAGS for el in cells[1].iter_elements()):
                continue
            raw_label = re.sub(r":$", "", cells[0].text())
            if not raw_label:
                continue
            label, default_value = _split_default_value(raw_label, include_secrets)
            value = redact_value(label, cells[1].text(), include_secrets)
            entries.append(ParsedValueEntry(section=section, label=label, value=value, default_value=default_value))
    return entries


def _parse_description_blocks(root: Element) -> list[str]:
    descriptions = [
        div.text() for div in root.find_all("div") if _DESC_CLASS.search(div.attr("class") or "") and div.text()
    ]
    return _unique(descriptions)


def _parse_links(root: Element) -> list[ParsedLink]:
    links: list[ParsedLink] = []
    seen: set[tuple[str, str]] = set()
    for anchor in root.find_all("a"):
        href = anchor.attr("href") or ""
        if not href:
            continue
        label = anchor.text() or anchor.attr("title") or href
        match = _CGI_PAGE.search(href)
        page = match.group(1) if match else None
        kind = "router-page" if page else ("external" if _HTTP_URL.match(href) else "other")
        if (href, label) in seen:
            continue
        seen.add((href, label))
        links.append(ParsedLink(label=label, href=href, kind=kind, page=page))
    return links


# --- controls -------------------------------------------------------------------------------------


def _parse_fields(root: Element, include_secrets: bool) -> list[ParsedField]:
    fields: list[ParsedField] = []
    for el in root.find_all("input"):
        name = _control_name(el)
        if not name or _is_button_input(el):
            continue
        fields.append(
            ParsedField(
                name=name,
                type=_input_type(el),
                value=redact_value(name, el.attr("value") or "", include_secrets),
                checked=el.has("checked"),
                sensitive=is_sensitive_name(name),
                disabled=_flag(el, "disabled"),
                read_only=_flag(el, "readonly"),
            )
        )
    return fields


def _parse_selects(root: Element, include_secrets: bool) -> list[ParsedSelect]:
    selects: list[ParsedSelect] = []
    for el in root.find_all("select"):
        name = _control_name(el)
        if not name:
            continue
        options: list[SelectOption] = []
        for option in el.find_all("option"):
            label = option.text()
            options.append(
                SelectOption(
                    value=option.attr("value") or label,
                    label=label,
                    selected=option.has("selected"),
                    disabled=option.has("disabled"),
                )
            )
        selected = next((o for o in options if o.selected), options[0] if options else None)
        selects.append(
            ParsedSelect(
                name=name,
                value=redact_value(name, selected.value if selected else "", include_secrets),
                options=[redact_value(name, o.value, include_secrets) for o in options],
                sensitive=is_sensitive_name(name),
                option_details=[
                    SelectOption(
                        value=redact_value(name, o.value, include_secrets),
                        label=redact_value(name, o.label, include_secrets),
                        selected=o.selected,
                        disabled=o.disabled,
                    )
                    for o in options
                ],
                disabled=_flag(el, "disabled"),
            )
        )
    return selects


def _parse_textareas(root: Element, include_secrets: bool) -> list[ParsedTextarea]:
    textareas: list[ParsedTextarea] = []
    for el in root.find_all("textarea"):
        name = _control_name(el)
        if not name:
            continue
        textareas.append(
            ParsedTextarea(
                name=name,
                value=redact_value(name, el.text(), include_secrets),
                sensitive=is_sensitive_name(name),
                disabled=_flag(el, "disabled"),
                read_only=_flag(el, "readonly"),
            )
        )
    return textareas


def _parse_buttons(root: Element, include_secrets: bool) -> list[ParsedButton]:
    """Button-type ``<input>`` controls first (document order), then ``<button>`` elements — as the TS CLI did."""
    buttons: list[ParsedButton] = []
    for el in root.find_all("input"):
        if not _is_button_input(el):
            continue
        button_type = _input_type(el).lower()
        value = el.attr("value") or ""
        name = _control_name(el) or value or button_type
        buttons.append(
            ParsedButton(
                name=name,
                type=button_type,
                value=redact_value(name, value, include_secrets),
                label=redact_value(name, value or name, include_secrets),
                sensitive=is_sensitive_name(name),
                disabled=_flag(el, "disabled"),
            )
        )
    for el in root.find_all("button"):
        button_type = el.attr("type") or "submit"
        label = el.text()
        value_attr = el.attr("value")
        name = _control_name(el) or value_attr or label or button_type
        buttons.append(
            ParsedButton(
                name=name,
                type=button_type,
                value=redact_value(name, label if value_attr is None else value_attr, include_secrets),
                label=redact_value(name, label or value_attr or name, include_secrets),
                sensitive=is_sensitive_name(name),
                disabled=_flag(el, "disabled"),
            )
        )
    return buttons


def _parse_forms(root: Element) -> list[ParsedForm]:
    forms: list[ParsedForm] = []
    for form in root.find_all("form"):
        inputs = form.find_all("input")
        forms.append(
            ParsedForm(
                method=(form.attr("method") or "GET").upper(),
                action=form.attr("action") or "",
                field_names=_unique([_control_name(i) for i in inputs if not _is_button_input(i) and _control_name(i)]),
                select_names=_unique([_control_name(s) for s in form.find_all("select") if _control_name(s)]),
                textarea_names=_unique([_control_name(t) for t in form.find_all("textarea") if _control_name(t)]),
                button_names=_unique(
                    [_control_name(i) for i in inputs if _is_button_input(i) and _control_name(i)]
                    + [_control_name(b) for b in form.find_all("button") if _control_name(b)]
                ),
            )
        )
    return forms


# --- tables ---------------------------------------------------------------------------------------


def _parse_generic_tables(root: Element, include_secrets: bool) -> list[Record]:
    """Tables with more than two header columns become one record per same-width row (header -> cell)."""
    records: list[Record] = []
    for table in root.find_all("table"):
        rows = _table_texts(table)
        if not rows or len(rows[0]) <= 2:
            continue
        headers = rows[0]
        for row in rows[1:]:
            if len(row) != len(headers):
                continue
            record: Record = {}
            for index, header in enumerate(headers):
                key = header or ("Metric" if index == 0 else "")
                if key:
                    record[key] = redact_value(key, row[index], include_secrets)
            if record:
                records.append(record)
    return records


def _parse_speed_tables(root: Element) -> list[Record]:
    return [
        {"Time": row[0], "Direction": row[1], "Mbps": row[2], "Server": row[3], "Latency ms": row[4], "Result": row[5]}
        for table in root.find_all("table")
        for row in _table_texts(table)
        if len(row) >= 6
    ]


def _parse_diagnostic_tables(root: Element) -> list[Record]:
    return [
        {"Test": row[0], "Status": row[1]}
        for table in root.find_all("table")
        for row in _table_texts(table)
        if len(row) >= 2 and row[0] in ("Ethernet", "Authentication", "IP", "DNS")
    ]


def _parse_page_specific_tables(
    page: str, root: Element, fields: list[ParsedField], selects: list[ParsedSelect], include_secrets: bool
) -> list[Record]:
    if page == "wconfig_unified":
        return _parse_wifi_channel_tables(root)
    if page == "wconfig":
        return _parse_advanced_wifi_tables(fields, selects)
    if page == "etherlan":
        return _parse_ethernet_port_tables(selects)
    if page == "wmacauth":
        return _parse_wifi_mac_filter_tables(selects)
    if page == "fiberstat":
        return _parse_fiber_threshold_tables(root)
    if page in ("voice", "voiceconfig"):
        return _parse_voice_status_tables(root, include_secrets)
    if page == "voicestat":
        return _parse_voice_statistics_tables(root, include_secrets)
    return []


def _first_table_after(root: Element, start: int, *, stop_at_h2: bool = False) -> Element | None:
    """First table after document position ``start``; optionally bounded by the next ``<h2>`` or a ``</form>``."""
    form_ends = [f.end for f in root.find_all("form")] if stop_at_h2 else []
    for el in root.iter_elements():
        if el.start <= start:
            continue
        if stop_at_h2 and el.tag == "h2":
            return None
        if el.tag == "table":
            if any(start < form_end < el.start for form_end in form_ends):
                return None
            return el
    return None


def _parse_wifi_channel_tables(root: Element) -> list[Record]:
    channels: list[Record] = []
    for h2 in root.find_all("h2"):
        match = _BAND_HEADING.match(h2.text())
        if not match:
            continue
        band = normalize_whitespace(match.group(1))
        table = _first_table_after(root, h2.end, stop_at_h2=True)
        rows = _table_texts(table) if table else []
        row = next((r for r in rows if r[0] == "Current Channel"), None)
        if row is None:
            continue
        current = row[1] if len(row) > 1 else ""
        mode = row[2] if len(row) > 2 else ""
        parsed = list(_CHANNEL_WIDTH.finditer(current))
        if not parsed:
            channels.append({"Radio": band, "Current Channel": current, "Channel Width": "", "Mode": mode})
            continue
        for entry in parsed:
            channel = entry.group(1)
            channels.append(
                {
                    "Radio": _radio_name(band, channel, len(parsed)),
                    "Current Channel": channel,
                    "Channel Width": normalize_whitespace(entry.group(2)),
                    "Mode": mode,
                }
            )
    return channels


def _radio_name(band: str, channel: str, count: int) -> str:
    if band.lower().startswith("2.4") or count == 1:
        return band
    return "5 GHz low-band" if int(channel) < 100 else "5 GHz high-band"


def _parse_advanced_wifi_tables(fields: list[ParsedField], selects: list[ParsedSelect]) -> list[Record]:
    def field_value(name: str) -> str:
        return next((f.value for f in fields if f.name == name), "")

    def select_value(name: str) -> str:
        return next((s.value for s in selects if s.name == name), "")

    return [
        {
            "Section": "Radio",
            "Radio": "2.4 GHz",
            "Enabled": select_value("wl80211on"),
            "Standard": select_value("standard"),
            "Bandwidth": select_value("bandwidth"),
            "Channel": select_value("channelplusauto"),
            "Power level (%)": field_value("power"),
        },
        {
            "Section": "Radio",
            "Radio": "5 GHz (combined configuration)",
            "Enabled": select_value("wl80211on_5"),
            "Standard": select_value("standard_5"),
            "Bandwidth": select_value("bandwidth_5"),
            "Channel": "",
            "Power level (%)": field_value("power_5"),
        },
        {
            "Section": "SSID",
            "Radio": "2.4 GHz",
            "Network": "Home",
            "Enabled": select_value("ussidenable"),
            "SSID": field_value("ssidname11"),
            "Hidden": select_value("hide"),
            "Security": select_value("security11"),
            "WPA version": select_value("wpaversion"),
            "WPS": select_value("wps"),
            "Maximum clients": field_value("maxclients"),
        },
        {
            "Section": "SSID",
            "Radio": "2.4 GHz",
            "Network": "Guest",
            "Enabled": select_value("gssidenable"),
            "SSID": field_value("ssidname12"),
            "Hidden": select_value("hide2"),
            "Security": select_value("security12"),
            "WPA version": select_value("wpaversion2"),
            "WPS": "",
            "Maximum clients": field_value("maxclients2"),
        },
        {
            "Section": "SSID",
            "Radio": "5 GHz (combined configuration)",
            "Network": "Home",
            "Enabled": select_value("wl80211on_5"),
            "SSID": field_value("ssidname21"),
            "Hidden": select_value("hide_5"),
            "Security": select_value("security21"),
            "WPA version": select_value("wpaversion_5"),
            "WPS": select_value("wps_5"),
            "Maximum clients": field_value("maxclients_5"),
        },
    ]


def _option_labels(select: ParsedSelect | None) -> list[str]:
    if select is None:
        return []
    if select.option_details is not None:
        return [o.label for o in select.option_details]
    return list(select.options)


def _parse_ethernet_port_tables(selects: list[ParsedSelect]) -> list[Record]:
    records: list[Record] = []
    for port in (1, 2, 3, 4):
        media = next((s for s in selects if s.name == f"enet{port}_port{port}_media"), None)
        mdix = next((s for s in selects if s.name == f"enet{port}_port{port}_mdix"), None)
        records.append(
            {
                "Port": str(port),
                "Configured media": media.value if media else "",
                "Configured MDI-X": mdix.value if mdix else "",
                "Supported media modes": ", ".join(_option_labels(media)),
                "Supported MDI-X modes": ", ".join(_option_labels(mdix)),
            }
        )
    return records


def _parse_wifi_mac_filter_tables(selects: list[ParsedSelect]) -> list[Record]:
    definitions = (
        ("2.4 GHz", "Home", "wmacr1user"),
        ("2.4 GHz", "Guest", "wmacr1guest"),
        ("5 GHz", "Home", "wmacr2user"),
    )
    return [
        {"Radio": radio, "Network": network, "Filtering": next((s.value for s in selects if s.name == name), "")}
        for radio, network, name in definitions
    ]


def _parse_fiber_threshold_tables(root: Element) -> list[Record]:
    records: list[Record] = []
    for h1 in root.find_all("h1"):
        heading = h1.text()
        if not _CURRENTLY_I.search(heading):
            continue
        measurement, current = (_CURRENTLY.split(heading, maxsplit=1) + [""])[:2]
        table = _first_table_after(root, h1.end)
        rows = _table_texts(table) if table else []
        for row in rows[1:]:
            if len(row) != 3:
                continue
            records.append(
                {
                    "Measurement": normalize_whitespace(measurement),
                    "Current": normalize_whitespace(current),
                    "State": row[0],
                    "Low": row[1],
                    "High": row[2],
                }
            )
    return records


def _parse_voice_statistics_tables(root: Element, include_secrets: bool) -> list[Record]:
    tables = [_table_texts(t) for t in root.find_all("table")]
    records: list[Record] = []

    for index, line in enumerate(("Line 1", "Line 2")):
        table = tables[index] if index < len(tables) else []
        for row in table[2:]:
            if len(row) != 5:
                continue
            records.append(
                {
                    "Section": "Call statistics",
                    "Line": line,
                    "Metric": row[0],
                    "Last call incoming": row[1],
                    "Last call outgoing": row[2],
                    "Cumulative incoming": row[3],
                    "Cumulative outgoing": row[4],
                }
            )

    summary = tables[2] if len(tables) > 2 else []
    for row in summary[2:]:
        if len(row) != 5:
            continue
        metric = row[0]
        records.append(
            {
                "Section": "Call summary",
                "Line": "Both",
                "Metric": metric,
                "Line 1 current": redact_value(metric, row[1], include_secrets),
                "Line 1 last": redact_value(metric, row[2], include_secrets),
                "Line 2 current": redact_value(metric, row[3], include_secrets),
                "Line 2 last": redact_value(metric, row[4], include_secrets),
            }
        )

    cumulative = tables[3] if len(tables) > 3 else []
    for row in cumulative[1:]:
        if len(row) != 3:
            continue
        records.append(
            {
                "Section": "Cumulative since last reset",
                "Line": "Both",
                "Metric": row[0],
                "Line 1": row[1],
                "Line 2": row[2],
            }
        )
    return records


def _parse_voice_status_tables(root: Element, include_secrets: bool) -> list[Record]:
    records: list[Record] = []
    for table in root.find_all("table"):
        for row in _table_texts(table)[1:]:
            if len(row) != 3 or not row[0]:
                continue
            metric = row[0]
            records.append(
                {
                    "Metric": metric,
                    "Line 1": redact_value(metric, row[1], include_secrets),
                    "Line 2": redact_value(metric, row[2], include_secrets),
                }
            )
    return records
