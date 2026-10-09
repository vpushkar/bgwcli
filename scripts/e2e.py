#!/usr/bin/env python3
"""End-to-end validation of bgwcli against a live gateway.

Usage: E2E_ACCESS_CODE=... .venv/bin/python scripts/e2e.py [--only substring] [--out DIR] [--skip-commits] [--quick]
Writes <out>/report.md, <out>/report.json and one <out>/<nn>-<slug>.{out,err} per case (all mode 0600,
created fresh: an existing file or a symlink at an output path is refused); dump files and sweep
artifacts of the run also go under <out>. Without --out a new private directory is made with
tempfile.mkdtemp; an --out directory must be new or empty. A case that exceeds its timeout fails.

Router writes this run commits (all dropped by --skip-commits):
  - `diagnostics ping`, `diagnostics nslookup` and `diagnostics traceroute` (DIAG): run a test, change no setting;
  - `action run-speed-test` (SPEED): runs a speed test, changes no setting;
  - `set dosprotect icmp_downstream_echo_rqst_drop_wan=...` (DOSPROTECT) twice: flips the live value,
    verifies it, then writes the captured live value back, verifies that, and diffs against the dump.
The dosprotect toggle and its restore are a guarded pair: once the toggle has been attempted, the restore
and its verification run even after an artifact-write failure, a timeout or an interrupt.

Exit codes: 0 clean; 1 one or more cases failed (or the report could not be written); 2 setup error (missing
E2E_ACCESS_CODE or CLI, unusable --out); 3 the dosprotect restore after a toggle could not be performed or
verified (the manual restore command is printed on stderr). An interrupt or error after the toggle still runs
and reports the restore: exit 3 when the restore is not verified, otherwise the interrupt or error propagates
after the note is printed. The guarantee is not absolute: a second interrupt (or error) raised while the
restore cases themselves run aborts the restore unreported, with no note, no report and no exit 3; restore by
hand with `bgwcli set dosprotect icmp_downstream_echo_rqst_drop_wan=<original value> --commit --confirm
DOSPROTECT`.

Everything else is a read, a dry-run or a refused commit. Never runs restart/reset/update/access-code/
clear-device-list actions or dangerous submits.

Modes (named in the report and on the last line): the default is `full`, the run a wave closes on. `--quick`
is for iterating: the same cases minus what the gateway makes slow - the four full-site traversals (sweep,
scan, audit text and JSON; about 87 s each on the Pi) run over QUICK_TRAVERSAL_PAGES instead of all 37
pages, the JSON twin of every section-tab case and of `device status` is dropped (the text case and the
per-page-id `page <id> --json` reads keep the coverage), and the pause between cases shrinks to
QUICK_DELAY_S except after the diagnostics commits and the speed test, which keep their longer pauses.
Measured 2026-10-08 on the Pi (read-only, --skip-commits): full 195 cases in 14.7 minutes, quick 161 cases
in 5.4 minutes, both clean. A quick run proves less (no full
traversal, no JSON view of the slow LAN pages); it never replaces the full run before a merge.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BGW = str(ROOT / ".venv" / "bin" / "bgwcli")

CASE_TIMEOUT_S = 600
DANGEROUS_PAGES = {"routerpasswd", "restart", "update", "reset"}

# The dosprotect commit cycle flips this select away from its live value and then restores it.
DOSPROTECT_FIELD = "icmp_downstream_echo_rqst_drop_wan"
DOSPROTECT_VALUES = ("on", "off")
LIVE: dict[str, str] = {}  # values captured from the gateway while the run progresses
# The temporary mutation and its undo are one guarded operation (see execute_cases).
TOGGLE_CASE = "set-commit-dosprotect-toggle"
RESTORE_CASES = ("set-commit-dosprotect-restore", "verify-dosprotect-restored")
ARTIFACT_PROBLEM = "artifact write failed"
EXIT_RESTORE_UNVERIFIED = 3
# --quick: the traversals walk these pages only (a form page, a status page, a documentary page), the
# pause between cases is this short, and the slow JSON twins below are dropped.
QUICK_TRAVERSAL_PAGES = "diag,sysinfo,dosprotect"
QUICK_DELAY_S = 0.2
TRAVERSAL_CASES = ("sweep-json", "scan-json", "audit-json", "audit-text")


@dataclass
class Case:
    name: str
    argv: list[str]
    rc: int = 0
    json: bool = False
    stderr_empty: bool = True
    check: object = None  # callable(stdout_text, parsed_json) -> str | None (problem)
    note: str = ""
    delay: float = 0.6
    # callable() -> argv | None, evaluated just before the case runs (None: precondition missing, case fails)
    argv_factory: object = None
    needs_commits: bool = False  # only meaningful after a committed write; dropped by --skip-commits
    stdout_empty: bool = False  # the case must print nothing on stdout (a negative answer has no body)
    stderr_has: tuple[str, ...] = ()  # text the case must print on stderr


@dataclass
class Result:
    case: Case
    rc: int
    out: str
    err: str
    seconds: float
    problems: list[str] = field(default_factory=list)


def nonempty_key(key, minimum=1):
    def check(_text, payload):
        value = payload.get(key) if isinstance(payload, dict) else None
        if not isinstance(value, (list, dict)) or len(value) < minimum:
            return f"expected non-empty '{key}' in JSON (got {type(value).__name__} len {len(value) if value else 0})"
        return None
    return check


def page_ok(_text, payload):
    if not isinstance(payload, dict):
        return "JSON payload is not an object"
    if payload.get("fallback") is True and payload.get("sections"):
        return None  # designed fallback (home/lanstatistics hang, securityoptions 404 on this firmware)
    if payload.get("ok") is False or payload.get("error"):
        return f"page reported failure: {payload.get('error')}"
    parsed_keys = ("values", "tables", "fields", "selects")
    if all(key in payload for key in parsed_keys) and not any(payload[key] for key in parsed_keys):
        return "page parsed to zero values/tables/fields/selects"
    return None


def contains(*needles):
    def check(text, _payload):
        missing = [n for n in needles if n not in text]
        return f"missing text {missing}" if missing else None
    return check


def dosprotect_value(payload):
    """Live value of the dosprotect select in a `page dosprotect --forms --json` payload, else None."""
    controls = [*payload.get("selects", []), *payload.get("fields", [])] if isinstance(payload, dict) else []
    return next((c.get("value") for c in controls if c.get("name") == DOSPROTECT_FIELD), None)


def capture_dosprotect(_text, payload):
    value = dosprotect_value(payload)
    if value not in DOSPROTECT_VALUES:
        return f"{DOSPROTECT_FIELD} live value {value!r} is not one of {DOSPROTECT_VALUES}"
    LIVE[DOSPROTECT_FIELD] = value
    return None


OPENER_KEY = "packetfilter-add-rule-button"
OPENER_BUTTONS = ("adddroprule", "addpassrule")  # Packet Filter's editor-opening buttons, normalised


def capture_packetfilter_opener(_text, payload):
    """Records the live name of a Packet Filter add-rule button for the opener-assignment refusal."""
    buttons = payload.get("buttons", []) if isinstance(payload, dict) else []
    names = [b.get("name") for b in buttons if isinstance(b, dict) and isinstance(b.get("name"), str)]
    found = next((n for n in names if re.sub(r"[^a-z0-9]", "", n.lower()) in OPENER_BUTTONS), None)
    if found is None:
        return f"no add-rule button among the packetfilter buttons {names}"
    LIVE[OPENER_KEY] = found
    return None


def packetfilter_opener_argv():
    """`submit packetfilter <add-rule button> x=1` (a dry run: the refusal comes after one GET), or None
    when the live button was not captured."""
    name = LIVE.get(OPENER_KEY)
    return None if name is None else ["submit", "packetfilter", name, "x=1"]


def dosprotect_target(restore: bool):
    """The live value (restore) or its opposite (toggle); None when the live value was not captured."""
    live = LIVE.get(DOSPROTECT_FIELD)
    if live is None:
        return None
    return live if restore else next(v for v in DOSPROTECT_VALUES if v != live)


def dosprotect_set_argv(restore: bool):
    def factory():
        target = dosprotect_target(restore)
        if target is None:
            return None
        return ["set", "dosprotect", f"{DOSPROTECT_FIELD}={target}", "--commit", "--confirm", "DOSPROTECT"]

    return factory


def dosprotect_is(restore: bool):
    def check(_text, payload):
        expected = dosprotect_target(restore)
        value = dosprotect_value(payload)
        return None if value == expected else f"{DOSPROTECT_FIELD} is {value!r}, expected {expected!r}"

    return check


def build_cases(out_dir: Path) -> list[Case]:
    """Every case; dump files and sweep artifacts go under `out_dir` (the --out directory)."""
    sys.path.insert(0, str(ROOT / "src"))
    dump = str(out_dir / "dump.json")
    dump_all = str(out_dir / "dump-all.json")
    dump_include_all = str(out_dir / "dump-include-all.json")
    from bgwcli.actions import ROUTER_ACTIONS
    from bgwcli.pages import ROUTER_TABS, list_sections

    cases: list[Case] = []
    # --- connectivity / local
    cases += [
        Case("check", ["check"], check=contains("Reachable")),
        Case("check-json", ["check", "--json"], json=True),
        Case("auth", ["auth"]),
        Case("tabs", ["tabs"]),
        Case("actions", ["actions"], check=contains("RESTART", "DIAG")),
        Case("actions-json", ["actions", "--json"], json=True),
        Case("session-status", ["session", "status"]),
        Case("session-status-json", ["session", "status", "--json"], json=True),
        Case("sitemap", ["sitemap"]),
        Case("sitemap-json", ["sitemap", "--json"], json=True),
        Case("coverage", ["coverage"]),
        Case("coverage-json", ["coverage", "--json"], json=True),
    ]
    for sec in list_sections():
        root = sec.lower().replace(" ", "-")
        cases.append(Case(f"section-{root}", ["section", sec]))
    # --- everyday reads
    cases += [
        Case("device-status", ["device", "status"]),
        Case("device-status-json", ["device", "status", "--json"], json=True),
        Case("devices", ["devices"]),
        Case("devices-all-json", ["devices", "--all", "--json"], json=True, check=nonempty_key("devices", 5)),
        Case("wifi", ["wifi"]),
        Case("wifi-forms-json", ["wifi", "--forms", "--json"], json=True),
        Case("nat", ["nat"]),
        Case("logs", ["logs", "--limit", "5"]),
        Case("logs-json", ["logs", "--json"], json=True, check=lambda t, p: None if isinstance(p, list) else "logs JSON must be an array (TS parity)"),
        Case("status", ["status"]),
        Case("status-json", ["status", "--json"], json=True),
    ]
    # --- every section tab, text + json (skip dangerous pages via section commands; read them via page below)
    for tab in ROUTER_TABS:
        if tab.dangerous:
            continue
        root = {"Device": "device", "Broadband": "broadband", "Home Network": "home-network", "Voice": "voice",
                "Firewall": "firewall", "Diagnostics": "diagnostics"}[tab.section]
        words = tab.label.lower().replace("&", "").replace("/", "-").split()
        slug = "-".join(words)
        cases.append(Case(f"tab-{root}-{slug}", [root, *words]))
        check = (lambda t, p: None if isinstance(p, list) else "logs JSON must be an array") if tab.page == "logs" else page_ok
        cases.append(Case(f"tab-{root}-{slug}-json", [root, *words, "--json"], json=True, check=check))
    # --- every mapped page by CGI id (GET only, incl. dangerous pages which are safe to read)
    for page in sorted({t.page for t in ROUTER_TABS}):
        cases.append(Case(f"page-{page}-json", ["page", page, "--json"], json=True,
                          check=(lambda t, p: None if isinstance(p, list) else "logs JSON must be an array") if page == "logs" else page_ok,
                          note="dangerous page, GET only" if page in DANGEROUS_PAGES else ""))
    cases += [
        Case("page-sysinfo-raw", ["page", "sysinfo", "--raw"], check=contains("<html")),
        Case("inspect-diag-json", ["inspect", "diag", "--json"], json=True, check=nonempty_key("buttons")),
        Case("inspect-ipalloc", ["inspect", "ipalloc"]),
        Case("page-unknown", ["page", "nosuchpagexyz"], rc=2, check=contains("Page unavailable")),
    ]
    # --- traversal
    cases += [
        Case("sweep-json", ["sweep", "--json"], json=True, delay=1.0),
        Case("scan-json", ["scan", "--json"], json=True, delay=1.0),
        Case("schema-json-2pages", ["schema", "--json", "--pages", "diag,sysinfo"], json=True),
        Case("audit-json", ["audit", "--json"], json=True, delay=1.0),
        Case("audit-text", ["audit"], delay=1.0, stderr_empty=False, note="progress lines go to stderr by design"),
        Case("sweep-out", ["sweep", "--pages", "sysinfo,diag", "--out", str(out_dir / "sweep-out")], stderr_empty=False),
    ]
    # --- backup (dry-run only here; restore commit cycle is a separate explicit test)
    cases += [
        Case("dump", ["dump", "--out", dump], check=contains("Dump written")),
        Case("dump-all-clients-json", ["dump", "--out", dump_all, "--all-clients", "--json"], json=True),
        Case("dump-include-all", ["dump", "--out", dump_include_all, "--include", "all"],
             check=contains("Dump written", "etherlan", "dhcpserver", "ippass", "wmacauth")),
        Case("dump-refuse-out-directory", ["dump", "--out", str(out_dir)], rc=2, stderr_empty=False, stdout_empty=True,
             stderr_has=("nothing was written",), note="refused before any request: --out is the run's own output directory"),
        Case("diff", ["diff", dump], check=contains("No differences")),
        Case("diff-json", ["diff", dump, "--json"], json=True),
        Case("diff-include-core", ["diff", dump, "--include", "dosprotect,wconfig"],
             check=contains("No differences")),
        Case("diff-include-all-dump", ["diff", dump_include_all], check=contains("No differences")),
        Case("restore-dry", ["restore", dump], check=contains("dry-run")),
        Case("restore-dry-include-missing", ["restore", dump, "--include", "dhcpserver"], rc=1,
             stderr_empty=False, stdout_empty=True, stderr_has=("nothing compared", "not in dump"),
             note="default dump is core-only: --include naming only absent pages compares nothing (exit 1, no stdout)"),
        Case("restore-dry-prune-json", ["restore", dump, "--prune", "--json"], json=True),
        Case("restore-refuse-commit", ["restore", dump, "--commit"], rc=1, stderr_empty=False,
             check=None),
    ]
    # --- mutations: dry-runs for every action; refusals; safe commits
    for action in ROUTER_ACTIONS:
        cases.append(Case(f"action-dry-{action.name}", ["action", action.name], check=contains("dry-run")))
    cases += [
        Case("action-refuse-wrong-token", ["action", "run-speed-test", "--commit", "--confirm", "WRONG"], rc=1, stderr_empty=False),
        Case("set-dry-dosprotect", ["set", "dosprotect", "icmp_downstream_echo_rqst_drop_wan=on"], check=contains("dry-run", "DOSPROTECT")),
        Case("set-dry-json", ["set", "dosprotect", "icmp_downstream_echo_rqst_drop_wan=on", "--json"], json=True),
        Case("set-refuse-dangerous", ["set", "restart", "x=y", "--commit", "--confirm", "RESTART"], rc=1, stderr_empty=False),
        Case("submit-dry-diag-ping", ["submit", "diag", "Ping", "WebAddress=8.8.8.8"], check=contains("dry-run")),
        Case("submit-dry-dangerous-labelled", ["submit", "restart", "Restart"], check=lambda t, p: None if "dry-run" in t and re.search(r"Dangerous\s+yes", t) else "dangerous page not labelled"),
        Case("submit-refuse-owning-form-broadband", ["submit", "home", "Broadband"], rc=1, stderr_empty=False, stdout_empty=True,
             stderr_has=("action restart-broadband",),
             note="dry run: the Broadband button belongs to the crestart.ha form; refused after the GET, before any POST"),
        Case("inspect-packetfilter-json", ["inspect", "packetfilter", "--json"], json=True, check=capture_packetfilter_opener,
             note="records the live add-rule button name for the next case"),
        Case("submit-refuse-opener-assignment", ["submit", "packetfilter", "<add-rule button>", "x=1"], rc=1,
             stderr_empty=False, stdout_empty=True, stderr_has=("only opens an editor",),
             argv_factory=packetfilter_opener_argv,
             note="dry run: refused after one GET, before any POST (an opener takes no assignments)"),
        Case("submit-refuse-dangerous-commit", ["submit", "restart", "Restart", "--commit", "--confirm", "RESTART"], rc=1, stderr_empty=False),
        Case("diag-ping-dry", ["diagnostics", "ping", "8.8.8.8"], check=contains("dry-run")),
        Case("diag-ping-commit", ["diagnostics", "ping", "8.8.8.8", "--commit", "--confirm", "DIAG"], check=contains("committed"), delay=2.0),
        Case("diag-nslookup-commit-json", ["diagnostics", "nslookup", "google.com", "--commit", "--confirm", "DIAG", "--json"], json=True, delay=2.0),
        Case("diag-traceroute-commit", ["diagnostics", "traceroute", "8.8.8.8", "--ipv4", "--commit", "--confirm", "DIAG"], check=contains("committed"), delay=2.0),
        Case("action-commit-speed-test", ["action", "run-speed-test", "--commit", "--confirm", "SPEED"], check=contains("committed"), delay=5.0),
        Case("capture-dosprotect-live", ["page", "dosprotect", "--forms", "--json"], json=True, check=capture_dosprotect,
             note="records the live value the commit cycle toggles and restores"),
        Case("set-commit-dosprotect-toggle", ["set", "dosprotect", f"{DOSPROTECT_FIELD}=<opposite of live>", "--commit", "--confirm", "DOSPROTECT"],
             argv_factory=dosprotect_set_argv(restore=False), check=lambda t, p: None if re.search(r"Verified\s+yes", t) else "set not verified"),
        Case("verify-dosprotect-toggled", ["page", "dosprotect", "--forms", "--json"], json=True, needs_commits=True,
             check=dosprotect_is(restore=False)),
        Case("set-commit-dosprotect-restore", ["set", "dosprotect", f"{DOSPROTECT_FIELD}=<live>", "--commit", "--confirm", "DOSPROTECT"],
             argv_factory=dosprotect_set_argv(restore=True), check=lambda t, p: None if re.search(r"Verified\s+yes", t) else "set not verified"),
        Case("verify-dosprotect-restored", ["page", "dosprotect", "--forms", "--json"], json=True, needs_commits=True,
             check=dosprotect_is(restore=True)),
        Case("diff-after-mutations", ["diff", dump], check=contains("No differences")),
    ]
    # --- error paths
    cases += [
        Case("strict-tls", ["check", "--strict-tls"], rc=2, stderr_empty=False, note="self-signed cert expected to fail"),
        Case("bad-host", ["check", "--host", "192.0.2.1", "--timeout", "1.5"], rc=2, stderr_empty=False),
    ]
    return cases


def skip_commit_cases(cases: list[Case]) -> list[Case]:
    """The cases `--skip-commits` keeps: no commits, plus the read-only `*refuse*` dry runs."""
    return [c for c in cases if not c.needs_commits and ("commit" not in c.name or "refuse" in c.name)]


def quick_cases(cases: list[Case]) -> list[Case]:
    """The `--quick` selection (see the module docstring): copies of the cases, so the full list is
    untouched. Dropped: the JSON twin of every `tab-*` case and `device-status-json`. Rewritten: the four
    traversals get `--pages QUICK_TRAVERSAL_PAGES` (one that already names pages keeps its own); every
    pause of a second or less becomes QUICK_DELAY_S, longer pauses (diagnostics commits, speed test) stay."""
    quick: list[Case] = []
    for case in cases:
        if (case.name.startswith("tab-") and case.name.endswith("-json")) or case.name == "device-status-json":
            continue
        argv = list(case.argv)
        if case.name in TRAVERSAL_CASES and "--pages" not in argv:
            argv += ["--pages", QUICK_TRAVERSAL_PAGES]
        quick.append(replace(case, argv=argv, delay=QUICK_DELAY_S if case.delay <= 1.0 else case.delay))
    return quick


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def write_private(path: Path, text: str) -> None:
    """Case output can carry router data: UTF-8, mode 0600 whatever the umask. The file is created
    fresh (O_EXCL) and never through a symlink (O_NOFOLLOW), so a planted link or a previous run's
    file is refused rather than followed or overwritten."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


