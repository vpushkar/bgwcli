"""Rewrite the generated blocks in README.md from bgwcli.helpdoc (pages, include pages, actions).

Run from the repository root: .venv/bin/python -I scripts/update_readme_pages.py
Exits 1 without writing anything when any marker pair is missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bgwcli import helpdoc  # noqa: E402


def main() -> int:
    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    for name in helpdoc.README_BLOCKS:
        if f"<!-- {name}:start -->\n" not in text or f"<!-- {name}:end -->" not in text:
            sys.stderr.write(f"README.md is missing the <!-- {name}:start/end --> markers\n")
            return 1
    for name, renderer in helpdoc.README_BLOCKS.items():
        start, end = f"<!-- {name}:start -->\n", f"<!-- {name}:end -->"
        head, rest = text.split(start, 1)
        _, tail = rest.split(end, 1)
        text = head + start + getattr(helpdoc, renderer)() + end + tail
    readme.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
