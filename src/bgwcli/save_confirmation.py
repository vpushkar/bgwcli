"""Read the gateway's save notifications in one DOM traversal."""

import re
from dataclasses import dataclass

from .parser import Element, TruncatedPageError, parse_document


def changes_saved_text(text: str) -> bool:
    return re.fullmatch(r"changes\s+saved\s*[.!]?", text.strip(), re.IGNORECASE) is not None


_NO_CHANGES = re.compile(r"No changes detected\s*\.\s*Save not performed\s*\.")


def no_changes_text(text: str) -> bool:
    return _NO_CHANGES.fullmatch(text) is not None


def no_changes_notice(html: str) -> str | None:
    """The gateway's "No changes detected. Save not performed." notification on any page (with or
    without the error icon), or None. An answer the parser cut at a bound that shows none raises
    ``TruncatedPageError``: the notice may have been in the part that was not read."""
    root = parse_document(html)
    for element in root.iter_elements(stop=frozenset({"script", "style"})):
        if element.tag == "div" and element.attr("id") == "error-message-text" and no_changes_text(element.text()):
            return element.text()
    _refuse_a_cut_answer(root, found=False)
    return None


def _refuse_a_cut_answer(root: Element, *, found: bool) -> None:
    if root.truncated and not found:
        raise TruncatedPageError("the answer page was cut by the parser's bounds; its save notice cannot be read")


@dataclass(frozen=True)
class SaveNotification:
    saved: bool = False
    no_changes: bool = False
    error: str | None = None


def save_notification(page: str, html: str) -> SaveNotification:
    saved = no_changes = False
    error = None
    previous = None
    root = parse_document(html)
    for element in root.iter_elements(stop=frozenset({"script", "style"})):
        if element.tag == "div" and element.attr("id") == "error-message-text":
            text = element.text()
            is_saved = changes_saved_text(text)
            is_no_changes = no_changes_text(text)
            saved |= is_saved
            no_changes |= is_no_changes
            if (not is_saved and not is_no_changes and previous is not None and previous.tag == "img"
                    and previous.attr("id") == "error-message-icon"
                    and "icon_error" in (previous.attr("src") or "")):
                error = error or text or None
        previous = element
    _refuse_a_cut_answer(root, found=saved or no_changes or error is not None)
    return SaveNotification(saved, no_changes, error)
