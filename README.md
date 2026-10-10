# bgwcli

Python CLI for the AT&T BGW320 gateway at `192.168.1.254`: read every page of its web UI from the
terminal, back up and restore its configuration, and bring it back automatically after a factory reset.
An idiomatic rewrite of the TypeScript/Bun [BGW320-CLI](https://github.com/TheSethRose/BGW320-CLI) (`bgw`)
with compatible environment variables, session cache files, dump-file format, confirmation tokens and
command surface. Python 3.10 or newer, standard library only.

This README is the short guide: install, the everyday commands, backup and recovery, and the safety
rules. Every detail of how a command behaves on every answer the gateway can give (exit codes, write
evidence, session cache, redaction, autorestore internals) lives in [docs/REFERENCE.md](docs/REFERENCE.md).

## Install

`bgwcli` runs the same on macOS and Linux, x86_64 or arm64, including a Raspberry Pi. The installer
installs [uv](https://docs.astral.sh/uv/) if needed and gives `bgwcli` its own isolated environment on
the system Python when it is 3.10 or newer (otherwise uv fetches one; `BGWCLI_PYTHON=3.12` picks a version).

**Install or upgrade (uv tool), any Mac or Linux box.** Install and upgrade are the same command; run it again whenever you want the current `main`:

```bash
curl -fsSL https://raw.githubusercontent.com/vpushkar/bgwcli/main/install.sh | sh
```

It prints the PATH line to add if `~/.local/bin` is not on your PATH yet.

**Install or upgrade as a single-file executable instead** (built on this machine with [PyInstaller](https://pyinstaller.org/) under a Python already installed here, no uv). Again one command for both; it rebuilds from the current `main` and replaces the file:

```bash
curl -fsSL https://raw.githubusercontent.com/vpushkar/bgwcli/main/install.sh | sh -s binary
```

**Uninstall** either flavour (lists the per-user state it leaves behind, deletes none of it):

```bash
curl -fsSL https://raw.githubusercontent.com/vpushkar/bgwcli/main/install.sh | sh -s uninstall
```

From a clone the same three are `sh install.sh`, `sh install.sh binary` and `sh install.sh uninstall`.

**If you already have uv or pipx:**

```bash
UV_PYTHON_PREFERENCE=system uv tool install git+https://github.com/vpushkar/bgwcli   # add --python 3.12 to pick a Python
pipx install git+https://github.com/vpushkar/bgwcli
pip install git+https://github.com/vpushkar/bgwcli                     # into the current environment
```

Upgrade with `uv tool upgrade bgwcli` / `pipx upgrade bgwcli`; pin with `git+https://github.com/vpushkar/bgwcli@<tag-or-commit>` (the installer takes the same form in `BGWCLI_REPO`).

**From a clone (for hacking on it):**

```bash
git clone https://github.com/vpushkar/bgwcli && cd bgwcli
uv venv .venv && uv pip install -e '.[dev]' --python .venv/bin/python   # or: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/bgwcli --help
.venv/bin/pytest -q -n auto    # the tests never touch a gateway
.venv/bin/ruff check
```

Requirements: Python 3.10 or newer, nothing else at runtime. Installing from a git URL means the installer and the `git+https://...` commands also need `git` on your PATH. How the executable is built, what uninstall touches, and the test-suite guarantees: [reference](docs/REFERENCE.md#install-details).

## Quick start

```bash
bgwcli check                      # is the gateway reachable?
bgwcli tabs                       # the router tab map
bgwcli broadband fiber-status
bgwcli firewall nat-gaming
bgwcli diagnostics logs --limit 50
bgwcli audit                      # walk every page, summarize what answered
```

Authenticated pages need the device access code. Export it once per shell and every command picks it up:

```bash
export BGW_ACCESS_CODE='<access-code>'
bgwcli auth
bgwcli wifi
bgwcli devices
bgwcli home-network advanced-wi-fi --json
```

Scripts that must not export the code pipe it per command instead:

```bash
printf '%s' "$CODE" | bgwcli dump --out ~/bgw-baseline.json --access-code-stdin
```

Every command accepts `--json`; global options go before or after the command name. `bgwcli <command> --help` prints one command's usage, `bgwcli --help` the whole reference.

## Commands

| Command | Purpose |
| --- | --- |
| `check` / `auth` | Reachability without logging in / a fresh login to verify the access code. |
| `tabs`, `section <section>`, `coverage`, `sitemap` | The CLI's tab map, one section of it, and the live sitemap compared with it. |
| `page <page-or-tab>`, `inspect <page-or-tab>` | Fetch and parse any mapped tab or raw CGI page id; `inspect` adds form fields and selects. |
| `status`, `devices [--all]`, `wifi`, `nat`, `logs [--limit N]` | The core status pages, the device list, Wi-Fi configuration (secrets redacted), the NAT table, the router log. |
| `sweep`, `scan`, `schema`, `audit` / `readiness` | Walk the mapped pages in one session and keep going through hangs; `audit` summarizes failed/fallback/empty/useful pages. |
| `device` / `broadband` / `home-network` / `voice` / `firewall` / `diagnostics` `<tab>` | Section commands, one per router tab (see [Router command tree](#router-command-tree)). |
| `dump`, `diff`, `restore`, `autorestore` | Backup, drift detection, restore and unattended factory-reset recovery (below). |
| `set <page> KEY=VALUE...`, `submit <page> <button> KEY=VALUE...`, `action <name>`, `actions` | Writes: dry-run by default, `--commit --confirm TOKEN` to post (see [Writing to the router](#writing-to-the-router)). |
| `session status` / `session clear-cache` | The local session cache shared by bursts of commands. |

Full per-command behaviour, including what each one does on a hung, truncated, login-bounced or
pool-full answer: [reference, Commands](docs/REFERENCE.md#commands).

## Backup, drift and restore

`dump` writes the configuration the gateway loses on a factory reset to an owner-only JSON file:
custom services, NAT/Gaming forwards, fixed IP reservations and the Firewall Advanced and Advanced Wi-Fi
pages; `--include all` adds LAN ports, Subnets & DHCP, IP Passthrough and the Wi-Fi MAC-filtering modes.
`diff` compares that file with the live router. `restore` puts back what is missing, dry-run by default.
Everything below was run against the live gateway (firmware 6.35.8).

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

The file is mode 0600 and contains Wi-Fi keys in clear text; treat it like a credential. The JSON is
plain and hand-editable and is validated on load.

**2. Detect drift**

```
$ bgwcli diff ~/bgw-baseline.json
+ extra service zz_test TCP 61000-61000 -> 61000
+ extra forward zz_test -> host-a (02:0a:0b:0c:0d:01)
$ echo $?
1
```

Exit 0 means identical, 1 means the router differs, 2 means the command could not answer.

**3. Restore: dry-run, then commit**

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

A step is `applied` only when the gateway acknowledged the save (`Changes saved`) **and** the requested
state read back. Every write is sent once; the run stops at the first rejected or unconfirmed write and
ends with a diff, and the exit code is 0 only when everything in the dump is on the router.

**4. Remove what the dump does not have (`--prune`)**

```
$ bgwcli restore ~/bgw-baseline.json --prune --commit --confirm RESTORE
[1] applied apphosting (302 -> /cgi-bin/apphosting.ha)   # forward removed first
[2] applied services (302 -> /cgi-bin/services.ha)       # then the service
[3] skipped packetfilter
restore committed
No differences.
```

Prune removes router-only services and forwards, one real removal per page per run, and never releases
a host reservation. Repeat the prune run until the closing diff prints `No differences.`

**5. Cadence.** Re-dump after any change you make in the web UI, and after a firmware upgrade
(`diff` against the old file first).

**Choosing what to back up.** Four form pages are opt-in because restoring them can cut you off:

| Page id | Router page | Why it is opt-in |
| --- | --- | --- |
| `etherlan` | LAN Ethernet ports | Forcing a port mode can cut the wire you are connected through. |
| `dhcpserver` | Subnets & DHCP | A different LAN address moves the gateway; restored last, with a `warning:` line. |
| `ippass` | IP Passthrough mode | Changes how the WAN address is handed to a LAN device. |
| `wmacauth` | Wi-Fi MAC Filtering modes | A `deny`/`allow` mode restored before the filter list exists locks Wi-Fi clients out; blocked until the list is back. |

```bash
bgwcli dump --out ~/bgw-baseline.json                           # core only
bgwcli dump --include dhcpserver,ippass --out ~/bgw-baseline.json
bgwcli dump --include all --out ~/bgw-baseline.json             # every page: the factory-reset baseline
bgwcli diff ~/bgw-baseline.json --include services,apphosting    # narrow diff/restore the same way
```

The whole contract (dump-file schema, how reservations are restored, the allocation-conflict preflight,
how `No changes detected` and lost acknowledgements are decided, what `--prune` will never remove):
[reference, Backup contract](docs/REFERENCE.md#backup-contract).

## Factory-reset recovery

The scenario the dump exists for: a firmware update or support call factory-resets the gateway. The reset
destroys custom services, forwards, fixed reservations, the firewall flags, the whole Advanced Wi-Fi page
(SSID, password, bands), the MAC-filtering modes, IP Passthrough and Subnets & DHCP; `restore` puts all of
that back from a `--include all` dump. It does not cover the access code (back to the sticker value),
packet-filter rules and the MAC filter list (recorded under `tables`, re-enter by hand), Public Subnet,
Remote Access or Voice.

```
export BGW_ACCESS_CODE='<sticker code>'          # a reset restores the printed access code
bgwcli check && bgwcli auth
bgwcli diff ~/bgw-baseline.json                  # everything missing is listed; exit 1
bgwcli restore ~/bgw-baseline.json               # read the plan, especially blocked/warning lines
bgwcli restore ~/bgw-baseline.json --commit --confirm RESTORE
```

The first commit restores services, forwards and reservations for devices the router already lists
(wired ones), the firewall flags and Advanced Wi-Fi, which brings the SSID and password back so Wi-Fi
clients reconnect. Wait a few minutes and run the same commit again for their forwards and reservations.
Repeat until the closing diff prints `No differences.` Run it from a wired client; if the dump carries a
different LAN address, the Subnets & DHCP step (restored last) moves the gateway and you reconnect to the
new address. Take a fresh dump afterwards as the new baseline.

## Automatic recovery (autorestore)

`bgwcli autorestore <dumpfile>` is that runbook as a watchdog: run it from a systemd timer on an
always-on LAN host (a Raspberry Pi) and the configuration comes back by itself within minutes of a reset.
It acts only on total loss (every dumped service, forward **and** reservation missing, or an unfinished
recovery of the same dump); ordinary drift is reported as `no-reset` and left alone. It never prunes,
never reboots the gateway, never changes the access code, and never re-posts a write the gateway
acknowledged within one run.

```
$ bgwcli autorestore ~/bgw-baseline.json                                   # dry-run: plan, exit 1 on a reset
$ bgwcli autorestore ~/bgw-baseline.json --commit --confirm RESTORE --wait 60
factory reset detected: access code reverted; factory reset suspected; services 2/2 missing; forwards 2/2 missing; reservations 2/2 missing
pass 1/3: 9 steps
[1] applied services (302)
...
pass 1/3: 7 applied, 2 blocked, 0 failed, 0 not run
not yet converged; waiting 60s before pass 2
pass 2/3: 3 steps
...
converged after pass 2
No configuration differences.
```

Set `BGW_FALLBACK_ACCESS_CODE` to the sticker code so the watchdog can log in after the reset. Deploy it
as a user unit with [deploy/README.md](deploy/README.md), and re-dump after every deliberate change so the
baseline is always the state you want back. Reset detection, the pass and run limits, the recovery
checkpoint and every status and exit code: [reference, Automatic recovery](docs/REFERENCE.md#automatic-recovery-autorestore).

## Router command tree

Every router tab is a command. All accept `--json`; `--forms` adds the page's form controls to the terminal view.

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

`home`, `lan` (for `home-network`) and `diag`, `diagnostic` (for `diagnostics`) are accepted as section
aliases. Pages on this router can hang: a page command reports a structured page-unavailable result (exit 2),
and `sweep`, `scan`, `schema` and `audit` keep going through per-page failures. `inspect <page> --forms`
shows what the CLI discovered on a page that changed.

## Writing to the router

Read commands only send GET requests plus the login POST. Every write is dry-run by default and needs
`--commit --confirm TOKEN`; the token is derived from the target page (`WCONFIG-UNIFIED` for Wi-Fi,
`RESTORE` for restore, `DIAG` for diagnostics) and the dry run prints it:

```bash
bgwcli set wconfig maxclients=80                                  # dry run: plan + token
bgwcli set wconfig maxclients=80 --commit --confirm WCONFIG
bgwcli submit "Diagnostics/Troubleshoot" Ping WebAddress=example.com --commit --confirm DIAG
bgwcli actions                                                    # every guarded action and its token
bgwcli action run-speed-test --commit --confirm SPEED
bgwcli action restart-wifi-2.4 --commit --confirm RESTART-WIFI --timeout 45
```

Generic `set` and `submit` refuse the dangerous pages (`routerpasswd`, `restart`, `reset`, `update`); those
go through named `action`s, which are marked dangerous. A write is sent once; `applied` means the gateway
acknowledged it and the requested state read back, an explicit rejection is exit 1, and an answer the CLI
could not read is exit 2 with `committed` in the JSON telling you whether the write reached the router.
Secrets (passwords, keys, access codes, nonces, phone numbers) are redacted on every output path unless
you pass `--include-secrets`. The complete rules: [reference, Safety](docs/REFERENCE.md#safety).

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success: `diff` identical, `restore --commit` converged, `autorestore` no reset or converged, a save acknowledged and read back as requested. |
| `1` | Negative answer: `diff` differs, restore not converged, usage error, confirmation refused, the gateway rejected a write, or a save read back differently. |
| `2` | No answer: connection or authentication failure, a full session pool, a page that could not be read, or a write whose acknowledgement was never seen (check `committed` before retrying). |
| `130` | Interrupted (Ctrl-C); with `--json` the object carries the write evidence. |

The exact mapping per command and per answer: [reference, Exit codes](docs/REFERENCE.md#exit-codes).

## Environment variables

Identical to the TypeScript CLI, so one shell setup serves both:

| Variable | Meaning | Default |
| --- | --- | --- |
| `BGW_HOST` / `ROUTER_IP` | Router host (`--host`) | `192.168.1.254` |
| `BGW_ACCESS_CODE` | Device access code (`--access-code-stdin` is the alternative) | unset |
| `BGW_FALLBACK_ACCESS_CODE` | `autorestore` only: the sticker code a reset reverts to | unset |
| `BGW_TIMEOUT_MS` | Request timeout in milliseconds (`--timeout` takes seconds) | `15000` |
| `BGW_INSECURE_TLS` | `0` enforces TLS validation (`--strict-tls`) | accept self-signed |
| `BGW_WAIT_FOR_SESSION` | `1` waits when the web session pool is full | off |
| `BGW_SESSION_CACHE_DIR` / `XDG_CACHE_HOME` | Session cache location | `~/.cache/bgw` |
| `BGW_DUMP_DIR` / `XDG_STATE_HOME` | Default `dump` output directory | `~/.local/state/bgw/dumps` |

Timeouts, the session-wait and cache variables, and the session cache rules: [reference, Environment Variables](docs/REFERENCE.md#environment-variables).

## Privacy note

All MAC addresses, device labels and SSIDs in the test fixtures and in these examples are synthetic
(`02:0a:0b:0c:0d:xx` MACs, `host-a`/`host-b`/`watch` labels, `EXAMPLE-NET` SSID). `bgwcli dump` output
contains Wi-Fi keys and access codes in clear text: treat dump files as credentials and never commit them.
`.gitignore` excludes `bgw-*.json`, `*.dump.json`, `dumps/` and `router-dumps/`. The router presents a
self-signed certificate, which the CLI accepts by default; run it only from a trusted local network. The
client never uses an HTTP(S) proxy, so the access code goes only to the router.

## Credits and license

`bgwcli` is an independent Python implementation whose command surface, dump-file format, session-cache
layout and safety rules were designed to match [BGW320-CLI](https://github.com/TheSethRose/BGW320-CLI)
by Seth Rose, the TypeScript/Bun tool that served as the behavioral reference. Thanks to that project
for mapping the gateway's pages, forms and quirks in the first place. The deliberate differences from
`bgw` are listed in the [reference](docs/REFERENCE.md#differences-from-bgw).

This repository is licensed under the [MIT License](LICENSE). The upstream BGW320-CLI project carries
its own terms; consult that repository for them.
