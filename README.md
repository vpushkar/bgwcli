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
Check what is installed with `bgwcli --version` (for example `bgwcli 0.2.0`).

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
bgwcli device system-information   # model, serial numbers, firmware (Software Version)
```

Scripts that must not export the code pipe it per command instead:

```bash
printf '%s' "$CODE" | bgwcli dump --out ~/bgw-baseline.json --access-code-stdin
```

Or keep the host, the code and your usual flags in a [configuration file](#configuration-file) so nothing has to be exported at all.

Every command accepts `--json`; global options go before or after the command name. `bgwcli <command> --help` prints one command's usage, `bgwcli --help` the whole reference.

## Configuration file

`~/.config/bgw/config` holds the same `BGW_*` variables as the environment, one `KEY='value'` per line. It is read before every command (except `help`) and fills in whatever the environment does not already set:

```bash
mkdir -p ~/.config/bgw && touch ~/.config/bgw/config && chmod 600 ~/.config/bgw/config
cat > ~/.config/bgw/config <<'EOF'
# gateway
BGW_HOST='192.168.1.254'
BGW_ACCESS_CODE='<access-code>'
BGW_TIMEOUT='30'
# standing preferences for flags
BGW_LIMIT='50'
BGW_DUMP_DIR='/home/<user>/bgw320'
EOF
bgwcli auth
bgwcli wifi
```

What to know:

- **Precedence.** A flag on the command line wins over an environment variable, which wins over the file, which wins over the built-in default. An `export BGW_HOST=...` in the shell therefore still overrides the file.
- **Keys.** Every variable in the two tables under [Environment variables](#environment-variables) plus `ROUTER_IP`. A key the CLI does not know, or a malformed line, is a usage error naming the file and line, so a typo never falls through to a default.
- **Format.** The `EnvironmentFile=` format systemd and `sh` (`set -a; . file`) read: quote values in single quotes, `#` starts a comment, a later line for the same key wins. One file can serve the CLI, the autorestore unit and a shell.
- **The access code.** `BGW_ACCESS_CODE` and `BGW_FALLBACK_ACCESS_CODE` are taken from the file only when it is a regular file owned by you with mode `0600` (or stricter). Otherwise those two keys are skipped with one warning naming the `chmod 600` to run, and every other setting still applies.
- **Another file.** `bgwcli --config <file> ...` or `BGW_CONFIG=<file>` names a different file; a named file must exist, the default location is optional.

