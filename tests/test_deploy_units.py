"""The systemd units the deploy README installs carry the sections its commands need."""

from __future__ import annotations

import configparser
import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # systemd keys are case-sensitive
    parser.read_string((DEPLOY / name).read_text())
    return parser


def test_readme_enables_the_timer_so_the_timer_unit_needs_an_install_section():
    readme = (DEPLOY / "README.md").read_text()
    assert re.search(r"systemctl --user enable --now bgw-autorestore\.timer", readme)
    timer = unit("bgw-autorestore.timer")
    assert timer["Install"]["WantedBy"] == "timers.target"
    assert {"OnBootSec", "OnUnitActiveSec"} <= set(timer["Timer"])


def test_service_runs_python_unbuffered_so_progress_reaches_the_journal_line_by_line():
    """Under systemd stdout is a pipe, so Python block-buffers it: a kill at the start timeout or a
    power loss would lose the whole run's output without this."""
    service = unit("bgw-autorestore.service")["Service"]
    assert "PYTHONUNBUFFERED=1" in service["Environment"].split()


def test_service_is_a_oneshot_with_an_exec_line():
    service = unit("bgw-autorestore.service")
    assert service["Service"]["Type"] == "oneshot"
    assert "ExecStart" in service["Service"]


def _seconds(value: str) -> float:
    """A systemd time span made of `<number><unit>` parts (`1320`, `22min`, `1h 5min`)."""
    units = {"": 1, "s": 1, "sec": 1, "min": 60, "m": 60, "h": 3600, "hr": 3600}
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]*)", value.strip())
    assert parts and "".join(f"{n}{u}" for n, u in parts) == re.sub(r"\s+", "", value)
    return sum(float(number) * units[suffix] for number, suffix in parts)


def test_start_timeout_covers_the_worst_case_autorestore_run():
    """The run's own time limit + the one step that may still be in flight (nonce read, POST and the
    acknowledgement window) + an upper bound for the one closing fetch of the 11 snapshot pages (7 always,
    up to 4 optional form pages) + a margin."""
    from bgwcli.allocation_preflight import RESCAN_TIMEOUT_SECONDS
    from bgwcli.autorestore import RUN_DEADLINE_SECONDS, UNIT_MARGIN_SECONDS, AutorestoreOptions
    from bgwcli.restore import SAVE_CONFIRMATION_TIMEOUT_SECONDS

    options = AutorestoreOptions(commit=True)
    page_timeout = 15  # the default per-page timeout
    in_flight = 2 * page_timeout + SAVE_CONFIRMATION_TIMEOUT_SECONDS
    closing_fetch = 37 * page_timeout  # 555 s, an upper bound for the 11 snapshot pages
    worst = options.max_run_seconds + in_flight + closing_fetch + UNIT_MARGIN_SECONDS
    assert worst == 1800 + 90 + 555 + 135 == 2580
    assert options.max_run_seconds == RUN_DEADLINE_SECONDS
    # The limit leaves room for the ownership window and the passes it bounds.
    assert RESCAN_TIMEOUT_SECONDS + closing_fetch + options.wait_seconds < RUN_DEADLINE_SECONDS
    assert _seconds(unit("bgw-autorestore.service")["Service"]["TimeoutStartSec"]) >= worst