def _write_artifacts(result: Result, out_dir: Path, slug: str) -> None:
    """Write the case's .out/.err; an OSError (disk full, permissions) is recorded as a problem."""
    for suffix, text in (("out", result.out), ("err", result.err)):
        try:
            write_private(out_dir / f"{slug}.{suffix}", text)
        except OSError as exc:
            result.problems.append(f"{ARTIFACT_PROBLEM}: {slug}.{suffix}: {exc}")


def output_directory(requested: str | None) -> Path:
    """A fresh private (0700) directory from tempfile.mkdtemp, or the requested one if it is new or
    empty; a directory holding a previous run is refused (exit 2) instead of mixed with it."""
    if requested is None:
        return Path(tempfile.mkdtemp(prefix="bgwcli-e2e-"))
    out_dir = Path(requested)
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if out_dir.is_symlink() or any(out_dir.iterdir()):
        print(f"--out {out_dir} must be a new or empty directory", file=sys.stderr)
        raise SystemExit(2)
    os.chmod(out_dir, 0o700)
    return out_dir


def run(case: Case, env: dict[str, str], out_dir: Path, index: int) -> Result:
    if case.argv_factory is not None:
        argv = case.argv_factory()
        if argv is None:
            return Result(case, -1, "", "", 0.0, ["not run: the live value it depends on was not captured"])
        case.argv = argv
    started = time.monotonic()
    slug = f"{index:03d}-{case.name}"
    try:
        proc = subprocess.run([BGW, *case.argv], env=env, capture_output=True, text=True, timeout=CASE_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        out, err = _text(exc.stdout), _text(exc.stderr)
        result = Result(case, -1, out, err, time.monotonic() - started, [f"timed out after {CASE_TIMEOUT_S}s"])
        _write_artifacts(result, out_dir, slug)
        return result
    seconds = time.monotonic() - started
    result = Result(case, proc.returncode, proc.stdout, proc.stderr, seconds)
    # An artifact that cannot be written is a harness error for this case; it must never raise out of
    # here, because the caller may owe the gateway a restore after this very case.
    _write_artifacts(result, out_dir, slug)
    if proc.returncode != case.rc:
        result.problems.append(f"exit {proc.returncode}, expected {case.rc}")
    if case.stderr_empty and proc.stderr.strip():
        result.problems.append(f"stderr not empty: {proc.stderr.strip().splitlines()[0][:160]}")
    if case.stdout_empty and proc.stdout.strip():
        result.problems.append(f"stdout not empty: {proc.stdout.strip().splitlines()[0][:160]}")
    missing = [needle for needle in case.stderr_has if needle not in proc.stderr]
    if missing:
        result.problems.append(f"stderr is missing {missing}")
    if "Traceback" in proc.stderr:
        result.problems.append("PYTHON TRACEBACK")
    payload = None
    if case.json:
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            result.problems.append(f"invalid JSON: {exc}")
    if case.check and all(p.startswith(ARTIFACT_PROBLEM) for p in result.problems):
        problem = case.check(proc.stdout, payload)
        if problem:
            result.problems.append(problem)
    return result


def _substantive(result: Result) -> list[str]:
    """Problems other than a failed artifact write (which says nothing about the gateway)."""
    return [p for p in result.problems if not p.startswith(ARTIFACT_PROBLEM)]


def _run_and_print(case: Case, env: dict[str, str], out_dir: Path, results: list[Result], total: int) -> Result:
    index = len(results) + 1
    result = run(case, env, out_dir, index)
    results.append(result)
    flag = "FAIL" if result.problems else "ok  "
    print(f"[{index:03d}/{total}] {flag} {case.name} ({result.seconds:.1f}s)" + (f" -> {result.problems[0]}" if result.problems else ""), flush=True)
    time.sleep(case.delay)
    return result


def execute_cases(
    cases: list[Case], all_cases: list[Case], env: dict[str, str], out_dir: Path
) -> tuple[list[Result], str | None, BaseException | None]:
    """Run `cases` in order. Once the dosprotect toggle has been attempted (with the original live value
    captured), its restore and the verification of it are guaranteed to run - in the loop when they are
    selected, otherwise (or when the loop is cut short by any exception, an interrupt included) from the
    `finally` path - and the returned note says how that went. The note is None when no toggle was
    attempted and starts with "dosprotect restore not verified" when the gateway may still hold the
    toggled value. An exception that cut the loop short is returned as the third value instead of being
    raised, so the caller can report the note before it propagates (or exits 3 over it). Not absolute: an
    exception raised inside the `finally` restore itself (a second interrupt while the restore cases run)
    propagates at once, unreported - restore by hand with
    `bgwcli set dosprotect icmp_downstream_echo_rqst_drop_wan=<original value> --commit --confirm DOSPROTECT`."""
    by_name = {c.name: c for c in all_cases}
    results: list[Result] = []
    toggled = False
    interrupted: BaseException | None = None
    try:
        for case in cases:
            if case.name == TOGGLE_CASE and LIVE.get(DOSPROTECT_FIELD) is not None:
                toggled = True  # before the run: a toggle that timed out may still have committed
            _run_and_print(case, env, out_dir, results, len(cases))
    except BaseException as exc:  # noqa: BLE001 - captured so main reports the restore, then re-raises it
        interrupted = exc
    finally:
        if toggled:
            for name in RESTORE_CASES:
                if name not in {r.case.name for r in results} and name in by_name:
                    _run_and_print(by_name[name], env, out_dir, results, len(cases))
    if not toggled:
        return results, None, interrupted
    original = LIVE.get(DOSPROTECT_FIELD)
    done = {r.case.name: r for r in results}
    missing = [n for n in RESTORE_CASES if n not in done]
    bad = [r for n in RESTORE_CASES if (r := done.get(n)) is not None and _substantive(r)]
    if missing or bad:
        detail = "; ".join(f"{r.case.name}: {_substantive(r)[0]}" for r in bad) or f"not run: {', '.join(missing)}"
        return results, (
            f"dosprotect restore not verified ({detail}); {DOSPROTECT_FIELD} may still be toggled on the gateway - "
            f"restore it by hand: bgwcli set dosprotect {DOSPROTECT_FIELD}={original} --commit --confirm DOSPROTECT"
        ), interrupted
    return results, f"dosprotect restore performed and verified ({DOSPROTECT_FIELD}={original})", interrupted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None)
    parser.add_argument("--out", default=None, help="new or empty output directory (default: a fresh temp dir)")
    parser.add_argument("--skip-commits", action="store_true")
    parser.add_argument("--quick", action="store_true",
                        help="iterating mode: traversals over a few pages, no JSON tab twins, short pauses (see the docstring)")
    args = parser.parse_args(argv)
    mode = "quick" if args.quick else "full"
    code = os.environ.get("E2E_ACCESS_CODE")
    if not code:
        print("E2E_ACCESS_CODE is required", file=sys.stderr)
        return 2
    if not os.access(BGW, os.X_OK):
        print(f"{BGW} not found: create the virtualenv and install bgwcli into it first", file=sys.stderr)
        return 2
    out_dir = output_directory(args.out)
    env = {**os.environ, "BGW_ACCESS_CODE": code}
    all_cases = build_cases(out_dir)
    cases = all_cases
    if args.only:
        cases = [c for c in cases if args.only in c.name]
    if args.skip_commits:
        cases = skip_commit_cases(cases)
    if args.quick:
        cases = quick_cases(cases)
        all_cases = quick_cases(all_cases)  # the guarded restore pair runs with the quick pacing too
    results, restore_note, interrupted = execute_cases(cases, all_cases, env, out_dir)
    failed = [r for r in results if r.problems]
    lines = ["# bgwcli e2e report", "", f"mode: {mode}", "", f"{len(results)} cases, {len(failed)} failed", ""]
    if restore_note:
        lines += [restore_note, ""]
    lines += ["| # | case | argv | exit | s | problems |", "|---|---|---|---|---|---|"]
    for index, r in enumerate(results, 1):
        lines.append(f"| {index:03d} | {r.case.name} | `{' '.join(r.case.argv)}` | {r.rc} | {r.seconds:.1f} | {'; '.join(r.problems) or 'ok'} |")
    report_failed = False
    try:
        write_private(out_dir / "report.md", "\n".join(lines) + "\n")
        write_private(out_dir / "report.json", json.dumps([{"case": r.case.name, "argv": r.case.argv, "rc": r.rc, "seconds": r.seconds, "problems": r.problems} for r in results], indent=1))
    except OSError as exc:
        report_failed = True
        print(f"{ARTIFACT_PROBLEM}: report in {out_dir}: {exc}", file=sys.stderr)
    print(f"\n{len(results)} cases, {len(failed)} failed, mode: {mode} -> {out_dir}/report.md")
    if restore_note:
        print(restore_note, file=sys.stderr if restore_note.startswith("dosprotect restore not verified") else sys.stdout)
    if restore_note and restore_note.startswith("dosprotect restore not verified"):
        return EXIT_RESTORE_UNVERIFIED  # the unverified restore outranks an interrupt or error
    if interrupted is not None:
        raise interrupted
    return 1 if failed or report_failed else 0


if __name__ == "__main__":
    sys.exit(main())