The full contract, including validation messages and how an unreadable file is reported: [reference, Configuration file](docs/REFERENCE.md#configuration-file).

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
state read back. Each write is sent once, except that a Login-page answer to a configuration POST is followed by
one re-send after logging in again; the run stops at the first rejected or unconfirmed write and ends with a
diff, and the exit code is 0 only when the closing diff shows nothing the restore still owes.

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

**Choosing what to back up.** Four form pages are opt-in because restoring them can cut you off
(`bgwcli dump -h` prints the same lists):

<!-- include-pages:start -->
Read by every `dump`:

| Page id | What it adds to the dump |
| --- | --- |
| `sysinfo` | firmware version only (dump metadata) |
| `services` | custom services |
| `apphosting` | NAT/Gaming forwards |
| `ipalloc` | fixed reservations (+ allocation table, Fixed rows unless --all-clients) |
| `packetfilter` | packet-filter table for reference; best effort, omitted if unreadable |
| `dosprotect` | Firewall Advanced form values |
| `wconfig` | Advanced Wi-Fi form values |

Optional, with `dump --include <id>[,<id>]` or `--include all`; capturing is read-only, the notes are the risks of restoring them:

| Page id | What it captures; restore risk |
| --- | --- |
| `etherlan` | LAN port settings; restoring can cut the LAN port you are on |
| `dhcpserver` | Subnets & DHCP; restoring can move the gateway address (restored last) |
| `ippass` | IP Passthrough; restoring changes which LAN client gets the public address |
| `wmacauth` | Wi-Fi MAC filtering modes; disabling filtering (mode none) needs no recorded list; enabling allow/deny on any network needs a nonempty recorded list with every entry on the gateway, and the check covers the whole page, so a save that enables filtering on one network can also hold up disabling it on another |

`dump --include`: `all` = `etherlan`, `dhcpserver`, `ippass`, `wmacauth`; `dosprotect` and `wconfig` are accepted but always read; any other id is rejected. `diff`, `restore` and `autorestore` `--include` take the restorable ids `services`, `apphosting`, `ipalloc`, `dosprotect`, `wconfig`, `etherlan`, `dhcpserver`, `ippass`, `wmacauth` (omitted or `all` = everything restorable in the dump; for `autorestore` the selection also limits reset detection).
<!-- include-pages:end -->

How `restore` plans the pages (`bgwcli restore -h` prints the same list):

<!-- restore-phases:start -->
| Phase | Page id | What restore does there |
| --- | --- | --- |
| 1 | `services` | custom services (before the forwards that use them) |
| 2 | `apphosting` | NAT/Gaming forwards (a forward waits until its service appears in the dropdown) |
| 3 | `ipalloc` | fixed reservations (a reservation can need Allocate, then Save) |
| 4 | `packetfilter` | documentary only: recorded, never written |
| 5 | `dosprotect` | Firewall Advanced |
| 6 | `wconfig` | Advanced Wi-Fi |
| 7 | `wmacauth` | Wi-Fi MAC filtering modes (allow/deny need the recorded list on the gateway; the guard is page-wide) |
| 8 | `ippass` | IP Passthrough |
| 9 | `etherlan` | LAN ports (can cut the LAN port you are on) |
| 10 | `dhcpserver` | Subnets & DHCP, last (can move the gateway address) |

These are planning phases, not the order writes are sent: with `--prune` a forward is removed before its service, and a page with an add pending defers its removes.
<!-- restore-phases:end -->

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
(wired ones), the firewall flags and Advanced Wi-Fi, which brings the SSIDs and passwords back, the
guest network included when the dump was taken with it on, so Wi-Fi clients reconnect. Wait a few minutes and run the same commit again for their forwards and reservations.
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
baseline is always the state you want back. The unit can read the same [configuration file](#configuration-file)
as the CLI (point its `EnvironmentFile=` at `%h/.config/bgw/config`), and `BGW_AUTORESTORE_MAX_PASSES`,
`BGW_AUTORESTORE_WAIT` and `BGW_AUTORESTORE_ON_ANY_DIFF` in that file set the timer's defaults. Reset detection, the pass and run limits, the recovery
checkpoint and every status and exit code: [reference, Automatic recovery](docs/REFERENCE.md#automatic-recovery-autorestore).

## Router command tree

Every router tab is a command. All accept `--json`; `--forms` adds the page's form controls to the terminal view.
`bgwcli <section> -h` lists that section's pages, and `bgwcli tabs` shows every page with its command.

<!-- pages:start -->
### Device

| Command | Shows |
| --- | --- |
| `bgwcli device status` | Gateway overview: broadband, Wi-Fi, LAN and uptime (default) |
| `bgwcli device device-list` | Devices on the network with address and connection |
| `bgwcli device system-information` | Model, serial numbers, firmware (Software Version) |
| `bgwcli device access-code` | Device access code page (guarded) |
| `bgwcli device remote-access` | Remote management access settings |
| `bgwcli device restart-device` | Gateway restart page (guarded) |

### Broadband

| Command | Shows |
| --- | --- |
| `bgwcli broadband status` | Broadband link status, addresses and counters (default) |
| `bgwcli broadband configure` | Broadband connection settings |
| `bgwcli broadband fiber-status` | Optical module and fiber signal levels |

### Home Network

| Command | Shows |
| --- | --- |
| `bgwcli home-network status` | LAN and Wi-Fi traffic counters (default) |
| `bgwcli home-network configure` | LAN Ethernet port settings |
| `bgwcli home-network ipv6` | LAN IPv6 settings |
| `bgwcli home-network wi-fi` | Wi-Fi networks, names and passwords |
| `bgwcli home-network advanced-wi-fi` | Advanced Wi-Fi radio settings and channels |
| `bgwcli home-network mac-filtering` | Wi-Fi MAC address filtering |
| `bgwcli home-network subnets-dhcp` | LAN subnets and DHCP server settings |
| `bgwcli home-network ip-allocation` | IP address allocation and fixed reservations |

### Voice

| Command | Shows |
| --- | --- |
| `bgwcli voice status` | Phone line status (default) |
| `bgwcli voice line-details` | Provisioned phone line details |
| `bgwcli voice call-statistics` | Phone call statistics |

### Firewall

| Command | Shows |
| --- | --- |
| `bgwcli firewall status` | Firewall status summary (default) |
| `bgwcli firewall custom-services` | Custom service (port) definitions |
| `bgwcli firewall packet-filter` | Packet filter rules |
| `bgwcli firewall nat-gaming` | NAT/Gaming port forwards |
| `bgwcli firewall public-subnet-hosts` | Public subnet host settings |
| `bgwcli firewall ip-passthrough` | IP Passthrough settings |
| `bgwcli firewall firewall-advanced` | Advanced firewall protections |
| `bgwcli firewall security-options` | Firewall security options |

### Diagnostics

| Command | Shows |
| --- | --- |
| `bgwcli diagnostics troubleshoot` | Troubleshooting tests and diagnostic details (default) |
| `bgwcli diagnostics speed-test` | Speed test history |
| `bgwcli diagnostics logs` | Router event log |
| `bgwcli diagnostics update` | Firmware update page (guarded) |
| `bgwcli diagnostics resets` | Reset and restart options (guarded) |
| `bgwcli diagnostics syslog` | Remote syslog settings |
| `bgwcli diagnostics event-notifications` | Event notification settings |
| `bgwcli diagnostics nat-table` | Active NAT sessions |
| `bgwcli diagnostics site-map` | The router's own page index |
| `bgwcli diagnostics ping <host>` | Ping a host from the gateway; dry run, `--commit --confirm DIAG` sends |
| `bgwcli diagnostics traceroute <host>` | Trace the route to a host from the gateway; dry run, `--commit --confirm DIAG` sends |
| `bgwcli diagnostics nslookup <host>` | Resolve a name with the gateway's DNS; dry run, `--commit --confirm DIAG` sends |
<!-- pages:end -->

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

Every action, its token and the page it starts from (`bgwcli action -h` prints the same list):

<!-- actions:start -->
| Action | Token | Source page | Kind | What it does |
| --- | --- | --- | --- | --- |
| `restart` | `RESTART` | `restart` | dangerous | Restart the gateway. (also `restart-device`, `reboot`) |
| `clear-device-list` | `CLEAR-DEVICES` | `devices` | guarded | Clear inactive devices from the device list. |
| `run-speed-test` | `SPEED` | `speed` | guarded | Run the router speed test. (also `speed-test`) |
| `detect-wifi-congestion-2.4` | `CONGESTION-2.4` | `lanstatistics` | guarded | Run 2.4 GHz Wi-Fi congestion detection from LAN Statistics. (also `congestion-2.4`, `congestion-24`) |
| `detect-wifi-congestion-5` | `CONGESTION-5` | `lanstatistics` | guarded | Run 5 GHz Wi-Fi congestion detection from LAN Statistics. (also `congestion-5`, `congestion-5ghz`) |
| `clear-connection-statistics` | `CLEAR-CONNECTION-STATISTICS` | `lanstatistics` | dangerous | Clear connection statistics counters from LAN Statistics. (also `clear-connection-stats`) |
| `clear-lan-statistics` | `CLEAR-LAN-STATISTICS` | `lanstatistics` | dangerous | Clear LAN statistics counters using the LAN Statistics Clear Statistics button. (also `clear-statistics`, `clear-lan-stats`) |
| `run-full-diagnostics` | `DIAG` | `diag` | guarded | Run the router's full diagnostic test suite. (also `full-diagnostics`) |
| `send-diagnostics` | `SEND-DIAGNOSTICS` | `diag` | guarded | Send the router diagnostics report to AT&T. (also `send-diagnostic-report`) |
| `diagnostics-ethernet-details` | `DIAG` | `diag` | guarded | Show Ethernet diagnostic details. (also `ethernet-details`) |
| `diagnostics-authentication-details` | `DIAG` | `diag` | guarded | Show authentication diagnostic details. (also `authentication-details`) |
| `diagnostics-ip-details` | `DIAG` | `diag` | guarded | Show IP diagnostic details. (also `ip-details`) |
| `diagnostics-dns-details` | `DIAG` | `diag` | guarded | Show DNS diagnostic details. (also `dns-details`) |
| `packet-filter-enable` | `PACKETFILTER` | `packetfilter` | guarded | Enable packet filters. (also `enable-packet-filter`, `enable-packet-filters`) |
| `packet-filter-disable` | `PACKETFILTER` | `packetfilter` | dangerous | Disable packet filters (the gateway stops applying every packet-filter rule). (also `disable-packet-filter`, `disable-packet-filters`) |
| `packet-filter-add-drop-rule` | `PACKETFILTER` | `packetfilter` | guarded | Open/add a packet-filter drop rule. (also `add-drop-rule`) |
| `packet-filter-add-pass-rule` | `PACKETFILTER` | `packetfilter` | guarded | Open/add a packet-filter pass rule. (also `add-pass-rule`) |
| `reset-ip` | `RESET-IP` | `reset` | dangerous | Reset the router IP stack. |
| `reset-connection` | `RESET-CONNECTION` | `reset` | dangerous | Reset the router broadband connection. |
| `restart-from-resets` | `RESTART` | `reset` | dangerous | Restart from the Diagnostics > Resets page. |
| `reset-wifi-config` | `RESET-WIFI-CONFIG` | `reset` | dangerous | Reset Wi-Fi configuration. (also `reset-wi-fi-config`) |
| `reset-firewall-config` | `RESET-FIREWALL-CONFIG` | `reset` | dangerous | Reset firewall configuration. |
| `factory-reset` | `FACTORY-RESET` | `reset` | dangerous | Reset the device to defaults. (also `reset-device`) |
| `restart-wifi-2.4` | `RESTART-WIFI` | `home` | guarded | Restart the 2.4 GHz Wi-Fi radio (home.ha Restart button; clients on 2.4 GHz drop briefly). (also `restart-wifi-24`, `restart-2.4ghz`, `restart-wifi-2-4`) |
| `restart-wifi-5` | `RESTART-WIFI` | `home` | guarded | Restart the 5 GHz Wi-Fi radio (home.ha Restart button; clients on 5 GHz drop briefly). (also `restart-wifi-5ghz`, `restart-5ghz`) |
| `restart-broadband` | `RESTART-BROADBAND` | `home` | dangerous | Restart the broadband connection (home.ha Restart button; drops WAN for a while). (also `restart-wan`) |
| `find-best-channel-5` | `CHANSCAN` | `wconfig` | guarded | Run the 5 GHz 'Find Best Channel' scan on Advanced Wi-Fi (5 GHz clients drop briefly). (also `chanscan5`, `find-best-channel`, `scan-5ghz-channel`) |
<!-- actions:end -->

<!-- write-rules:start -->
On the guarded pages (`reset`, `restart`, `routerpasswd`, `update`) `set` refuses even a dry run, and `submit` previews a button on them but refuses `--commit`; use a named `action` where one exists (Restart and Resets have dangerous actions; Access Code and Update have no registered action).

`set` and `submit` commit with `--commit --confirm <TOKEN>`: the page id in upper case, punctuation runs become hyphens, as the dry run prints it (`wconfig_unified` → `WCONFIG-UNIFIED`).

A write is sent once, with one exception: if the gateway answers a configuration POST with its Login page, bgwcli logs in again and re-sends once, so two attempts can be reported.
<!-- write-rules:end -->
`applied` means the gateway acknowledged it and the requested state read back, an explicit rejection is exit 1, and an answer the CLI
could not read is exit 2. In the JSON, `committed: true` means the gateway acknowledged the save (`Changes saved`), even when
the later read-back failed. `committed: false` does not prove the write did not land: `writeAttempted` says the POST was sent,
`writeResponseReceived` says the gateway answered it with any status, and `writeAttempts` is 2 after a re-send and absent
otherwise; a key whose answer is unknown is absent. After an uncertain write, re-read the gateway state (for example
`bgwcli page <page>`) before retrying.
Secrets (passwords, keys, access codes, nonces, phone numbers) are redacted in terminal and JSON output unless
you pass `--include-secrets`, except `--raw` output, `--out` artifacts and dump files, which keep them (files are
owner-only). The complete rules: [reference, Safety](docs/REFERENCE.md#safety).

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success: `diff` identical, `restore --commit` converged, `autorestore` no reset or converged, a save acknowledged and read back as requested. |
| `1` | Negative answer: `diff` differs, restore not converged, usage error, confirmation refused, the gateway rejected a write, or a save read back differently. |
| `2` | No answer: connection or authentication failure, a full session pool, a page that could not be read, or a write whose acknowledgement was never seen (check `committed` before retrying). |
| `130` | Interrupted (Ctrl-C); with `--json` the object carries the write evidence. |

The backup commands in detail (`bgwcli <command> -h` prints the same rows):

<!-- backup-exits:start -->
| Command | Code | Meaning |
| --- | --- | --- |
| `dump` | `0` | dump file written |
| `dump` | `1` | usage error (e.g. an unknown --include id) |
| `dump` | `2` | the router or a page could not be read, or the snapshot could not be built |
| `dump` | `2` | the --out destination is unusable or could not be written |
| `dump` | `130` | interrupted (Ctrl-C) |
| `diff` | `0` | identical (a firmware-only change is still 0) |
| `diff` | `1` | differences found, or none of the --include pages is in the dump |
| `diff` | `2` | the dump file or the router could not be read |
| `diff` | `130` | interrupted (Ctrl-C) |
| `restore` | `0` | dry run: plan printed (even when work remains) |
| `restore` | `0` | --commit: the closing diff finds no selected dumped service, forward, reservation or form value still missing or different (--prune: and no extra service or forward); form fields the live page disables or does not render, controls only on the router, and extra reservations are ignored |
| `restore` | `1` | not converged, a write rejected, confirmation refused, or nothing to compare |
| `restore` | `2` | the dump file or the router could not be read |
| `restore` | `2` | the allocation preflight is blocked (a fixed allocation held by another MAC, no enabled Clear and Rescan button, or a failed preflight or rescan), also in a dry run |
| `restore` | `2` | a write was sent and never acknowledged (even when the closing diff is readable) |
| `restore` | `130` | interrupted (Ctrl-C); the write state is reported |
| `autorestore` | `0` | no reset, converged, or router unreachable (also during the allocation preflight, before any write) |
| `autorestore` | `1` | a restore is needed (dry run) or did not converge (the next timer run retries) |
| `autorestore` | `2` | page-level 401/403 or an initial structural failure |
| `autorestore` | `2` | the allocation preflight was blocked by a conflict or failed for another reason |
| `autorestore` | `2` | a write was sent and never acknowledged, or the closing verification failed |
| `autorestore` | `2` | a recovery checkpoint could not be read or written, another unfinished recovery is recorded, or the same failure ended three consecutive runs |
| `autorestore` | `130` | interrupted (Ctrl-C) |
<!-- backup-exits:end -->

The exact mapping per command and per answer: [reference, Exit codes](docs/REFERENCE.md#exit-codes).

## Environment variables

Every variable can also live in the [configuration file](#configuration-file). Every duration is in seconds, fractions allowed (`BGW_TIMEOUT='1.5'`); every boolean takes `true`/`false` (or `1`/`0`, `yes`/`no`, `on`/`off`). The first group mirrors the TypeScript CLI's variables apart from that unit:

| Variable | Meaning | Default |
| --- | --- | --- |
| `BGW_HOST` / `ROUTER_IP` | Router host (`--host`) | `192.168.1.254` |
| `BGW_ACCESS_CODE` | Device access code (`--access-code-stdin` is the alternative) | unset |
| `BGW_FALLBACK_ACCESS_CODE` | `autorestore` only: the sticker code a reset reverts to | unset |
| `BGW_TIMEOUT` | Request timeout in seconds (`--timeout`), at most `3600` | `15` |
| `BGW_INSECURE_TLS` | `false` enforces TLS validation (`--strict-tls`) | `true` (accept self-signed) |
| `BGW_WAIT_FOR_SESSION` | `true` waits when the web session pool is full | `false` |
| `BGW_SESSION_WAIT_TIMEOUT` / `BGW_SESSION_WAIT_INTERVAL` | How long to wait for a free session and how often to retry, in seconds (`--session-wait-timeout`, `--session-wait-interval`) | `120` / `10` |
| `BGW_SESSION_CACHE_DIR` / `XDG_CACHE_HOME` | Session cache location | `~/.cache/bgw` |
| `BGW_DUMP_DIR` / `XDG_STATE_HOME` | Default `dump` output directory | `~/.local/state/bgw/dumps` |
| `BGW_CONFIG` | Configuration file (`--config`) | `~/.config/bgw/config` |

Standing preferences for flags, meant for the configuration file (a flag given on the command line still wins):

| Variable | Flag | Default |
| --- | --- | --- |
| `BGW_JSON` | `true` means `--json` everywhere | `false` |
| `BGW_LIMIT` | `--limit` | `20` |
| `BGW_DELAY` | `--delay`, in seconds | `1` |
| `BGW_DUMP_ALL_CLIENTS` | `true` means `dump --all-clients` | `false` |
| `BGW_DUMP_INCLUDE` | `dump --include <csv\|all>` (`dump` only) | core pages |
| `BGW_AUTORESTORE_MAX_PASSES` | `autorestore --max-passes` | `3` |
| `BGW_AUTORESTORE_WAIT` | `autorestore --wait`, in seconds | `120` |
| `BGW_AUTORESTORE_ON_ANY_DIFF` | `true` means `autorestore --on-any-diff` | `false` |

Boolean variables take `1`, `true`, `yes`, `on` or `0`, `false`, `no`, `off` (any case); anything else is a usage error naming the variable.

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
