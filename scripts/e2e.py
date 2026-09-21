#!/usr/bin/env python3
"""End-to-end validation of bgwcli against a live gateway (non-destructive commands only).

Usage: E2E_ACCESS_CODE=... .venv/bin/python scripts/e2e.py [--only substring] [--out /tmp/e2e]
Writes <out>/report.md, <out>/report.json and one <out>/<nn>-<slug>.{out,err} per case.
Never runs restart/reset/update/access-code/clear-device-list actions or dangerous submits.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BGW = str(ROOT / ".venv" / "bin" / "bgwcli")

DANGEROUS_PAGES = {"routerpasswd", "restart", "update", "reset"}
DESTRUCTIVE_ACTIONS = {
    "restart", "reset-ip", "reset-connection", "restart-from-resets", "reset-wifi-config",
    "reset-firewall-config", "factory-reset", "clear-device-list", "send-diagnostics",
    "packet-filter-enable", "packet-filter-add-drop-rule", "packet-filter-add-pass-rule",
}


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
    summary = payload.get("summary") or {}
    counts = [v for v in summary.values() if isinstance(v, int)] if isinstance(summary, dict) else []
    if summary and counts and sum(counts) == 0:
        return "page parsed to zero values/tables/fields"
    return None


def contains(*needles):
    def check(text, _payload):
        missing = [n for n in needles if n not in text]
        return f"missing text {missing}" if missing else None
    return check


def build_cases() -> list[Case]:
    sys.path.insert(0, str(ROOT / "src"))
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
        Case("page-unknown", ["page", "no-such-page-xyz"], rc=2, check=contains("Page unavailable")),
    ]
    # --- traversal
    cases += [
        Case("sweep-json", ["sweep", "--json"], json=True, delay=1.0),
        Case("scan-json", ["scan", "--json"], json=True, delay=1.0),
        Case("schema-json-2pages", ["schema", "--json", "--pages", "diag,sysinfo"], json=True),
        Case("audit-json", ["audit", "--json"], json=True, delay=1.0),
        Case("audit-text", ["audit"], delay=1.0, stderr_empty=False, note="progress lines go to stderr by design"),
        Case("sweep-out", ["sweep", "--pages", "sysinfo,diag", "--out", "/tmp/e2e/sweep-out"], stderr_empty=False),
    ]
    # --- backup (dry-run only here; restore commit cycle is a separate explicit test)
    cases += [
        Case("dump", ["dump", "--out", "/tmp/e2e/dump.json"], check=contains("Dump written")),
        Case("dump-all-clients-json", ["dump", "--out", "/tmp/e2e/dump-all.json", "--all-clients", "--json"], json=True),
        Case("dump-include-all", ["dump", "--out", "/tmp/e2e/dump-include-all.json", "--include", "all"],
             check=contains("Dump written", "etherlan", "dhcpserver", "ippass", "wmacauth")),
        Case("diff", ["diff", "/tmp/e2e/dump.json"], check=contains("No differences")),
        Case("diff-json", ["diff", "/tmp/e2e/dump.json", "--json"], json=True),
        Case("diff-include-core", ["diff", "/tmp/e2e/dump.json", "--include", "dosprotect,wconfig"],
             check=contains("No differences")),
        Case("diff-include-all-dump", ["diff", "/tmp/e2e/dump-include-all.json"], check=contains("No differences")),
        Case("restore-dry", ["restore", "/tmp/e2e/dump.json"], check=contains("dry-run")),
        Case("restore-dry-include-missing", ["restore", "/tmp/e2e/dump.json", "--include", "dhcpserver"],
             stderr_empty=False, check=contains("dry-run", "not present in the dump"),
             note="default dump is core-only: expect the missing-page warning on stderr and exit 0"),
        Case("restore-dry-prune-json", ["restore", "/tmp/e2e/dump.json", "--prune", "--json"], json=True),
        Case("restore-refuse-commit", ["restore", "/tmp/e2e/dump.json", "--commit"], rc=1, stderr_empty=False,
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
        Case("submit-refuse-dangerous-commit", ["submit", "restart", "Restart", "--commit", "--confirm", "RESTART"], rc=1, stderr_empty=False),
        Case("diag-ping-dry", ["diagnostics", "ping", "8.8.8.8"], check=contains("dry-run")),
        Case("diag-ping-commit", ["diagnostics", "ping", "8.8.8.8", "--commit", "--confirm", "DIAG"], check=contains("committed"), delay=2.0),
        Case("diag-nslookup-commit-json", ["diagnostics", "nslookup", "google.com", "--commit", "--confirm", "DIAG", "--json"], json=True, delay=2.0),
        Case("diag-traceroute-commit", ["diagnostics", "traceroute", "8.8.8.8", "--ipv4", "--commit", "--confirm", "DIAG"], check=contains("committed"), delay=2.0),
        Case("action-commit-speed-test", ["action", "run-speed-test", "--commit", "--confirm", "SPEED"], check=contains("committed"), delay=5.0),
        Case("set-commit-dosprotect-on", ["set", "dosprotect", "icmp_downstream_echo_rqst_drop_wan=on", "--commit", "--confirm", "DOSPROTECT"], check=lambda t, p: None if re.search(r"Verified\s+yes", t) else "set not verified"),
        Case("verify-dosprotect-on", ["page", "dosprotect", "--forms", "--json"], json=True,
             check=lambda t, p: None if any(s.get("name") == "icmp_downstream_echo_rqst_drop_wan" and s.get("value") == "on" for s in p.get("selects", [])) else "field not on after commit"),
        Case("set-commit-dosprotect-off", ["set", "dosprotect", "icmp_downstream_echo_rqst_drop_wan=off", "--commit", "--confirm", "DOSPROTECT"], check=lambda t, p: None if re.search(r"Verified\s+yes", t) else "set not verified"),
        Case("diff-after-mutations", ["diff", "/tmp/e2e/dump.json"], check=contains("No differences")),
    ]
    # --- error paths
    cases += [
        Case("strict-tls", ["check", "--strict-tls"], rc=2, stderr_empty=False, note="self-signed cert expected to fail"),
        Case("bad-host", ["check", "--host", "192.0.2.1", "--timeout", "1500"], rc=2, stderr_empty=False),
    ]
    return cases


def run(case: Case, env: dict[str, str], out_dir: Path, index: int) -> Result:
    started = time.monotonic()
    proc = subprocess.run([BGW, *case.argv], env=env, capture_output=True, text=True, timeout=600)
    seconds = time.monotonic() - started
    result = Result(case, proc.returncode, proc.stdout, proc.stderr, seconds)
    slug = f"{index:03d}-{case.name}"
    (out_dir / f"{slug}.out").write_text(proc.stdout)
    (out_dir / f"{slug}.err").write_text(proc.stderr)
    if proc.returncode != case.rc:
        result.problems.append(f"exit {proc.returncode}, expected {case.rc}")
    if case.stderr_empty and proc.stderr.strip():
        result.problems.append(f"stderr not empty: {proc.stderr.strip().splitlines()[0][:160]}")
    if "Traceback" in proc.stderr:
        result.problems.append("PYTHON TRACEBACK")
    payload = None
    if case.json:
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            result.problems.append(f"invalid JSON: {exc}")
    if case.check and not result.problems:
        problem = case.check(proc.stdout, payload)
        if problem:
            result.problems.append(problem)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None)
    parser.add_argument("--out", default="/tmp/e2e")
    parser.add_argument("--skip-commits", action="store_true")
    args = parser.parse_args()
    code = os.environ.get("E2E_ACCESS_CODE")
    if not code:
        print("E2E_ACCESS_CODE is required", file=sys.stderr)
        return 2
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "BGW_ACCESS_CODE": code}
    cases = build_cases()
    if args.only:
        cases = [c for c in cases if args.only in c.name]
    if args.skip_commits:
        cases = [c for c in cases if "commit" not in c.name or "refuse" in c.name]
    results: list[Result] = []
    for index, case in enumerate(cases, 1):
        result = run(case, env, out_dir, index)
        results.append(result)
        flag = "FAIL" if result.problems else "ok  "
        print(f"[{index:03d}/{len(cases)}] {flag} {case.name} ({result.seconds:.1f}s)" + (f" -> {result.problems[0]}" if result.problems else ""), flush=True)
        time.sleep(case.delay)
    failed = [r for r in results if r.problems]
    lines = ["# bgwcli e2e report", "", f"{len(results)} cases, {len(failed)} failed", ""]
    lines += ["| # | case | argv | exit | s | problems |", "|---|---|---|---|---|---|"]
    for index, r in enumerate(results, 1):
        lines.append(f"| {index:03d} | {r.case.name} | `{' '.join(r.case.argv)}` | {r.rc} | {r.seconds:.1f} | {'; '.join(r.problems) or 'ok'} |")
    (out_dir / "report.md").write_text("\n".join(lines) + "\n")
    (out_dir / "report.json").write_text(json.dumps([{"case": r.case.name, "argv": r.case.argv, "rc": r.rc, "seconds": r.seconds, "problems": r.problems} for r in results], indent=1))
    print(f"\n{len(results)} cases, {len(failed)} failed -> {out_dir}/report.md")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
