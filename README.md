# bgwcli

Python CLI for the AT&T BGW320 gateway at `192.168.1.254`. Idiomatic rewrite of the TypeScript/Bun
[BGW320-CLI](https://github.com/TheSethRose/BGW320-CLI) (`bgw`) with the same observable behavior: environment variables, session
cache files, dump-file format, exit codes, confirmation tokens, `--json` shapes and command surface are
identical, so both CLIs can be used interchangeably against one gateway. Runtime dependencies: Python
standard library only (3.10+).

## Install

`bgwcli` is pure Python (standard library only) and runs the same on macOS and Linux, x86_64 or arm64,
including a Raspberry Pi. The installer below does not depend on the system Python: it installs
[uv](https://docs.astral.sh/uv/) if needed and gives `bgwcli` its own managed interpreter.

**One line, any Mac or Linux box:**

```bash
curl -fsSL https://raw.githubusercontent.com/vpushkar/bgwcli/main/install.sh | sh
```

Re-run the same line to upgrade. It prints the PATH line to add if `~/.local/bin` is not on your PATH yet.

**If you already have uv or pipx:**

```bash
uv tool install --python 3.12 git+https://github.com/vpushkar/bgwcli   # uv fetches Python 3.12 if the system one is older
pipx install git+https://github.com/vpushkar/bgwcli                    # uses the system Python (3.10+)
pip install git+https://github.com/vpushkar/bgwcli                     # into the current environment
```

Upgrade with `uv tool upgrade bgwcli` / `pipx upgrade bgwcli`; pin with `git+https://github.com/vpushkar/bgwcli@<tag-or-commit>`.

**From a clone (for hacking on it):**

```bash
git clone https://github.com/vpushkar/bgwcli && cd bgwcli
uv venv .venv && uv pip install -e '.[dev]' --python .venv/bin/python   # or: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/bgwcli --help
.venv/bin/pytest -q
```

Requirements: Python 3.10 or newer, nothing else at runtime. Dev extras: pytest, ruff.

## Quality Checks

```bash
.venv/bin/pytest
.venv/bin/ruff check
```

Tests never touch the gateway: parser tests use inline HTML, CLI tests inject a fake client, and the
router fixture pack tests skip when `tests/fixtures/router-html` is absent.

## Usage

```bash
bgwcli check
bgwcli coverage
bgwcli sweep --pages diag,dhcpserver --json
bgwcli audit
bgwcli tabs
bgwcli section Diagnostics
bgwcli broadband fiber-status
bgwcli home-network wi-fi --json
bgwcli home-network advanced-wi-fi --json
bgwcli firewall nat-gaming
bgwcli diagnostics logs --limit 50
```

Authenticated pages require the device access code. Export it once per shell session and every command picks it up:

```bash
export BGW_ACCESS_CODE='<access-code>'
bgwcli auth
bgwcli wifi
```

Alternative for scripts that must not export the code: pipe it per command with `--access-code-stdin`.

```bash
printf '%s' "$CODE" | bgwcli auth --access-code-stdin
printf '%s' "$CODE" | bgwcli dump --out ~/bgw-baseline.json --access-code-stdin
```

Global options are accepted before or after the command name (`bgwcli --json check` and
`bgwcli check --json` are equivalent). `bgwcli <command> --help` prints argparse usage for one command;
`bgwcli --help` prints the full command reference.

## Commands

| Command | Purpose |
| --- | --- |
| `check` | Verify the router is reachable. |
| `auth` | Verify the access code and session login flow. |
| `tabs` | Print the CLI's router tab map. |
| `actions` | List guarded router actions and confirmation tokens. |
| `action <name>` | Dry-run a guarded router action. Requires `--commit --confirm TOKEN` to POST. |
| `section <section>` | Print mapped tabs for one router section. |
| `coverage` | Compare the CLI tab map against the live router sitemap. |
| `sweep` | Shared traversal command for mapped router pages. Default output is compact status/count metadata. |
| `scan` | Compatibility alias for compact sweep metadata. |
| `schema` | Sweep with parsed/form detail enabled. |
| `audit` / `readiness` | Sweep-backed health check that keeps going through hangs and summarizes failed/fallback/empty/useful pages. |
| `sitemap` | Print the live router sitemap. |
| `page <page-or-tab>` | Fetch and parse any mapped tab or raw CGI page ID. |
| `inspect <page-or-tab>` | Fetch a page and include parsed form fields/selects. |
| `status` | Fetch the core status pages. |
| `devices` | List current and remembered devices; status comes from the gateway and may be stale. |
| `wifi` | Fetch Wi-Fi configuration with secrets redacted by default. |
| `nat` | Fetch NAT table details. |
| `logs` | Fetch router logs. |
| `session status` / `session clear-cache` | Inspect or clear the local session coordination state. |
| `set <page> KEY=VALUE...` | Build a dry-run mutation plan. Requires `--commit --confirm TOKEN` to POST. |
| `submit <page> <button> KEY=VALUE...` | Build a dry-run form/button submission. Requires `--commit --confirm TOKEN` to POST. |
| `device` / `broadband` / `home-network` / `voice` / `firewall` / `diagnostics` `<tab>` | Section commands; see the Router Command Tree below. |

### Backup

| Command | What it does |
| --- | --- |
| `bgwcli dump [--out <file>] [--include <csv\|all>] [--all-clients]` | Captures custom services, NAT/Gaming forwards (device label resolved to MAC), host reservations (Fixed Allocation rows) and the core form pages Firewall Advanced (`dosprotect`) and Advanced Wi-Fi (`wconfig`) to an owner-only JSON file. `--include` adds the optional form pages LAN ports (`etherlan`), Subnets & DHCP (`dhcpserver`), IP Passthrough (`ippass`) and Wi-Fi MAC Filtering modes (`wmacauth`); `--include all` captures every page. Pages not included are not fetched. Packet-filter rules and the MAC filter list are recorded as documentary tables only. |
| `bgwcli diff <dumpfile> [--include <csv\|all>]` | Read-only comparison of the dump with the live router: every section and every form page present in the dump. `--include` restricts the comparison to the listed page ids (`services`, `apphosting`, `ipalloc` and the form page ids); a requested page the dump never captured prints a warning on stderr and is skipped. Exit 0 when identical, 1 when different, 2 on error. |
| `bgwcli restore <dumpfile> [--prune] [--include <csv\|all>] [--commit --confirm RESTORE]` | Dry-run by default: prints the ordered plan (services → forwards → reservations → firewall advanced → Advanced Wi-Fi → Wi-Fi MAC filtering → IP Passthrough → LAN ports → Subnets & DHCP). Only adds what is missing and only saves forms whose values differ; optional pages are restored only when the dump captured them, and `--include` restricts the plan like `diff` (a requested page missing from the dump becomes a `skip` step). `--prune` also removes router rows not in the dump, but a page that still has an addition to make has its removes deferred, so adds and prunes can need two runs. `--prune` never releases a reservation back to DHCP — extra reservations are reported by `diff` only, releasing one stays a manual UI action. Live runs need both `--commit` and `--confirm RESTORE`; the run stops at the first rejected POST and ends with a diff. A step marked `applied` means the router accepted the POST — the convergence diff printed at the end is the authoritative success signal. Packet-filter rules are captured as text only and never restored. |

Dump files are schema 2 JSON, byte-compatible with the TypeScript CLI: a dump written by either tool
diffs clean and restores with the other. Schema-1 dumps are refused on load.

A reservation is restored through the same two-step flow the router UI uses: `Allocate` opens an "IP Allocation Entry" block for that device, then a second POST selects the target address and saves it. Only addresses the router currently reports as free can be reserved — an address already held by another device is not offered, the step fails, and the run stops there. After a restore, the gateway keeps that "IP Allocation Entry" block rendered for the rest of the web session even though nothing further is pending; this is harmless sticky session state, not an unsaved change.

Because a reservation address ends up in that second POST verbatim, `restore` refuses to release one to DHCP by accident: a dump file whose reservation entries are not a MAC plus a dotted IPv4 address is rejected on load, and the executor refuses to post any allocation value that is not a dotted IPv4 address (the router's entry form always offers `normal`, "Address from DHCP pool"). If the gateway is hiding the per-row `Allocate` buttons because an entry block is still open, reserve steps are reported blocked with "not present on IP Allocation page" — close the gateway web session or wait for it to expire, then re-run.

Advanced Wi-Fi saves go through the gateway's "Wi-Fi Warning" confirmation page automatically: `restore` follows the redirect and posts its `Continue` button, so a `wconfig` step marked `applied` means the change was confirmed, not just submitted. `bgwcli` never releases a reservation, and releasing one for a device that is currently offline may not take effect in the gateway until that device reconnects.

Both ambiguous and unknown device labels make `dump` fail on purpose, and the error says what to do. Ambiguous (two devices both called "watch"): rename one of them in the gateway first. Unknown (the forwarded device is not in the gateway's device list at all, e.g. it is offline): reconnect the device or delete that forward in the gateway UI, then re-run `dump`. A dump must never contain a forward whose device could not be resolved to a MAC — `restore --prune` would read it as both missing and extra and delete the live forward the dump was meant to preserve.

Because `Remove_<n>` buttons address table positions rather than stable ids, `restore --prune` defers the removes on any page that still has an addition to make, and performs at most one real remove per page per run. A dump that both adds and prunes therefore converges over two commands: `bgwcli restore <dumpfile> --commit --confirm RESTORE`, then `bgwcli restore <dumpfile> --prune --commit --confirm RESTORE`.

### Backup walkthrough

Everything below was run against the live gateway on 2026-09-20 (firmware 6.35.8). Export the access code once for the shell session; every command picks it up from the environment (`--access-code-stdin` remains available for scripts that must not export it):

```
export BGW_ACCESS_CODE='your-access-code'
```

Alternative when you do not want the code in the environment: keep it in a shell variable or a secret store and pipe it per command with `--access-code-stdin`. Every example below works the same way with that suffix:

```
printf '%s' "$CODE" | bgwcli dump --out ~/bgw-baseline.json --access-code-stdin
```

**1. Take a baseline and verify it matches the router**

```
$ bgwcli dump --include all --out ~/bgw-baseline.json
Dump written: /Users/you/bgw-baseline.json
Firmware: 6.35.8
Services: 4
Forwards: 4
Reservations: 4
Forms: dosprotect, wconfig, etherlan, dhcpserver, ippass, wmacauth
Tables: packetfilter, ipalloc, etherlan, wmacauth

$ bgwcli diff ~/bgw-baseline.json
No differences.
```

The file is mode 0600 and contains Wi-Fi keys in clear text; treat it like a credential. Without `--include`, only the core pages (`dosprotect`, `wconfig`) are captured — see "Choosing what to back up and restore" below. Keep `--all-clients` off unless you want DHCP-only devices recorded in the documentary IP Allocation table.

**2. What is in the file**

```
$ python3 -c "import json; d=json.load(open('$HOME/bgw-baseline.json')); print(d['meta']); print([s['name'] for s in d['services']]); print(d['reservations'])"
{'schema': 2, 'firmware': '6.35.8', 'ts': '2026-09-20T07:03:32.601Z', 'routerHost': '192.168.1.254'}
['custom_ssh', 'Wireguard', 'Wireguard2', 'Mosh']
[{'mac': '02:0a:0b:0c:0d:01', 'ip': '192.168.1.65'}, ...]
```

`services`, `forwards`, `reservations` and `forms` are what `diff`/`restore` act on; `tables` is documentary. The JSON is plain and hand-editable, and it is validated on load (schema 2, well-formed entries, IPv4-only reservation addresses).

**3. Detect drift**

```
$ bgwcli diff ~/bgw-baseline.json
+ extra service zz_test TCP 61000-61000 -> 61000
+ extra forward zz_test -> host-a (02:0a:0b:0c:0d:01)
$ echo $?
1
```

Exit 0 means identical, 1 means the router differs, 2 means the command could not answer (auth, connection, bad file). `--json` prints the same diff as structured data. A firmware change is reported as a note and is not a difference by itself.

**4. Restore: dry-run, then commit**

```
$ bgwcli restore ~/bgw-baseline.json
[1] services add-service: add service zz_test TCP 61000-61000 -> 61000
    payload: {"Service":"zz_test","extMinPort":"61000","extMaxPort":"61000","intStartPort":"61000","protocol":"tcp","Add":"Add"}
[2] apphosting add-forward: add forward zz_test -> host-a (02:0a:0b:0c:0d:01)
    deferred: service 'zz_test' is added earlier in this run; the NAT/Gaming dropdown is re-read after that add
[3] packetfilter skip: packet filter rules are documentary only in v1; 79 rule rows in dump
dry-run: no router restore was sent

$ bgwcli restore ~/bgw-baseline.json --commit --confirm RESTORE
[1] applied services (302 -> /cgi-bin/services.ha)
[2] applied apphosting (302 -> /cgi-bin/apphosting.ha)
[3] skipped packetfilter
restore committed
Result
2 applied, 0 failed, 0 not run
No differences.
```

Without `--commit` nothing is sent. `applied` only means the router accepted the POST; the diff printed at the end is the authoritative success signal, and the exit code is 0 only when everything in the dump is present on the router.

**5. Remove things the dump does not have (`--prune`)**

```
$ bgwcli restore ~/bgw-baseline.json --prune --commit --confirm RESTORE
[1] applied apphosting (302 -> /cgi-bin/apphosting.ha)   # forward removed first
[2] applied services (302 -> /cgi-bin/services.ha)       # then the service
[3] skipped packetfilter
restore committed
No differences.
```

Prune removes router-only services and forwards, forwards before services (the gateway refuses to delete a service that still has a forward). It never releases host reservations. Only one real removal per page happens per run, and removals on a page that still has an add pending wait for the next run, so add-and-prune dumps may need two runs; the closing diff tells you when you are done.

**6. Recommended cadence**

- Re-dump after any change you make in the web UI, so the baseline is the state you want to keep.
- Re-dump after a firmware upgrade and `diff` against the old file before trusting `restore`.
- Old schema-1 dumps (before 2026-09-19) are rejected on load with a re-dump message.

### Factory-reset recovery

The scenario the dump exists for: a firmware update or support call factory-resets the gateway.

What the reset destroys and `restore` puts back: custom services, NAT/Gaming forwards, fixed IP
reservations, Firewall Advanced flags, the whole Advanced Wi-Fi page (SSID, password, bands, max
clients), Wi-Fi MAC-filtering modes, IP Passthrough mode, and the Subnets & DHCP page (LAN address,
mask, DHCP range and lease). What it does not cover: the device access code (back to the sticker
value), packet-filter rules and the MAC filter list (both recorded in the dump under `tables`, re-enter
them by hand), Public Subnet, Remote Access, Voice.

The baseline for this scenario is `bgwcli dump --include all`: only a dump that captured the optional
pages can put Subnets & DHCP, IP Passthrough, MAC-filtering modes and the LAN ports back (a core-only
dump restores services, forwards, reservations, Firewall Advanced and Advanced Wi-Fi).

```
export BGW_ACCESS_CODE='<sticker code>'          # a reset restores the printed access code
bgwcli check && bgwcli auth
bgwcli diff ~/bgw-baseline.json                  # everything missing is listed; exit 1
bgwcli restore ~/bgw-baseline.json               # read the plan, especially blocked/warning lines
bgwcli restore ~/bgw-baseline.json --commit --confirm RESTORE
```

The first commit restores services, forwards and reservations for devices the router already lists
(wired ones), the firewall flags and Advanced Wi-Fi, which brings your SSID and password back so Wi-Fi
clients start reconnecting. Wait a few minutes and run the same commit again: the Wi-Fi devices are now
in the router's list, so their forwards and reservations apply. Repeat until the closing diff prints
`No differences.`

Subnets & DHCP is restored last, on purpose. If your dump carries a LAN address different from the
router's current one, the plan shows a `warning:` line on that step: saving it moves the gateway, the
closing diff cannot re-fetch it, and you reconnect to the new address before running `diff` again.
Run the recovery from a wired client. Afterwards take a fresh dump as the new baseline, since the
firmware changed.

### Choosing what to back up and restore

`dump` always captures the sections `diff`/`restore` act on (custom services, NAT/Gaming forwards, fixed
reservations) plus the two **core** form pages, Firewall Advanced (`dosprotect`) and Advanced Wi-Fi
(`wconfig`). Four form pages are **optional** and only captured when named with `--include`:

| Page id | Router page | Why it is opt-in |
| --- | --- | --- |
| `etherlan` | LAN Ethernet ports (speed/duplex, MDI-X per port) | Forcing a port mode can cut the wire you are connected through. Its write path has not been exercised live; run a first commit from a client that is not on the port being changed. |
| `dhcpserver` | Subnets & DHCP (LAN address, mask, DHCP range, lease) | A different LAN address moves the gateway; the closing diff cannot re-fetch it. Still restored **last**, and the step carries a `warning:` line with the new address. |
| `ippass` | IP Passthrough mode | Changes how the WAN address is handed to a LAN device. |
| `wmacauth` | Wi-Fi MAC Filtering modes (allow/deny/none per network) | A `deny`/`allow` mode restored before the filter list exists can lock Wi-Fi clients out. The list itself is documentary. |

```
bgwcli dump --out ~/bgw-baseline.json                           # core only
bgwcli dump --include dhcpserver,ippass --out ~/bgw-baseline.json
bgwcli dump --include all --out ~/bgw-baseline.json             # every page: the factory-reset baseline
```

Pages not included are not fetched, and a page the dump did not capture is never compared or written:
`diff` and `restore` act on everything present in the dump. `--include` on `diff`/`restore` narrows that to
the listed page ids — `services`, `apphosting` (forwards), `ipalloc` (reservations) and the form page ids
(`all` or no flag = everything in the dump):

```
bgwcli diff ~/bgw-baseline.json --include services,apphosting
bgwcli restore ~/bgw-baseline.json --include dhcpserver          # dry-run of that one page
```

A requested page the dump never captured is reported, not guessed at: `diff` and `restore` print
`warning: page '<x>' is not present in the dump; nothing to compare/restore` on stderr, the restore plan
shows a `skip` step for it, and `--json` output lists it under `missingPages`. An unknown page id is a
usage error (exit 1) before the router is touched. Exit codes are unchanged: `diff` 0/1/2, `restore` 0 once
everything selected from the dump is present on the router.

## Router Command Tree

All of these commands accept `--json`. Parsed page JSON includes a `summary` object with the same high-value fields used by the terminal view, plus the underlying values, tables, controls, buttons, and forms. Use `--forms` to include form controls in normal terminal output.

```bash
bgwcli device status
bgwcli device device-list
bgwcli device system-information
bgwcli device access-code
bgwcli device remote-access
bgwcli device restart-device

bgwcli broadband status
bgwcli broadband configure
bgwcli broadband fiber-status

bgwcli home-network status
bgwcli home-network configure
bgwcli home-network ipv6
bgwcli home-network wi-fi
bgwcli home-network advanced-wi-fi
bgwcli home-network mac-filtering
bgwcli home-network subnets-dhcp
bgwcli home-network ip-allocation

bgwcli voice status
bgwcli voice line-details
bgwcli voice call-statistics

bgwcli firewall status
bgwcli firewall custom-services
bgwcli firewall packet-filter
bgwcli firewall nat-gaming
bgwcli firewall public-subnet-hosts
bgwcli firewall ip-passthrough
bgwcli firewall firewall-advanced
bgwcli firewall security-options

bgwcli diagnostics troubleshoot
bgwcli diagnostics ping example.com
bgwcli diagnostics ping example.com --commit --confirm DIAG
bgwcli diagnostics traceroute example.com --commit --confirm DIAG
bgwcli diagnostics nslookup example.com --commit --confirm DIAG
bgwcli diagnostics speed-test
bgwcli diagnostics logs
bgwcli diagnostics update
bgwcli diagnostics resets
bgwcli diagnostics syslog
bgwcli diagnostics event-notifications
bgwcli diagnostics nat-table
```

`home`, `lan` (for `home-network`) and `diag`, `diagnostic` (for `diagnostics`) are accepted as section aliases, as in the TypeScript CLI.

## Generic Inspection And Forms

Use `inspect` or `--forms` when the router page changed and you need to see what the CLI discovered:

```bash
bgwcli inspect "Diagnostics/Troubleshoot" --forms
bgwcli home-network wi-fi --forms
```

Readable page output shows available buttons and control counts by default. JSON includes parsed values, duplicate-preserving `valueEntries`, tables, fields, select option labels/state, textareas, buttons, forms, links, and disabled/readonly metadata. Use `--forms` to show those preserved details in terminal output.

Several configuration pages have page-specific summaries built from current form state so default terminal output stays useful:

- `broadband configure`: source override and MTU values.
- `device access-code`, `device remote-access`, `device restart-device`: current form/action state with sensitive values redacted and restart surfaced as an explicit guarded action.
- `home-network configure`, `home-network ipv6`, `home-network wi-fi`, `home-network advanced-wi-fi`, `home-network mac-filtering`: compact current network form state, including per-port configured Ethernet modes, basic current channel rows, advanced radio/SSID controls, and per-radio MAC-filter state. Passwords, SSIDs embedded as defaults, and WPS PINs stay redacted in fixtures.
- `home-network subnets-dhcp`: gateway/subnet, DHCP range, lease, public subnet, inbound, and cascaded-router state.
- `firewall packet-filter`, `firewall custom-services`, `firewall nat-gaming`, `firewall public-subnet-hosts`: current firewall/NAT form state; NAT/Gaming shows selected service/device and available service/device counts instead of dumping every dropdown option.
- `firewall ip-passthrough`: allocation mode, default server, passthrough mode/MAC, and lease.
- `firewall firewall-advanced`: ICMP, Reflexive ACL, ESP ALG, and SIP ALG toggles.
- `diagnostics update`, `diagnostics resets`, `diagnostics event-notifications`: current action/form state without requiring browser clicks.

Pages on this router can hang. Normal parsed page commands report a structured page-unavailable result (exit 2) instead of taking down broader workflows. `scan`, `schema`, and `audit` continue across failures so one broken AT&T page does not hide the rest of the router.

## Sweep

`sweep` is the shared traversal spine used by `scan`, `schema`, `audit`/`readiness`, and fixture capture. It uses one client/session, walks mapped pages in router-tab order, keeps going through per-page failures, and reports compact counts by default.

```bash
bgwcli sweep
bgwcli sweep --json
bgwcli sweep --pages diag,wconfig_unified,dhcpserver --json
bgwcli sweep --include-parsed --json
bgwcli sweep --forms --json
bgwcli sweep --out router-dumps/latest
```

Default sweep output does not dump raw HTML or full parsed payloads. It reports counts for unique values, duplicate-preserving value entries, tables, controls, forms, and links. Use:

- `--include-parsed` for parsed values, value entries, tables, fields, select option metadata, textareas, buttons, forms, links, fallback sections, and device-list fallback data in JSON.
- `--forms` for detailed controls, buttons, forms, and submit targets.
- `--pages <csv>` to limit traversal.
- `--raw --pages <single-page>` to emit one raw HTML page.
- `--out <dir>` to write raw HTML and parsed JSON artifacts to disk while keeping stdout compact.

`scan` is retained as the compatibility command for compact sweep metadata. `schema` is sweep with parsed/form detail. `audit` and `readiness` are sweep plus the health/usefulness summary.

`device status` first tries `home.ha`. If that page hangs, it falls back to a concise summary from System Information, Broadband Status, and Firewall Status. Use the section-specific commands for deeper output such as `broadband fiber-status` or `home-network status`.

`devices` first tries `devices.ha`. If that page hangs, it falls back to `ipalloc.ha` and returns degraded device records with IP, name, MAC, status, and allocation. Treat `last activity` as a timestamp reported by the gateway, not proof of a disconnect.

`home-network status` first tries `lanstatistics.ha`. If that page hangs, it falls back to LAN configure, Subnets & DHCP, IP Allocation, and Wi-Fi configuration data.

`firewall security-options` first tries `securityoptions.ha`. On firmware that advertises that page but returns Page not found, it falls back to Firewall Status and Firewall Advanced.

The router can also refuse login when its tiny web session pool is full. By default the CLI fails fast with a clear error (exit 2) so normal commands do not appear hung. To wait only for that exact condition:

```bash
bgwcli sweep --wait-for-session
bgwcli sweep --wait-for-session --session-wait-timeout 120000 --session-wait-interval 10000
```

Waiting does not retry bad access codes, random connection failures, or parser failures. An active five-minute local pool cooldown still fails fast with `--wait-for-session`; do not clear it simply to force another authentication attempt. `bgwcli session clear-cache` acquires the same per-router lock and is intended only for explicit local-state troubleshooting.

## Timeouts

The default request timeout is 15 s (`--timeout <ms>` / `BGW_TIMEOUT_MS`). Two pages on this gateway legitimately take longer (measured 2026-09-20: home.ha 17–18 s, lanstatistics.ha 23–29 s), so the client applies a 45 s floor to them (`SLOW_PAGE_TIMEOUT_MS`) when the timeout is the implicit default. An explicit `--timeout` or `BGW_TIMEOUT_MS` is always honored exactly, floors included. Everything else keeps the 15 s default so a dead page still fails fast in sweeps.

## Environment Variables

Identical to the TypeScript CLI, so one shell setup serves both:

| Variable | Meaning | Default |
| --- | --- | --- |
| `BGW_HOST` / `ROUTER_IP` | Router host (`--host`) | `192.168.1.254` |
| `BGW_ACCESS_CODE` | Device access code; the usual way to authenticate (`--access-code-stdin` is the alternative for scripts) | unset |
| `BGW_TIMEOUT_MS` | Request timeout (`--timeout`) | `15000` |
| `BGW_INSECURE_TLS` | `0` enforces TLS validation (same as `--strict-tls`) | accept self-signed |
| `BGW_WAIT_FOR_SESSION` | `1` waits when the web session pool is full (`--wait-for-session`) | off |
| `BGW_SESSION_WAIT_TIMEOUT_MS` | Session wait timeout (`--session-wait-timeout`) | `120000` |
| `BGW_SESSION_WAIT_INTERVAL_MS` | Session wait poll interval (`--session-wait-interval`) | `10000` |
| `BGW_SESSION_CACHE_TTL_MS` | Local router session cache lifetime | `120000` |
| `BGW_SESSION_POOL_COOLDOWN_MS` | Local fail-fast cooldown after a pool-full response | `300000` |
| `BGW_SESSION_LOCK_TIMEOUT_MS` | Per-router lock wait | `300000` |
| `BGW_SESSION_CACHE_DIR` / `XDG_CACHE_HOME` | Session cache location (`~/.cache/bgw` by default) | |
| `BGW_DUMP_DIR` / `XDG_STATE_HOME` | Default `dump` output directory (`~/.local/state/bgw/dumps`) | |

Numeric variables follow JavaScript `Number()` for the documented inputs: decimal, fractional (`1500.5` is kept as a float), exponent notation and surrounding whitespace are accepted; empty falls back to the default; NaN/Infinity/non-numeric/below-minimum values are rejected with the same message as `bgw`. Hex (`0x10`) is not accepted.

Agent bursts reuse a short-lived local router session cache under a per-host lock to avoid filling the web session pool; the cache files (`~/.cache/bgw/<hash>.session.json|.cooldown.json|.lock`) have the same shape as the TypeScript CLI's, so both tools share one router web session.

## Router Fixture Pack

Parser ground truth belongs under:

```text
tests/fixtures/router-html/<page>.html
tests/fixtures/parsed/<page>.json
tests/fixtures/expected/<page>.json
```

Generated fixture files are gitignored on purpose. They are sanitized, but they can still reveal local topology, device names, firmware behavior, and configuration shape. Keep them local unless you have manually reviewed them.

Capture sanitized fixtures from the real router with the hidden `fixtures-capture` command (the port of `bun run fixtures:capture`):

```bash
bgwcli fixtures-capture [--out tests/fixtures] [--pages diag,logs] [--delay 750]
```

The capture is sweep-backed, serialized through the same per-router session coordinator, paced at no less than 750 ms between pages, and read-only at the configuration level: it performs GET requests plus the login POST required by the router. It redacts access-code-adjacent fields, Wi-Fi identifiers, device identifiers, hashes, MAC addresses, and IP addresses before writing owner-only fixture files, and refuses to write a page whose sanitized output still looks sensitive.

## Diagnostics

After a committed ping/traceroute/nslookup the gateway answers 302 with an empty body and fills its progress window asynchronously. `bgwcli` polls `diag` once a second for up to 15 s until the output is non-empty and stable, then prints it as the result. On timeout it prints the fallback line and suggests `bgwcli page diag`.

Diagnostic network actions dry-run by default and require `--commit --confirm DIAG` to send the router form:

```bash
bgwcli diagnostics ping example.com
bgwcli diagnostics ping example.com --commit --confirm DIAG
bgwcli diagnostics traceroute example.com --commit --confirm DIAG
bgwcli diagnostics nslookup example.com --commit --confirm DIAG
```

Use `--ipv4` or `--ipv6` to set the router protocol preference.

## Safety

Read commands only send GET requests plus the login POST required for authenticated pages.

`set` defaults to dry-run. Every actual POST requires `--commit --confirm TOKEN`; the token is derived from the target CGI page, such as `WCONFIG-UNIFIED` for Wi-Fi.

`action` defaults to dry-run. Actual action POSTs require `--commit --confirm TOKEN`; run `actions` to see tokens.

`submit` defaults to dry-run. It fetches the page, discovers the requested button, builds the router POST payload from the current form state plus your overrides, and prints the confirmation token:

```bash
bgwcli submit "Diagnostics/Troubleshoot" Ping WebAddress=example.com
bgwcli submit "Diagnostics/Troubleshoot" Ping WebAddress=example.com --commit --confirm DIAG
```

Generic `submit` is blocked on dangerous pages such as restart/reset/update/access-code. Use an explicit supported `action` for those.

Dry-run JSON for `action`, `set`, `submit`, and diagnostic commands uses the same operation shape:

```json
{
  "operation": "action",
  "dryRun": true,
  "committed": false,
  "page": "speed",
  "guarded": true,
  "dangerous": false,
  "confirmation": "SPEED",
  "commitCommand": "action run-speed-test --commit --confirm SPEED",
  "payload": { "run": "Run Speed Test" }
}
```

The generic `set` command refuses mutation attempts against dangerous pages:

- `routerpasswd`
- `restart`
- `reset`
- `update`

Supported explicit actions are guarded separately. Current action commands:

```bash
bgwcli action restart
bgwcli action clear-device-list
bgwcli action run-speed-test
bgwcli action run-full-diagnostics
bgwcli action send-diagnostics
bgwcli action diagnostics-ethernet-details
bgwcli action diagnostics-authentication-details
bgwcli action diagnostics-ip-details
bgwcli action diagnostics-dns-details
bgwcli action packet-filter-enable
bgwcli action packet-filter-add-drop-rule
bgwcli action packet-filter-add-pass-rule
bgwcli action reset-ip
bgwcli action reset-connection
bgwcli action restart-from-resets
bgwcli action reset-wifi-config
bgwcli action reset-firewall-config
bgwcli action factory-reset
```

Every `action` command is dry-run by default. `actions` prints the confirmation token required to commit each one. Reset/restart/factory-reset actions are marked dangerous and should be treated as destructive router operations.

Sensitive values are redacted by default. Use `--include-secrets` only when intentionally inspecting local output. Debug/schema output still redacts secrets by default.

`restore --commit` is a multi-POST operation behind a single `--confirm RESTORE`; review the dry-run plan first.

The router normally presents a self-signed certificate, so the CLI currently accepts it by default. This encrypts traffic without proving router identity; use `--strict-tls` only after installing or pinning a certificate that the runtime can validate, and otherwise run the CLI only from a trusted local network.

## Privacy note

All MAC addresses, device labels and SSIDs in the test fixtures and in the README examples are synthetic (locally administered `02:0a:0b:0c:0d:xx` MACs, `host-a`/`host-b`/`watch` labels, `EXAMPLE-NET` SSID); they do not describe any real network. `bgwcli dump` output contains Wi-Fi keys and access codes in clear text: treat dump files as credentials and never commit them. `.gitignore` excludes `bgw-*.json`, `*.dump.json` and `dumps/`.

## Exit Codes

| Code | Meaning |
| --- | --- |
| `0` | Success (`diff`: identical; `restore --commit`: converged). |
| `1` | Negative answer: `diff` found differences, `restore --commit` did not converge, usage errors, and confirmation refusals. |
| `2` | Could not answer: authentication/connection failures, session pool full, unreadable dump file, snapshot extraction failure, or a page that could not be fetched. |

## Differences from bgw

`bgwcli` is a behavior-for-behavior port of the TypeScript `bgw` CLI; the deliberate exceptions are:

- Unknown `--flags` are rejected (`unrecognized arguments: --bogus`, exit 1). `bgw` silently treats unknown flags as positional arguments (`bgw tabs --bogus` prints the tabs and exits 0). Strict rejection catches typos such as `--jsno` before a mutation runs.
- `--flag=value` and unambiguous option prefixes (`--jso`) are additionally accepted by the argument parser.
- Help text mentions `bgwcli` where `bgw` prints `bgw` (e.g. `Run bgwcli help.`).
- Hex numeric environment values (`BGW_TIMEOUT_MS=0x10`) are rejected instead of parsed as 16.

## Router Tab Coverage

The local registry contains the 36 observed sitemap pages plus the linked read-only `wconfig` Advanced Wi-Fi endpoint, for 37 mapped pages total. `coverage` reports sitemap differences explicitly, so the linked endpoint can appear under "Not in live sitemap" without being treated as missing.

If a router page hangs or changes, use:

```bash
bgwcli audit
bgwcli scan --json
bgwcli inspect "Home Network/Wi-Fi" --forms
```

## Live verification

2026-09-20, gateway firmware 6.35.8: `bgwcli dump` diffed clean against a `bgw` dump in both directions. A dump with a throwaway `zz_test` service (TCP 61000) and a `zz_test -> host-a` forward restored in one `bgwcli restore --commit` run (service applied, deferred forward applied after the NAT/Gaming dropdown re-read, convergence diff clean); the TypeScript `bgw diff` agreed with the resulting state; `bgwcli restore <baseline> --prune --commit` removed forward then service in one run; final diffs from both CLIs reported no differences.

By default `bgwcli dump` keeps only Fixed Allocation rows in the documentary `tables.ipalloc` block, so the dump carries no trace of DHCP-only devices; `--all-clients` keeps every client row. The `reservations` list that `diff`/`restore` act on is always fixed-rows-only regardless of the flag. Same behavior in the TypeScript `bgw`.

## Radio and broadband restarts

home.ha carries per-radio Restart buttons whose forms post to their own CGI scripts (`wrestart.ha?1`, `wrestart.ha?2`, `crestart.ha?1`) with the nonce served on home.ha. They are exposed as named actions:

```
bgwcli action restart-wifi-2.4 --commit --confirm RESTART-WIFI        # aliases restart-wifi-24, restart-2.4ghz
bgwcli action restart-wifi-5   --commit --confirm RESTART-WIFI
bgwcli action restart-broadband --commit --confirm RESTART-BROADBAND  # dangerous: drops WAN
```

Clients on the restarted band drop for a few seconds; run it from a wired host or a client on the other band. Actions with a `post_path` go through `client.post_form(nonce_page, post_path, fields)`; everything else still posts to `<page>.ha`.

Two gateway facts shape `post_form` (proven live 2026-09-20 with `restart-wifi-2.4`): home.ha is a public page, so fetching it never triggers the auto-login — `post_form` authenticates first when no session exists and re-logins + retries once if the POST answers with the Login page; and each form on home.ha carries its own nonce(s) (the Restart forms carry two, in nested markup), so the nonces of the form whose action matches the target path are posted, never the page's first nonce.

home.ha often takes longer than the default 15 s to answer; pass `--timeout 45000` with these actions:

```
bgwcli action restart-wifi-2.4 --commit --confirm RESTART-WIFI --timeout 45000
```

Live status of the restart actions (2026-09-20, firmware 6.35.8): `restart-wifi-2.4` proven live with the Python `bgwcli` (302 -> home.ha, radio back to Enabled within seconds); `restart-wifi-5` and `restart-broadband` post to the sibling forms and are unit-tested only; the TypeScript `bgw` port is unit-tested only.

To see which channels the radios are on, read the channel table from the Wi-Fi page:

```
bgwcli wifi            # text: channel table per radio
bgwcli wifi --json     # tables: Radio / Current Channel / Channel Width / Mode
```

## 5 GHz channel scan

Advanced Wi-Fi has no 5 GHz channel selector on this firmware (the radio is Automatic); its "Find Best Channel" button (`chanscan5`) sits inside the main wconfig form. It is exposed as a form-button action that posts the live form's base payload plus the button (never the button alone) and follows the Wi-Fi Warning page:

```
bgwcli action find-best-channel-5 --commit --confirm CHANSCAN --timeout 45000   # aliases chanscan5, find-best-channel
```

5 GHz clients drop briefly while the radio re-tunes; run it from a wired host or a 2.4 GHz client. Dry-run without `--commit`. Not yet run live. The generic `submit` and `set` commits share the same Wi-Fi Warning handling.

## Credits and license

`bgwcli` is an independent Python implementation whose command surface, dump-file format, session-cache
layout and safety rules were designed to match [BGW320-CLI](https://github.com/TheSethRose/BGW320-CLI)
by Seth Rose, the TypeScript/Bun tool that served as the behavioral reference. Thanks to that project
for mapping the gateway's pages, forms and quirks in the first place. The test fixtures here use
synthetic device identifiers.

This repository is licensed under the [MIT License](LICENSE). The upstream BGW320-CLI project carries
its own terms; consult that repository for them.
