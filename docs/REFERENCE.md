# bgwcli reference

The full behaviour contract of `bgwcli`: what every command does on every answer the gateway can give,
the exit codes, the environment variables, the session cache and recovery-state rules, redaction, and the
live-verification record. The [README](../README.md) is the short version with the everyday commands and
examples; this file is where every sentence of the contract lives, and the tests pin it.

Contents: [Install details](#install-details) · [Quality checks](#quality-checks) · [Commands](#commands) ·
[Backup contract](#backup-contract) · [Choosing what to back up and restore](#choosing-what-to-back-up-and-restore) ·
[Automatic recovery (autorestore)](#automatic-recovery-autorestore) · [Page views and `--limit`](#page-views-and---limit) ·
[Generic Inspection And Forms](#generic-inspection-and-forms) · [Sweep](#sweep) · [Timeouts](#timeouts) ·
[Environment Variables](#environment-variables) · [Router Fixture Pack](#router-fixture-pack) · [Diagnostics](#diagnostics) ·
[Safety](#safety) · [Exit Codes](#exit-codes) · [Differences from bgw](#differences-from-bgw) ·
[Router Tab Coverage](#router-tab-coverage) · [Live verification](#live-verification) ·
[Radio and broadband restarts](#radio-and-broadband-restarts) · [5 GHz channel scan](#5-ghz-channel-scan)

## Install details

How the executable is built: the installer uses a Python already on the machine (the default `python3` first, then the other installed `python3.x`; bgwcli needs 3.10 or newer, and pip checks PyInstaller's own supported-version metadata, so an interpreter too new for the current PyInstaller is skipped for the next one; `BGWCLI_PYTHON=3.12` names one), installs PyInstaller into a throwaway venv under a temporary directory (pip's and PyInstaller's caches live in that directory too), builds, and deletes the directory, so the executable at `~/.local/bin/bgwcli`, with its Python embedded, is the only thing that stays. When no installed Python works it stops and says what is missing (3.10+, PyInstaller support, `python -m venv`) instead of fetching anything. The file runs only on the OS and CPU it was built on (build once per machine type), is about 10 MB, and starts in roughly half a second because it unpacks itself on every run, so it suits a machine you copy one file to rather than scripted bursts or the autorestore timer. The two flavours replace each other, so only one `bgwcli` is ever on your PATH: `binary` removes a uv-tool install first, and the plain installer replaces the executable with the tool's symlink (or removes the executable when uv links its tools into another directory). Everything else (session cache, dumps, config paths, exit codes) is identical between them.

What uninstall does: runs `uv tool uninstall bgwcli` when `uv` is on your PATH (otherwise it names the pipx and pip equivalents), removes the single-file executable at `~/.local/bin/bgwcli` when that is what is installed (a `bgwcli` launcher script that pip or pipx put there is left in place and their uninstall commands are named instead), then prints the per-user state paths (session cache, state and dump directories, autorestore timer files, the baseline dump) marked present or absent, and deletes none of them. If you deployed the autorestore timer, remove it first as `deploy/README.md` ("Removing it") describes.

Requirements: Python 3.10 or newer, nothing else at runtime. The one-line installer and the `git+https://...` commands install from a git URL, so they also need `git` on your PATH; the installer checks that `git --version` runs before it installs anything (including `uv`) and stops with a message when git is missing or is only the macOS command-line-tools stub that fails when run. Dev extras: pytest, pytest-xdist, ruff.

## Quality Checks

```bash
.venv/bin/pytest
.venv/bin/ruff check
```

Tests never touch the gateway: parser tests use inline HTML, CLI tests inject a fake client, and the
router fixture pack tests skip when `tests/fixtures/router-html` is absent. An autouse guard in `tests/conftest.py` fails any test that connects to, or looks up the name of, anything other than a loopback address, so a test cannot reach a real host even by accident.

## Commands

| Command | Purpose |
| --- | --- |
| `check` | Verify the router is reachable. Never logs in: `authenticated` is true only when the gateway serves a protected page to the session the CLI holds (for example a cached one). A held session the gateway refuses (the Login page, a redirect to `login.ha`, or 401/403) reports `authenticated: false` and the cached session is dropped instead of kept or re-cached; a transport fault or another HTTP error leaves the cache untouched. A full session pool is reported as such (exit 2, `sessionPoolFull`, cooldown recorded), not as an unauthenticated session. `check` exits 2 when the gateway did not answer at all (`reachable: false`), so a script can tell an unreachable router from a reachable one; the JSON object is still printed. |
| `auth` | Verify the access code with a fresh login against the router, even when a session is cached; a cached session is never taken as proof. |
| `tabs` | Print the CLI's router tab map. |
| `actions` | List guarded router actions and confirmation tokens. |
| `action <name>` | Dry-run a guarded router action. Requires `--commit --confirm TOKEN` to POST. |
| `section <section>` | Print mapped tabs for one router section. |
| `coverage` | Compare the CLI tab map against the live router sitemap; an HTTP error answer, a `Page not found` document or the Login page in place of the sitemap exits 2. |
| `sweep` | Shared traversal command for mapped router pages. Default output is compact status/count metadata. |
| `scan` | Compatibility alias for compact sweep metadata. |
| `schema` | Sweep with parsed/form detail enabled. |
| `audit` / `readiness` | Sweep-backed health check that keeps going through hangs and summarizes failed/fallback/empty/useful pages. |
| `sitemap` | Print the live router sitemap; an HTTP error answer, a `Page not found` document or the Login page in place of it exits 2. |
| `page <page-or-tab>` | Fetch and parse any mapped tab or raw CGI page ID. |
| `inspect <page-or-tab>` | Fetch a page and include parsed form fields/selects. |
| `status` | Fetch the core status pages; exits 2 when none of them could be read. |
| `devices` | List online devices (status exactly `on`, case-insensitive); `--all` adds offline (remembered) devices. Status comes from the gateway and may be stale. |
| `wifi` | Fetch Wi-Fi configuration with secrets redacted by default. |
| `nat` | Fetch NAT table details. |
| `logs` | Fetch router logs. `--limit N` shortens only the text listing (the summary still counts every entry) and ends it with the overflow line; `--json` always returns every entry. A 200 answer that is the router's "Page not found" document or the Login page is a page failure (exit 2, structured), never an empty log, and so is a 200 answer without a log table: the table must open with a header row of at least six columns naming at least three of time, source, destination, protocol and reason (a busy or unrelated page is not the log), while a log table with its header and no rows is an empty log. |
| `session status` / `session clear-cache` | Inspect or clear the local session coordination state. |
| `set <page> KEY=VALUE...` | Build a dry-run mutation plan. Requires `--commit --confirm TOKEN` to POST. |
| `submit <page> <button> KEY=VALUE...` | Build a dry-run form/button submission. Requires `--commit --confirm TOKEN` to POST. The button must belong to the page's own form (one posting to `/cgi-bin/<page>.ha`): a button that lives in a form posting elsewhere (the Wi-Fi restart buttons on `home`) is a usage error (exit 1, dry-run and `--commit` alike, nothing posted) naming the matching `action <name>` when there is one, because the page's fields are never sent to another form. |
| `device` / `broadband` / `home-network` / `voice` / `firewall` / `diagnostics` `<tab>` | Section commands; see the Router Command Tree below. |


## Backup contract

| Command | What it does |
| --- | --- |
| `bgwcli dump [--out <file>] [--include <csv\|all>] [--all-clients]` | Captures custom services, NAT/Gaming forwards (device label resolved to MAC), host reservations (Fixed Allocation rows) and the core form pages Firewall Advanced (`dosprotect`) and Advanced Wi-Fi (`wconfig`) to an owner-only JSON file; the whole snapshot is validated with the dump loader's own rule before anything is written, and any problem (duplicate service names, an invalid forward entry, ...) is exit 2 with nothing written. `--include` adds the optional form pages LAN ports (`etherlan`), Subnets & DHCP (`dhcpserver`), IP Passthrough (`ippass`) and Wi-Fi MAC Filtering modes (`wmacauth`); `--include all` captures every page. Pages not included are not fetched. Packet-filter rules and the MAC filter list are recorded as documentary tables only; the packet-filter page is best effort: when it cannot be read for any reason (it did not load, or the parser had to cut it at its bounds), a `warning: documentary page 'packetfilter' could not be read (...)` line goes to stderr and the dump (or `diff`/`restore`/`autorestore` read) continues without `tables.packetfilter`. The services, NAT/Gaming forwards and IP Allocation pages must show their table header row (the expected columns) - or, for services and NAT/Gaming forwards, the gateway's empty-table cell described below - before they count as a section: a page answered with neither (a "Please wait" page, a cut-off body, a renamed header) is a failed read, never an empty section, so `dump` refuses, `diff`/`restore` exit 2, `autorestore` handles it like any page that could not be read, and `--prune` cannot delete rows the dump meant to keep. A header row with no data rows is a legitimate empty section, and so is the gateway's own empty rendering: with no entries at all it drops the header row and shows a single cell reading "No Custom Service entries have been defined" (services) or "No Application Hosting entries have been defined" (NAT/Gaming), so a dump taken then records the section as empty and `diff`/`restore`/`autorestore` read it as a section with every row missing (the post-factory-reset state). A form page must show at least one form control in the same way: a page answered without any control at all (a "Please wait" document, an empty or cut-off body) is a failed read, never an empty form, so `dump` refuses it and `diff`/`restore`/`autorestore` treat it like a page that could not be read. `dump` also fails (exit 2, nothing written) when the live services table lists the same name twice (compared stripped and case-insensitively); the message names the duplicates. A dump file with duplicate service names is rejected on load, and `restore` never adds a service whose name is already on the router. |
| `bgwcli diff <dumpfile> [--include <csv\|all>]` | Read-only comparison of the dump with the live router: every section and every form page present in the dump. `--include` restricts the comparison to the listed page ids (`services`, `apphosting`, `ipalloc` and the form page ids); a requested page the dump never captured prints a warning on stderr and is skipped. Exit 0 when identical, 1 when different, 2 on error (including a page the router answered without its table). When none of the requested pages is in the dump nothing is compared: exit 1 with `nothing compared: <pages> not in dump` (`--json` keeps `missingPages`), never `No differences.` with exit 0. |
| `bgwcli restore <dumpfile> [--prune] [--include <csv\|all>] [--commit --confirm RESTORE]` | Dry-run by default: prints the ordered plan (services → forwards → reservations → firewall advanced → Advanced Wi-Fi → Wi-Fi MAC filtering → IP Passthrough → LAN ports → Subnets & DHCP). Only adds what is missing and only saves forms whose values differ; optional pages are restored only when the dump captured them, and `--include` restricts the plan like `diff` (a requested page missing from the dump becomes a `skip` step; when none of the requested pages is in the dump the run is `nothing compared: <pages> not in dump`, exit 1, dry-run and `--commit` alike). `--prune` also removes router rows not in the dump, but a page that still has an addition to make has its removes deferred, so adds and prunes can need two runs, and several router-only rows on one page need one `--prune` run per row. `--prune` never releases a reservation back to DHCP - extra reservations are reported by `diff` only, releasing one stays a manual UI action. Live runs need both `--commit` and `--confirm RESTORE`; the run stops at the first rejected or unconfirmed write and ends with a diff. When stdout is a closed pipe (a hand run piped to `head`), the run stops printing progress but completes; its exit code is the run's own, as for `autorestore`. A planned step marked `applied` requires both `Changes saved` and the requested form values or table state. One rule decides `writeUnanswered` for `restore`, `autorestore` and the CLI: a configuration write was sent (or may have been: its delivery is unknown) and the gateway gave no acknowledgement or answer. A pool that is full before any write was sent is `sessionPoolFull` with `writeAttempted: false`, not an unanswered write; Clear and Rescan for Devices without an answer ends the run through `allocationPreflight` (exit 2), never as `writeUnanswered`. The closing diff checks configuration convergence, but a write that got no answer (see below) keeps the exit at 2 even when that diff is readable: `--json` then carries `writeUnanswered: true` and `unansweredWrite`. Packet-filter rules are captured as text only and never restored. |
| `bgwcli autorestore <dumpfile> [--commit --confirm RESTORE] [--max-passes N] [--wait S] [--on-any-diff] [--include <csv\|all>]` | Unattended factory-reset recovery for a systemd timer. Diffs the live router against the dump and restores it (never `--prune`) only when the difference looks like a factory reset: every dumped service, forward and reservation missing (or an unfinished recovery of the same dump). A login that needed `BGW_FALLBACK_ACCESS_CODE` corroborates such a difference but is not a reset on its own. Ordinary drift is reported as `no-reset` and left alone. When `--include` names only pages the dump never captured, nothing is compared: exit 1 with `nothing compared: <pages> not in dump` (`--json`: `status: "nothing-selected"` and `missingPages`) before any login, page read or recovery intent, as for `diff` and `restore`. See [Automatic recovery](#automatic-recovery-autorestore). |

Dump files are schema 2 JSON, byte-compatible with the TypeScript CLI: a dump written by either tool
diffs clean and restores with the other. Schema-1 dumps are refused on load. One addition: when a dumped
form page has `type="password"` controls, the dump also carries `formSecrets` (page id -> those field
names), because field names alone do not show that such a value is secret; `diff` and `restore` output
keep those values redacted even when the live page no longer renders the control as a password input.
The key is absent from every other dump, and readers that do not know it ignore it. A dump that carries it is no longer byte-identical to the TypeScript CLI's dump of the same router, and the TypeScript CLI, which ignores the key, will not redact a password-type value whose field name does not look secret.
A form page with a text or select control whose real value is the string `<unchecked>` also carries `formUncheckedText` (page id -> those field names), so that value is restored as text while an off checkbox/radio (recorded as `<unchecked>`) is posted absent; a dump without the key (older, or from the TypeScript CLI) reads every `<unchecked>` as an off box, so one on a field the live page renders as text is skipped with a `warning:` line naming it (printed by the dry-run plan and by the `--commit` output, and carried as `warning` on the skipped step in `--json`).

Older schema-2 dumps can record a checked checkbox/radio with no HTML `value` attribute as `""`
instead of the browser default `"on"`. Live diff/restore recognizes that spelling only when the
current HTML proves an implicit default without an explicit-empty or conflicting same-name control.
Python callers should retain `Snapshot.live_form_evidence` from `extract_snapshot` when copying or
projecting the same capture; `dataclasses.replace(live, forms=...)` preserves it even when the form
dictionaries are copied or filtered. Evidence belongs to that capture and must not be reused with
controls from another capture. Schema-2 JSON intentionally stores no HTML evidence: serializing and
loading a snapshot, or constructing one from form values alone, compares those strings literally.
Use a fresh parsed live capture for legacy compatibility; empty values are never migrated blindly.

Dumps taken before form data was kept as the page wrote it stored every attribute value (input and
option values) with U+00A0 replaced by a plain space, and textarea text whitespace-normalised.
`diff` treats exactly those spellings of the unchanged live value as equal (the whitespace collapse
only for controls the live page renders as textareas), so such a dump neither diffs against the
unchanged router forever nor plans a restore that would post the altered spelling back.

A reservation is restored through the same two-step flow the router UI uses: `Allocate` opens an "IP Allocation Entry" block for that device, then a second POST selects the target address and saves it. Each allocation waits for both `Changes saved` and a Fixed Allocation row matching the intended MAC and IP before the next step. Only addresses offered by the gateway can be selected. After a restore, the gateway keeps that "IP Allocation Entry" block rendered for the rest of the web session even though nothing further is pending; this is harmless sticky session state, not an unsaved change. `set` and `submit` on `ipalloc` therefore never post an `alloc_<mac>` select the user did not assign: the sticky block's select is dropped from the base payload, so a plain `set`/`submit` there cannot release or move another device's reservation.

Before any restore configuration writes, pending reservations are checked against both `devices.ha` and `ipalloc.ha`, including offline devices and fixed allocations. Ownership is matched by IP and MAC (case-insensitive), never by hostname. A different MAC's **Fixed Allocation** requires manual release and stops restore promptly without Clear; a Devices `static` label alone does not prove a fixed allocation. For stale DHCP/discovery conflicts, a committed restore presses **Clear and Rescan for Devices** once, waits at least 60 seconds, then checks both pages every 5 seconds within a 180-second polling window. Every pending requested IP is rechecked, and a different MAC reported by either page keeps the IP occupied. Transient connection, busy, malformed, or incomplete responses are retried within that budget; only a complete valid pair can prove availability. Terminal authentication failures stop verification. No new ownership read starts at or after the deadline. Unresolved conflicts or verification failures stop the run with a reason. After a successful rescan, all restore pages are refreshed and the plan is rebuilt. Dry-run marks the plan explicitly pending or blocked and incomplete, with the ownership reason. Correct existing reservations and reservations excluded by `--include` do not trigger this check.

A reservation refused before its Save POST (the requested address is not offered or is disabled, the Allocate entry did not open or has no Save button, or the value is not a dotted IPv4 address) is reported `blocked` with that reason and `writeAttempted: false`; it does not stop the run, and the rest of the plan continues. An HTTP error or a fault while reading the entry page is a `failed` step and stops the run, because the gateway gave no answer. The same holds when a write's 3xx answer points at a page that then cannot be read for an HTTP error status or a page-level 401/403: the step is `failed` with the POST's evidence (`writeAttempted`, `writeResponseReceived`, `statusCode`) and counts as a write with no answer (exit 2), not as success; a lost session or a full session pool on that read raises with the same evidence, and a lost connection on that read is the same failed step (exit 2): the POST was delivered, its answer was not seen. The one exception is an action whose effect takes the gateway's web server down (see Diagnostics). A session pool that fills while an acknowledged write is being verified exits 2 with the pool-full JSON still carrying `writeAttempted`, `writeResponseReceived`, `writePerformed`, `acknowledgementObserved` and `committed`, so scripts can tell the write reached the router. On a page without a Save button, a verification fault (a full session pool or a lost session) after the POST was answered reports `writeAttempted` and `writeResponseReceived` as true (and `writeAttempts` after a re-send). A LAN save whose acknowledgement was observed stays `committed` when the later poll of the requested state fails or meets a full pool: the result keeps `writePerformed`, `acknowledgementObserved` and `committed` and exits 2 because the state is unverified. A reservation is never held back until after Subnets & DHCP: an address outside the current subnet is reported `blocked` ("not offered") in that run and is picked up by a later run once the new subnet is live. A dumped form field the live page does not render at all (for example one removed by newer firmware) is reported by `diff`, left out of the restore POST and of the post-save check with a `skip` note naming it, and does not stop the rest of that page from being restored; it never holds restore or autorestore convergence, or factory-reset detection, open, because no save can ever make the page show it. That tolerance applies only to a page that rendered a form with at least one control: a form page answered without any control is a failed read (a `restore` step blocked or failed, `diff` exit 2), never "every field removed by the firmware". A dump value `<unchecked>` for such a checkbox or radio is not even a difference. In the post-save check an absent checkbox counts as unchecked only on a readable form: a page with no form control, a Login page or a Page-not-found answer proves nothing about the box (and a services, forwards or IP Allocation page without its table header row proves nothing about a row), so an acknowledged save followed only by such reads is an unverifiable `failed` step (exit 2, `acknowledgementObserved` true; `stateObserved` is absent, or `false` when the acknowledgement page itself already showed the box still checked and the later reads failed outright). An `--include` or `--pages` list that names no page at all (`--include=`, or only commas) is a usage error (exit 1) on every command, before the router is touched; it never widens to every page. `dump --include` still accepts the core page names (`dosprotect`, `wconfig`) and ignores them, since those pages are always captured.

`restore` never posts a button the page renders disabled: a disabled `Allocate` button, or a disabled `Save` on the IP Allocation entry form, makes that step `blocked` with the reason (`writeAttempted: false`), as `set` and `submit` do. Write accounting is conservative: a configuration POST whose connection failed before any bytes were written is still reported as `writeAttempted: true` with no response (unanswered), because the tool cannot prove it was not sent. A `restore --commit` step that fails on an HTTP or transport fault before any write was sent ends the run with exit 2 and an `executionFault` object (`order`, `page`, `error`) in `--json`; after a full session pool or a lost session no closing read is made. `writeUnanswered` is also true for a LAN step that required a reconnect when its POST got no response.

The initial ownership check may reuse the successful raw IP Allocation response collected by that invocation's snapshot fetch, if it is at most 30 seconds old when needed and belongs to the same client. It validates the original HTML, consumes that evidence once, and otherwise fetches the page again. Raw HTML and its private provenance are never included in public JSON. Both ownership pages are always fetched again after Clear.

Every configuration Save, custom-service Add/Remove and Application Hosting Entry Add/Remove waits for `Changes saved`, including when the message is red. The polling window is 60 seconds per write. Verification GETs may be retried during that window; the write itself is sent once. A timeout stops the restore invocation (including subsequent autorestore passes) and asks you to inspect the gateway before retrying. These polling windows are separate from `--timeout`, which still limits each HTTP request; an in-flight request can extend elapsed time beyond the polling window. An `applied` planned restore step requires acknowledgement and its known requested state. A stale banner alone cannot confirm a dropped write. If the acknowledgement is lost, observed desired state is reported separately and the write remains unconfirmed; it is never automatically resubmitted. A LAN address, mask or DHCP change is `applied` when its POST response already contains both the save acknowledgement and the requested state. If `ipaddr` changed, the applied step retains `lanAddressChanged` and the known reconnect address; restore skips the closing snapshot at the old endpoint, and whole-snapshot convergence remains unavailable until reconnecting. An applied mask/DHCP-only change still gets its closing diff at the current endpoint. Otherwise an attempted LAN Save returns `reconnect-required` with the next address when known and skips polling the old address. A failed nonce/authentication read proven to precede the POST is a nonwrite (`writeAttempted: false`, `writeUnanswered` false), and an explicit rejection is a failure; neither reports a committed change or a reconnect address. An HTTP or transport fault of that kind still makes the run exit 2 (a fault, not a negative answer) even when the closing diff is readable; after a full session pool or a lost router session no closing read is attempted at all, so an exhausted pool is not asked again. LAN `set` and `submit` results report `committed: true` only for an applied change. A LAN `set` that shows `Changes saved` but reads back different values is `applied` with `verified: false` and `mismatches`, exit 1, like every other page. Reconnect and verify an uncertain LAN change before retrying. A failed write ends the current run. Subsequent runs re-read the gateway and may retry remaining differences, subject to the recovery failure limit. `blocked` steps (such as an unopened allocation entry) never stop a pass and are planned again in the next one. The final diff verifies configuration convergence; neither result proves that the destination host has renewed its DHCP lease or that forwarded packets reach it.

Because a reservation address ends up in that second POST verbatim, `restore` refuses to release one to DHCP by accident: a dump file whose reservation entries are not a MAC plus a dotted IPv4 address (four octets, each 0-255) is rejected on load, and the executor refuses to post any allocation value that is not one (the router's entry form always offers `normal`, "Address from DHCP pool"). If the gateway is hiding the per-row `Allocate` buttons because an entry block is still open, reserve steps are reported blocked with "not present on IP Allocation page" - close the gateway web session or wait for it to expire, then re-run.

Wi-Fi's explicit `No changes detected. Save not performed.` notification produces an `unchanged` result with `committed: false` and `writePerformed: false`. The request was sent (`writeAttempted: true`), but the gateway performed no save, so there is no wait for `Changes saved`. The notification alone never decides the outcome: on a `No changes detected` answer both `set` and `submit` re-read the page and compare every requested value with the live form before answering. A match is exit 0 (`verified: true`, the result states the live form was re-read); a difference is exit 1 with `outcome: failed` and the differing fields under `mismatches` (the router ignored or normalised the change), and `autorestore` does not re-post it in a later pass; a re-read that fails is exit 2 because the state is unknown. A generic Save without requested values reports only the no-change notification. A refusal before the POST (a refused nonce page: nothing was sent, `writeAttempted: false`, `writePerformed: false`) is never reported as a no-change result: it is a failed result with exit 2 and the refusal reason, without a re-read, on Wi-Fi, LAN and every other page. `No changes detected. Save not performed.` is a no-change answer on any form page, redirected or inline, with or without the error icon (the Wi-Fi pages are where it was seen live; the LAN page answers an unchanged save with `Changes saved`): the live re-read decides, so a plan that already matches is `unchanged` (exit 0) and one that differs is a mismatch (exit 1). Restore counts unchanged steps separately, and autorestore removes only newly created recovery intent when all attempted saves explicitly performed no write; earlier recovery intent remains until convergence is verified.

Every `set`/`submit` save (Wi-Fi, LAN and generic pages) reports the same outcome through the same mapping (`save_result.py`): `Changes saved` is `applied` (exit 0); an error banner is `failed` with `router rejected the change: <banner>` (exit 1); a missing acknowledgement is `failed` (exit 2) with the router state unknown. After `Changes saved`, `set` always re-reads the page and a field that reads back differently makes the result `applied` with `verified: false` (exit 1); an `ipalloc` reservation (`alloc_<mac>=<ip>`) the save wait already saw as a matching Fixed Allocation row is verified by that state instead (the gateway no longer renders its editor select, so the field cannot be compared), so `set ipalloc alloc_<mac>=<ip>` exits 0 with `verified: true`. When the save wait did not see that row (it could not read the page, or the pages it read did not show the row yet), the re-read is verified from its Fixed Allocation rows (never from the vanished select): a row with that MAC and IP exits 0, a readable table without it is `verified: false` (exit 1) naming the row, and an unreadable re-read is unverified (exit 2). A release (`alloc_<mac>=normal`) is verified the same way by the device's row no longer being a Fixed Allocation (exit 0); a row that is still Fixed, or no row for the device at all, is `verified: false` (exit 1).

`submit` is not re-read after a plain `Changes saved`: an applied `submit` exits 0 without verification (`verified` and `verifyAttempts` are absent). It is re-read only when the save was acknowledged but a requested state the save wait checks (an `ipalloc` reservation, a release `alloc_<mac>=normal` whose device is not yet listed without a Fixed Allocation row, or a LAN setting) is not visible afterwards; the re-read then decides like for `set` (exit 0 `verified: true`, 1 on a difference, 2 when it cannot be completed, `verifyAttempts` present). A `submit` that requests no values posts no such state, so nothing triggers that re-read after `Changes saved`; its `No changes detected` answer is re-read only with requested values, as described in the Wi-Fi paragraph above. An applied LAN address move is not re-read either: the old address is no longer valid, so the result is `applied` with `reconnectAddress` and no `verified` field. The write is sent once in every case, except the documented re-send after a Login-page answer (see below); the Wi-Fi Warning `Continue` is the second POST of one save, not a repeat of the write. When the gateway acknowledges a save (`Changes saved`) and the page's known state is not visible afterwards, the write is reported as committed (`committed: true`, outcome `applied`) and the live re-read decides the exit code: 1 when it still differs, 2 only when the re-read could not complete. This is the same on Wi-Fi, LAN and every other page.

The verification re-read (after `Changes saved` on `set`, and the `No changes detected` check on `set`/`submit`) is retried: up to 3 attempts, waiting 2 s before the second and 4 s before the third, on exactly HTTP 408, 429, 500, 502, 503 and 504 (501, 505 and every other status are final), connection errors, and a lost session (the CLI logs in again before the next attempt). A failed login during the retry is final when the gateway rejects the credentials or the session: no further attempts are made and that error is the last error reported. The same holds when a re-read is bounced to the Login page and the gateway refuses the client's automatic re-login inside that read: the re-read stops immediately, with no further attempt and no further wait. A connection error or a transient status (408, 429, 500, 502, 503, 504) during the login is retried like any other attempt. A page-level 401/403, any other 4xx, a full session pool, and a Page-not-found answer are not retried; a Page-not-found answer, like a Login-page answer the client's own automatic re-login did not cure (see above), is final after that attempt. A re-read answered by a page that cannot be read as the form (a "Please wait" document or any page without a form control, or a page the parser cut at one of its bounds) is retried the same way; if it is still unreadable after the last attempt it is the same unverifiable result as below, with the reason in the warning, never a mismatch with an `<absent>` live value. A readable page that lacks the requested field, or shows another value, remains a mismatch (exit 1). An acknowledged save whose verification re-read could not be completed after the retries (session lost, page unavailable, connection error) reports `outcome: applied`, `committed: true`, `verified` absent and a warning naming the attempt count and the last error, and exits 2: the write reached the router but its result is unknown, so scripts must check `committed` before re-running the save. A `No changes detected` save in the same situation also exits 2. Whenever a re-read ran, the JSON result carries `verifyAttempts` (1 on first-try success) so scripts can see that retries happened; it is absent when no re-read was performed (an applied `submit` that needs none, LAN address moves, a `set` whose only requested values are state-verified `ipalloc` reservations). On every page, with or without a Save control, a `No changes detected. Save not performed.` answer is a no-change answer decided by the live re-read, as described above and below, never a router rejection. A write POST answered by a redirect to the login page (or by the Login page itself after the client's one automatic re-login) is a lost session: exit 2, nothing is re-sent and the cached session is dropped. A rejection banner rendered inline in a 200 answer on a Save-less page is reported like the banner after a redirect: `router rejected the change: <banner>`, exit 1. A connection error during a save (while posting, or while waiting for the acknowledgement) produces the same structured `failed` result with exit 2 on every page, Wi-Fi included: `writeAttempted` says whether the POST went out and the warning carries the error text plus the "sent once; verify the gateway state" guidance when it did. Save results carry three write evidence keys: `writeAttempted` (the save POST was sent), `writeResponseReceived` (the gateway answered that POST, with any status) and `acknowledgementObserved` (`Changes saved` was seen); a key whose answer is unknown is absent. `statusCode` is the save POST's HTTP status when a POST was sent and answered; when the save failed before any POST, it is the status of the failing read before it (for example the nonce read) and `writeAttempted` is `false`. When the write was re-sent after a Login-page answer, `statusCode` and `writeResponseReceived` describe the last configuration POST only (a later GET's error is never shown as the POST status), and the JSON carries `writeAttempts` whether the re-send was answered successfully or not (the value is 2 after a re-send; a failed write whose transport raised after a single POST may report 1, while an acknowledged or timed-out single POST carries no key), on every result shape (success, acknowledgement timeout, rejection, a lost session or full pool while reading the answer or verifying, a diagnostic); every sentence that says how often the change was sent says "sent 2 times" instead of "sent once". For a Wi-Fi save that goes through the Warning `Continue` confirmation, `writeAttempts` is the larger POST count of the Save and the `Continue`, the maximum over the step's POSTs and not a sum (a re-sent Save with one `Continue`, or one Save with a re-sent `Continue`, both report 2, and so does a step whose Save and `Continue` were both re-sent); a single POST that succeeds carries no `writeAttempts` key; the Wi-Fi and LAN saves report the same two keys and the last POST's status as every other page. A failed Wi-Fi Warning `Continue` confirmation keeps the save POST's 302 as `statusCode` and reports the `Continue` failure in the warning. The LAN page keeps its `reconnect-required` answer for an address change. Pages without a Save button, outside `services` and `apphosting`, report `applied` on any 2xx/3xx answer with no acknowledgement wait. An inline 200 answer is still read and its full save notification reported: `Changes saved` in it sets `acknowledgementObserved: true` and `writePerformed: true`, and a rejection banner in it is a rejection with exit 1. `No changes detected. Save not performed.` on such a page (inline or on the redirect target, with or without the error icon) is not a rejection: the result is `unchanged` with `committed: false` and `writePerformed: false`, `set` re-reads the page (a match is exit 0 with `verified: true`, a difference exit 1 `failed`, a re-read that fails exit 2), and `submit` with nothing requested exits 0. `action ... --commit` answered that way is `unchanged` with exit 0 (`committed: false`, `writePerformed: false`) on every action path. `set` on such a page is still re-read (up to 3 attempts, as above), and a re-read that cannot be completed exits 2 with `committed: true`; `submit` skips the re-read, so `submit <page> <non-Save button>` exits 0 unverified, which is intended for diagnostic buttons. `submit` refuses a button the page renders disabled, and `set` refuses to press a `Save` the page renders disabled, before any POST (usage error, exit 1, nothing sent). The advisory that a page has no Save button stays in the `warning` of a `set` result that did not commit (a rejection, a failed save), not only of one that did.

`set`, `submit` and `action` refuse a page that answers without any form control (a "Please wait" document, a truncated body), on any page, as a failed read: exit 2, structural, nothing is posted; a button counts as a control. `set` additionally needs a settable control on the page (an input other than a button or hidden input, a select or a textarea): on a button-only page it is a usage error (exit 1, nothing posted, also without `--commit`) that names the page and points to `submit` or `action` for its buttons. Every `action` (plain, other-form and in-form button alike) reads its source page first and applies the same check, so a "Please wait" answer is refused before any POST; the restart and reset family still skips only the answer read. Before a write POST the client also re-reads the page the nonce comes from and refuses, before anything is sent, a Login, Page-not-found, parser-cut or control-less page and any page that yields no nonce for the target form (for the selected form when a page has several forms); `set`, `submit` and `action` report this as exit 2, and so does a full pool or a refused login before the write, with the same evidence on every save path (generic, Wi-Fi, LAN move; a structured result or a raised pool-full/auth error): `writeAttempted`, `writeResponseReceived`, `writePerformed`, `acknowledgementObserved` and `committed` all `false`, and the sentence "The request failed before any write was sent; nothing was changed." A page the parser cut at one of its bounds (`truncated: true`) is a failed read for `set`, `submit`, `action`, the diagnostics plan, `dump`, `diff` and `restore` (exit 2, nothing posted, nothing written, and no removal is planned from it), except the documentary packet-filter page, which is a warning and is left out (see `dump`); `page` and `--json` still show it with `truncated: true` and a note on stderr. An acknowledged save whose requested state is not visible is committed and decided by the live re-read for `set` and `submit` alike: exit 1 when the page reads back differently, exit 2 when it cannot be re-read. Opener detection uses the button the argument resolves to (name, value or label) and includes Packet Filter's add-rule buttons; a `submit` button argument that matches more than one button is a usage error listing the candidates, and nothing is posted, unless exactly one of them has a `name` that matches the argument exactly (case-sensitive), which is then chosen. Buttons that only open an editor (`submit ipalloc Allocate_<mac>`, `action packet-filter-add-drop-rule`, `action packet-filter-add-pass-rule`) report `committed: false`, `outcome: opened`, exit 0, with a note that nothing was saved; the `action` and `submit` results carry the same evidence (`writePerformed` false, `acknowledgementObserved` false). An opener button (`Allocate_<mac>`, the Packet Filter add-rule buttons) refuses KEY=VALUE assignments on `submit` (usage error, exit 1, nothing posted, dry run included); open the editor first, then use `set`.

Advanced Wi-Fi saves go through the gateway's "Wi-Fi Warning" confirmation page automatically: `restore` follows the redirect and posts its `Continue` button to the owning form's action with the nonce carried by the warning page itself (a warning page whose `Continue` form has no nonce is refused and `Continue` is not posted). The owning form's action must be `/cgi-bin/<page>.ha` or a relative `<page>.ha` with no query string; when no form owns the `Continue` button, or its action is anything else, `Continue` is not posted: the step fails (exit 2 for `set`, `submit` and `restore --commit`) with the Save's evidence kept, because a `Continue` posted to the warning page itself is answered 302 and the gateway discards the change. So a `wconfig` step marked `applied` means the change was confirmed, not just submitted. `bgwcli` never releases a reservation, and releasing one for a device that is currently offline may not take effect in the gateway until that device reconnects.

Both ambiguous and unknown device labels make `dump` fail on purpose, and the error says what to do. Ambiguous (two devices both called "watch"): rename one of them in the gateway first. Unknown (the forwarded device is not in the gateway's device list at all, e.g. it is offline): reconnect the device or delete that forward in the gateway UI, then re-run `dump`. A dump must never contain a forward whose device could not be resolved to a MAC - `restore --prune` would read it as both missing and extra and delete the live forward the dump was meant to preserve.

Because `Remove_<n>` buttons address table positions rather than stable ids, `restore --prune` defers the removes on any page that still has an addition to make, and performs at most one real remove per page per run. A dump that both adds and prunes therefore converges over two commands: `bgwcli restore <dumpfile> --commit --confirm RESTORE`, then `bgwcli restore <dumpfile> --prune --commit --confirm RESTORE`. When several router-only rows share a page, that second command removes one of them per run: repeat `--prune --commit --confirm RESTORE` until the closing diff reports no differences (each run re-reads the page, so the shifted positions are always current).

`--prune` never removes a custom service that a NAT/Gaming forward still uses, unless the same run removes every such forward first; the remove is `blocked` with that reason otherwise. `--prune --include services` therefore also reads the NAT/Gaming page (it is not compared or written), and without it the remove is blocked as "forwards not inspected". A dumped custom service whose ports or protocol differ from a router service of the same name (compared case-insensitively) is never added as a duplicate: without `--prune` the add is `blocked` with that reason; with `--prune` restore removes the router's service and then adds the dumped one in the same run, and the forwards that use it are added by the next restore run. Adding a NAT/Gaming forward is `blocked` when its device label is shared by another device in the router's device list, because the forwards table identifies devices by label only; rename one of the devices and run restore again.

## Choosing what to back up and restore

`dump` always captures the sections `diff`/`restore` act on (custom services, NAT/Gaming forwards, fixed
reservations) plus the two **core** form pages, Firewall Advanced (`dosprotect`) and Advanced Wi-Fi
(`wconfig`). Four form pages are **optional** and only captured when named with `--include`:

| Page id | Router page | Why it is opt-in |
| --- | --- | --- |
| `etherlan` | LAN Ethernet ports (speed/duplex, MDI-X per port) | Forcing a port mode can cut the wire you are connected through. Its write path has not been exercised live; run a first commit from a client that is not on the port being changed. |
| `dhcpserver` | Subnets & DHCP (LAN address, mask, DHCP range, lease) | A different LAN address moves the gateway; the closing diff cannot re-fetch it. Still restored **last**, and the step carries a `warning:` line with the new address. |
| `ippass` | IP Passthrough mode | Changes how the WAN address is handed to a LAN device. |
| `wmacauth` | Wi-Fi MAC Filtering modes (allow/deny/none per network) | A `deny`/`allow` mode restored before the filter list exists can lock Wi-Fi clients out, so restore blocks a non-`none` mode while the live filter list lacks any dumped entry (or the dump recorded no list); re-enter the list in the gateway UI and run restore again. Setting a mode back to `none` is never blocked. The list itself is documentary. |

```
bgwcli dump --out ~/bgw-baseline.json                           # core only
bgwcli dump --include dhcpserver,ippass --out ~/bgw-baseline.json
bgwcli dump --include all --out ~/bgw-baseline.json             # every page: the factory-reset baseline
```

Pages not included are not fetched, and a page the dump did not capture is never compared or written:
`diff` and `restore` act on everything present in the dump. `--include` on `diff`/`restore` narrows that to
the listed page ids - `services`, `apphosting` (forwards), `ipalloc` (reservations) and the form page ids
(`all` or no flag = everything in the dump):

```
bgwcli diff ~/bgw-baseline.json --include services,apphosting
bgwcli restore ~/bgw-baseline.json --include dhcpserver          # dry-run of that one page
```

An explicit selection scopes both reads and extraction for `diff`, `restore`, and `autorestore`.
Unrelated unavailable pages or malformed tables do not block the selected sections. Forwarding
still uses the device and service dropdowns on `apphosting` to resolve MAC addresses and plan adds.
Scoped reads omit system information, so they do not report a firmware change without observing it.

A requested page the dump never captured is reported, not guessed at: `diff` and `restore` print
`warning: page '<x>' is not present in the dump; nothing to compare/restore` on stderr, the restore plan
shows a `skip` step for it, and `--json` output lists it under `missingPages`. An unknown page id is a
usage error (exit 1) before the router is touched. Exit codes are unchanged: `diff` 0/1/2, `restore` 0 once
everything selected from the dump is present on the router.

Live-only form fields remain visible in `diff` but do not prevent restore convergence, even with
`--prune`; a missing or different value captured in the dump still prevents convergence.

A checkbox/radio the dump recorded as `<unchecked>` is not a difference when the live page does not
render that control or renders it disabled: neither posts anything for it, which is what "off" means.
A dumped value for a control the live page renders **disabled** is reported by `diff`, but a browser
never posts a disabled control and its saved value can never be read back. When the same page has a
change to an enabled field, the dumped value is posted with that save (it is often the change that
re-enables the control, for example a Wi-Fi password behind the security mode) and checked after the
save only if the page then renders the control enabled. Without such a change the plan shows a
`blocked` step naming the disabled controls. Either way it does not hold restore convergence open,
and a dump that differs from the router only in disabled controls is not a reset for `autorestore`.
A name counts as disabled only when every control with that name is disabled: a radio group with one
disabled member is restored and verified like any enabled field.
A dumped `<unchecked>` for a control that is not a checkbox or radio (a text field, a select) cannot be posted as "off": it is reported as a `skip` note and never sent. The note names the field and never quotes its value (the field may be a secret), in the text plan and in `--json` alike.

## Automatic recovery (autorestore)

`bgwcli autorestore <dumpfile>` is the factory-reset runbook above as a watchdog: run it from a systemd
timer on an always-on LAN host (a Raspberry Pi) and the configuration comes back by itself within
minutes of AT&T resetting the gateway, without you noticing the Wi-Fi went away. It fetches the same
pages `diff`/`restore` use, diffs them against the dump and only then decides whether to act.

**What triggers it.** A run counts as a factory reset only when the difference is total loss:

- every dumped custom service is missing **and** every dumped NAT/Gaming forward is missing **and**
  every dumped reservation is missing (a section the dump has no entries for does not vote; a dump
  with no sections at all falls back to "every dumped form page differs"), or
- an unfinished recovery of the same dump is recorded (see Recovery checkpoints below).

A reset reverts the gateway to the printed sticker code, so a login that needed
`BGW_FALLBACK_ACCESS_CODE` is named in the reason when one of those holds. It is not a reset on its own:
when the gateway still holds the dump's configuration the run reports `no-reset`, sends nothing and
carries a warning (JSON `warnings`, and a `warning:` log line) to set `BGW_ACCESS_CODE` to the gateway's
current code.

`--on-any-diff` widens that to "any difference restore can act on"; use it only if nothing is ever changed from the UI.
Differences no restore pass can change never count, with or without it: router-only entries (autorestore
never prunes), controls only the live page renders (newer firmware) and dumped controls the live page
renders disabled. Such a run reports `no-reset: nothing restore can act on differs`.

**What it never does.** It never uses `--prune`, so router-only entries survive every pass. It never
acts on ordinary drift without a matching unfinished recovery: one service you deleted by hand,
an edited firewall flag, a re-addressed reservation all report `no-reset` (exit 0) with the diff, and are left exactly as they are. It never
restores anything the dump did not capture, and `--include` narrows it like `diff`/`restore`. It
never reboots the gateway, changes the access code or touches packet-filter rules.

**Recovery checkpoints.** Before the first recovery write (including a required Clear), the CLI
atomically saves intent to `$XDG_STATE_HOME/bgw/recovery/<router-hash>.recovery.json`, or
`~/.local/state/bgw/recovery/<router-hash>.recovery.json` when `XDG_STATE_HOME` is unset. The directory
is owner-only (0700), and the file is owner-readable/writable only (0600). It contains the canonical
router origin, normalized selection, schema version and a fingerprint of the selected desired
configuration: no credentials, backup contents or POST payloads. Capture timestamps, firmware
metadata, documentary tables and forward display labels do not affect matching. File contents and
directory entries are synchronized before recovery writes. The durability boundary is the existing
state base (`XDG_STATE_HOME` or `~/.local/state`): owned `bgw/recovery` entries and newly created
base directories are synchronized, without fsyncing unrelated existing home/mount/root ancestors.
Existing application entries are synchronized on every begin: they may remain from an interrupted
mkdir before its parent was synchronized, and they have no pending marker proving publication.
Validated existing directories skip mkdir, and already-correct permissions need no chmod.
Existing base permissions are preserved; newly created base parents retain the umask's group/other
permissions with owner access restored (0700), while the recovery directory stays private. A private `.bgw-publication-<hash>.pending` sibling
marks each new base directory until its parent has synchronized. A failed publication leaves this
marker for the next invocation to retry, even if another process adds children to that directory.
If creation stops before permissions are complete, retry restores owner access only for a validated
pending base entry or an owned application directory; unmarked existing base permissions remain unchanged.
Each parent is published before creating its children; retries inspect the pending marker at each
entry they ensure, without checking unrelated ancestor publication markers. Markers must be owned private (0600),
single-link regular files with the matching physical directory record. Canonical paths and safe
legacy version-1 aliases identify the same directory, including retries before its creation;
owned empty markers from earlier versions remain compatible. Owned marker and checkpoint file
descriptors are set to 0600 explicitly so restrictive umasks cannot make them unreadable. Foreign
or replaced markers are refused without mutation, including
symlinks and their targets. Required synchronization failures stop
writes; checkpoint removal also synchronizes its directory. Cleanup failure after
verified convergence retains the pass evidence and verified final diff.

The state base and its ancestors may be symlink aliases. The application's `bgw` and `recovery`
directories, or a custom library checkpoint root, must be real directories: checkpoint publication
and removal refuse application symlinks with a descriptive error and preserve their targets.
On older Linux systems without no-follow chmod support, permission recovery uses a validated
directory descriptor through `/proc/self/fd`; procfs must be mounted and accessible. If that
fallback is unavailable, recovery stops without changing the directory through its original path.
This fallback accepts concurrent permission updates to the same owned directory and preserves
the current group/other bits when repairing base/parent access. The checkpoint directory is set
to exactly 0700 without following a replacement symlink. An explicitly supplied custom checkpoint
root is the private directory, including `.`: its permissions become 0700 even when it coincides
with the state base. Existing ancestor permissions remain unchanged. The filesystem root, including
equivalent path spellings, is refused as a checkpoint directory before any directory changes.

An unfinished matching recovery resumes on the next invocation even if only some entries remain
missing. Every invocation fetches fresh pages and rebuilds the remaining plan; it never replays
saved POSTs. A different router, desired configuration or selection cannot inherit this intent. While one unfinished recovery is recorded for a router, a run with a different dump or page selection that needs to start its own recovery refuses with exit 2 (`error`) and leaves the other record in place.
Read-only ownership failures, fixed-owner refusals, and blocked or empty plans do not create intent.
New intent is published only when an eligible Clear or executable restore plan is ready. It is removed
after verified convergence, or when this invocation can positively establish that no mutation was
attempted; earlier recovery intent and ambiguous attempts are retained. A crash between durable intent
and the first gateway write remains possible: the local checkpoint and gateway are not one atomic
transaction. Failed writes still stop the current invocation; a later invocation can resume after
reading fresh state. Dry-runs neither start nor
remove a checkpoint. A missing or corrupt record never authorizes recovery (a corrupt one is treated as no record and
may be replaced); a record that exists but cannot be read (for example wrong permissions) is unknown
recovery state, so the run stops with exit 2 (`error`, `recovery checkpoint read failed: ...`) after the
read-only diff. Failure to publish intent stops the command before any recovery write.

To intentionally abandon unfinished recovery, stop the timer and any active invocation, inspect the
record's `origin` to identify the router, and remove that router's `.recovery.json` file. Subsequent
runs use reset detection normally; a complete reset or `--on-any-diff` can still start a new recovery.
For library calls, `run_autorestore(..., checkpoint=...)` accepts an optional store; callers supplying
one must serialize access, as the CLI does through its router session lock.

**How it runs.** Dry-run by default: a detected reset prints the restore plan and exits 1, nothing is
sent. With `--commit --confirm RESTORE` it runs `restore` passes - services, forwards, reservations,
then the form pages with Subnets & DHCP last and its warning intact - re-diffs after each, sleeps
`--wait` seconds (default 120) between passes while devices reconnect and their forwards become
restorable, and stops after `--max-passes` (default 3). The access code is resolved as for every other
command (`BGW_ACCESS_CODE`, `--access-code-stdin`); `BGW_FALLBACK_ACCESS_CODE` is only tried after the
primary one is rejected. That includes a cached primary session the gateway no longer honours: the
first page read is bounced, the primary code is refused on the re-login, and the run drops the dead
session and logs in as an uncached run would (the primary code, then the fallback). Without a usable
fallback code (unset, or equal to the primary) a rejected code ends the run with exit 2, as on every
other command. When the fallback code logs in, its session is the one cached, so the next run reuses
it without logging in and reports `usedFallbackCode: false`; reset detection on that run comes from the
diff or an unfinished recovery. Once a reset is detected, the same allocation conflict preflight runs before
the first restore pass, including the conditional Clear and Rescan and the wait described above.
`autorestore` ends the run on any fault before a configuration POST (a lost or refused session, a page-level 401/403 on the nonce read or on the initial snapshot read, a full session pool, a structural failure): the run ends as an error with exit 2 and a reason naming the failure, with no further pass, no inter-pass sleep and no second login attempt; such a fault is not counted unless the run already sent a write or the failure is a refused nonce page (below), and the recovery intent is kept. A transport fault before the first POST of a run that has sent nothing stays router-unreachable with exit 0 (once the run has already sent a write it ends the run `not-converged`, exit 1, and is counted, as described below). A refused nonce page (the client's Login, Page-not-found, "Please wait", parser-cut, control-less or no-nonce answer for the page a write reads its nonce from, the allocation Clear included) is structural, not transport: the run ends as an error with exit 2 and the reason names the page and what was refused, nothing is sent (`writeAttempted: false`, `writePerformed: false`), it counts toward the three-run limit against the recovery intent when one exists (with no recovery intent there is nothing to count against, so the run only exits 2), and it is never retried quietly as router-unreachable. A full pool before any write records the pool cooldown. No rescan runs for ordinary drift or during a dry-run. A write the gateway acknowledged is never posted again within one invocation: a later pass that still finds that page or entry different reports it as a `skip` step ("acknowledged in pass N; not re-posted in this run") and ends `not-converged`, and only blocked, deferred or never-posted steps are retried. Only a run in which a configuration POST was sent or may have been sent, or whose failure is structural (a missing table, the Login page, `Page not found`, a form without controls), counts toward a three-run limit, whichever way it ended: a write that got no answer, a closing read that could not be completed, or a `No changes detected` answer with the page still different. A fault before any POST (for example a nonce read timeout) neither increments nor resets the count and keeps the recovery intent, and an acknowledged LAN move that requires reconnecting does not count either. The counter (with a fingerprint of the failure that holds no values) lives in the recovery intent, which is therefore kept after any sent write, `No changes detected` included, and a run whose failure has a different shape restarts the count. The run that reaches the limit still reports `writeUnanswered: true` when its write got no answer and ends with exit 2 (`error`); later committed runs of that stopped recovery send nothing and exit 2 naming the intent file; remove it, as described above, once the gateway has been checked by hand. Verified convergence clears the count. Each failing run's reason carries its count (`failure n/3 of this recovery`). A pass that sent nothing and failed nothing starts the next pass without the `--wait` pause. An unreachable router (a connection error or an HTTP
error answer) during the login or the initial snapshot fetch exits 0 with one log line so the timer stays quiet, and so does one
during the ownership preflight's reads before its Clear is sent; an ownership preflight that cannot be
verified for any other reason reports an error. Autorestore makes no closing read after a full session pool or a session-wide auth failure on a write step. A transport fault (connection error, timeout, HTTP error answer) before any POST in a run that sent nothing ends the run as `router-unreachable` (exit 0) without further passes or sleeps and without counting toward the limit. An acknowledged write whose state read then fails with an HTTP or transport fault, or keeps answering a page that cannot be read as its form or table until the save deadline (a "Please wait" document, a cut page, a Login or Page-not-found answer), ends the run as `error` (exit 2), `verification-unavailable`, with `writeUnanswered` false, and counts toward the limit.

| Exit | Status | Meaning |
| --- | --- | --- |
| `0` | `no-reset` | Router matches the dump, or differs in a way that is not a reset. Nothing sent. |
| `1` | `restore-needed` | Dry-run: a reset was detected; the printed plan is what `--commit` would send. |
| `0` | `converged` | `--commit`: everything in the dump is back on the router. |
| `1` | `not-converged` | `--commit`: passes exhausted with entries still missing, or a transport fault before a step's POST in a run that had already sent a write; the next timer run re-reads the gateway and retries, up to the three-identical-failures limit. |
| `0` | `router-unreachable` | Connection error or HTTP error answer, on a read before anything was written (including the allocation preflight's reads before its Clear is sent). An answer that is not the page asked for, and a page-level 401/403 on the initial read, are not this status (see `error`). |
| `2` | `error` | Allocation preflight could not complete after its Clear was attempted, or failed for a reason other than an unreachable router (a connection error or HTTP error answer before the Clear was sent is `router-unreachable`), the gateway answered a read with something that is not the page (the Login page, a `Page not found` document, a table page without its header row, a form page without any control), pages could not be re-read after a write, a write went out (or may have) and got no answer (a timeout, an HTTP error status, a refused session, a full session pool after the write or an answer without `Changes saved`; `writeUnanswered: true` on every such exit, and the reason names the write, even when the closing diff is readable or the closing read failed), the recovery checkpoint could not be read or written, another unfinished recovery is recorded, or the same failure ended three consecutive runs of this recovery (later committed runs of it send nothing); check by hand. A full session pool before any write is reported as `sessionPoolFull` with a reason that says nothing was written. |

`--json` prints `status`, `reason`, `exitCode`, `detected`, `usedFallbackCode`, `writeUnanswered`, `warnings` (when set), `missing` counts per
section at detection time, one summary per `passes` entry (`applied`/`blocked`/`failed`/`skipped`/
`notRun`/`converged`), the redacted final `diff`, the `plan` and `missingPages` like `diff` does.
When IP ownership conflicts are found, `allocationPreflight` includes the holder/target MACs,
the discovered Clear payload, its availability, `clearAttempted`, `clearResponseReceived`,
`rescanPerformed` (Clear accepted), and `ownershipVerified` (both sources subsequently validated).
A failed committed preflight reports a blocked incomplete plan and keeps this mutation evidence;
transport ambiguity asks you to verify gateway state before retrying. A custom client without
transport-attempt/response evidence reports `clearAttempted: null` and
`clearResponseReceived: null` when its Clear call fails. Pool-full failures also publish session
coordination metadata so the local cooldown protects the next invocation; a full pool during the ownership rescan after Clear ends the rescan at once (exit 2, cooldown recorded) even if a later read would have succeeded, while connection, HTTP and unverifiable-page reads are still retried within the 180-second window. A dry-run whose
ownership depends on a rescan reports `planStatus: "pending"`, `planComplete: false`, and `plan: null`;
a fixed conflict, unavailable Clear, or unverifiable ownership reports `planStatus: "blocked"` with
a structured reason. These outcomes do not claim a completed zero-step plan.
If closing verification fails after writes, the command keeps its pass evidence, returns exit 2,
and reports `diff: null` with `converged: null` for the unverified pass. `restore --json` likewise
retains `execution` and reports `diff: null`, with `verificationFailures` for unavailable pages or
`verificationError` (type and message) for authentication, transport or extraction exceptions.

Install as a user unit on a Raspberry Pi with [deploy/README.md](../deploy/README.md) (`deploy/bgw-autorestore.service`,
`deploy/bgw-autorestore.timer`, `deploy/autorestore.env.example`). The timer unit carries `[Install] WantedBy=timers.target`, so `systemctl --user enable --now bgw-autorestore.timer` keeps it enabled across boots, not only for the current session. The one habit that keeps the watchdog
from fighting you: run `bgwcli dump --include etherlan,dhcpserver,ippass --out ~/bgw-baseline.json` after every
deliberate change, so the baseline is always the state you want back. The timer's baseline leaves out
Wi-Fi MAC Filtering modes (`wmacauth`): a reset also wipes the MAC filter list, which is never restored,
so a dumped `allow`/`deny` mode would be a blocked step on every run. `autorestore` stops starting steps, passes and inter-pass sleeps once its own run limit of 1800 s has passed, the first step included, and the allocation Clear and Rescan (a gateway whose initial reads used up the limit gets no write at all). The limit is re-checked right before every write starts: after the recovery checkpoint is prepared (before the Clear, and on every pass before that pass's first step, so a checkpoint first published in a later pass is covered too) and after the inter-pass sleep (before pass N+1), so slow checkpoint publication or an overrunning sleep cannot push a write past it; a step that already started finishes its own save. The run then ends not-converged, exit 1, with an earlier unfinished recovery intent kept (a fresh intent that nothing used is discarded), unless the run's own allocation Clear was already sent and the refreshed read after it already shows the gateway converged: that run ends converged, exit 0, and counts nothing. The service unit allows 2580 s (`TimeoutStartSec`): the 1800 s limit, plus the one step that may be in flight (15 s nonce read + 15 s POST + 60 s acknowledgement window), plus 555 s as an upper bound for the closing fetch (the 11 snapshot pages, 7 always plus up to 4 optional form pages, at the 15 s page timeout; the figure is kept generous and keeps the 2580 s arithmetic unchanged), plus a 135 s margin. `autorestore` ends the run on any fault before a step's POST once the run has already sent a write, transport faults included: no further pass and no inter-pass sleep; the run ends not-converged (exit 1) with the sent writes counted, the closing verification read is still attempted (a failed closing read is the counted verification failure), and the recovery intent is kept. The unit sets `PYTHONUNBUFFERED=1`, and the progress lines and step lines are flushed as they are written, so a kill at `TimeoutStartSec` or a power loss loses at most the line being written.

A worked example, the run after a reset (the Wi-Fi devices are still offline in pass 1, so their
forwards and reservations wait for pass 2):

```
$ bgwcli autorestore ~/bgw-baseline.json --commit --confirm RESTORE --wait 60
access code reverted; factory reset suspected
factory reset detected: access code reverted; factory reset suspected; services 2/2 missing; forwards 2/2 missing; reservations 2/2 missing
pass 1/3: 9 steps
[1] applied services (302)
[2] applied services (302)
[3] applied apphosting (302)
[4] blocked apphosting (device host-a not in NAT/Gaming device list)
[5] applied ipalloc (302)
[6] blocked ipalloc (Allocate button for 02:0a:0b:0c:0d:03 not present on IP Allocation page)
[7] applied dosprotect (302)
[8] applied wconfig (302 -> /cgi-bin/wconfig.ha)
[9] applied dhcpserver (302)
pass 1/3: 7 applied, 2 blocked, 0 failed, 0 not run
not yet converged; waiting 60s before pass 2
pass 2/3: 3 steps
[1] skipped packetfilter
[2] applied apphosting (302)
[3] applied ipalloc (302)
pass 2/3: 2 applied, 0 blocked, 0 failed, 0 not run
converged after pass 2
No configuration differences.
Firmware differs between dump and router.
$ echo $?
0
```

The same command on an untouched router prints `no-reset: no differences` and exits 0; after you remove
one service by hand it prints `no-reset: services 1/2 missing; forwards 0/2 missing; reservations 0/2
missing` plus the diff, still exit 0, still nothing sent.

## Page views and `--limit`

All of these commands accept `--json`. Parsed page JSON includes a `summary` object with the same high-value fields used by the terminal view, plus the underlying values, tables, controls, buttons, and forms. Use `--forms` to include form controls in normal terminal output.

`--limit N` (default 20) is applied by the terminal renderer only, everywhere: it bounds every table and every button listing of a page view (the `--forms` control listings stay complete), the device list, the composite status views and the `logs` listing, and each cut block ends with `... K more rows. Use --limit T to show all.`, where `T` is that block's full row count. `--json` output is never truncated by `--limit`.

`device status`, `home-network status` and `firewall security-options` are composite views with their own fallbacks (see [Sweep](#sweep)), so their JSON is a status result rather than a parsed page and carries no `summary`: `page`, `fallback` and `sections` always, plus `statusCode`, `title`, `parsed` (the primary page's parsed JSON when it answered) and `error` when set. Each `sections` entry has `page`, `ok`, `values`, `tables` and, when set, `title`, `heading` and `error`.

`home`, `lan` (for `home-network`) and `diag`, `diagnostic` (for `diagnostics`) are accepted as section aliases, as in the TypeScript CLI.

## Generic Inspection And Forms

Use `inspect` or `--forms` when the router page changed and you need to see what the CLI discovered:

```bash
bgwcli inspect "Diagnostics/Troubleshoot" --forms
bgwcli home-network wi-fi --forms
```

Readable page output shows available buttons and control counts by default. Form data is kept as the page wrote it: a textarea value keeps its line breaks and spacing (only the newline HTML drops after `<textarea>` is removed), input and option values keep non-breaking spaces, and input `type` is lowercased. In the `values` map, table, heading and description text is whitespace-normalised and a `Field <name>` entry for a textarea is its whitespace-normalised text, but a `Field <name>` entry for an input or select carries the raw value exactly as it would be posted (a non-breaking space stays); display labels are whitespace-normalised. JSON includes parsed values, duplicate-preserving `valueEntries`, tables, fields, select option labels/state, textareas, buttons, forms, links, and disabled/readonly metadata. Use `--forms` to show those preserved details in terminal output.

Redaction is decided once per document from the laid-out tables and controls, and then applied to every text the parser derives, so a secret cannot come back through a second view of the same markup. Every value cell of a row whose label (first column) names a secret is redacted, whether that label is the row's own cell or a `rowspan` cell carried down from an earlier row, and whatever the row's width; so is every cell under a secret column header of a table of any width (the header row itself keeps its names: a row of header cells, or in a two-column table a plain word above a secret-named column such as `Name | Password`, names columns; a header cell beside a data cell is a row label), the text a secret label cell holds itself (`Access Code Default: <value>`, `Password: <value>`, which keeps the label), the text of a secret `<select>`, `<textarea>` or `<button>` (option labels included) wherever it is copied into an enclosing cell, and everything inside a redacted cell, including a table nested in it. The enclosing cell, `<div class="desc">` description, heading (`<label> Currently <value>`) and link label therefore show `[redacted]` for those parts, a `?password=` style query value in a link target is redacted by its name, and `--include-secrets` shows all of it. A secret cell spanning rows and columns at once keeps its colspan marks in every row it covers, so the continuation row is as excluded from records as the row it starts in. A control (`input`, `select`, `textarea`) in a value cell of a secret-labelled row is redacted by that label as well as by its name (value, options and option labels) and is marked `sensitive`; so is a control inside a label cell that names a secret (`<td>Wi-Fi Password: <input ...></td>`, with or without a value cell after it), so is a button (an `<input type="button|submit">` value or a `<button>` value and caption, and any name taken from them) there, whose value and label read `[redacted]`, and a link or form there has its target replaced by `[redacted]` wholesale, an untexted link's `title` is replaced by `[redacted]` (links have no `sensitive` field), and a link there is not listed by `sitemap` or the sitemap page. Labels, headers and records derived from text inside a redacted cell are not read (a table nested in a secret cell yields no value entries). A table wider than two columns whose first row has no `<th>` cell and whose first cell names a secret is read as a labelled data row (fail closed), not as a header row, and a narrow header row whose first cell names a secret also governs its own values. A row with no cells stays on the column grid, so a `rowspan` cell reaches over it as it does in a browser. A query parameter name is judged percent-decoded (`%70assword` is `password`). A heading left unclosed names its section by its own words, never by the table that follows it, and a form's `action` redacts a secret query parameter like a link target. Field names containing `pwd`, `community` or `authkey`, and names with `pw`, `pass` or `token` as a whole word or suffix (`ap_pw`, `ssid_pass`, `csrftoken`), are redacted like passwords; `bypass`, `passive`, `pass_through`, `power` and `tokenizer` are not.

Tables are read on their column grid: a `colspan` header names every column it covers (`Name`, `Name 2`, ...), a `rowspan` cell's value is carried into the rows it covers, a data row with a `colspan` cell is not a record, a further row made only of `<th>` cells (a second header row) is not a record either, and neither a header cell spanning columns nor a row continuing an earlier `rowspan` cell is read as a label/value pair. A `<select>` start tag ends a `<select>` left open, so an unclosed select cannot absorb the next one's options. Deeply nested or unclosed markup is walked iteratively, without Python's recursion limit. The content of `script`, `style`, `textarea` and `title` is read by the parser itself (character references decoded for the last two), so a page parses the same on every supported Python. Hostile bodies are bounded (the client already caps a body at 4 MiB): a `colspan` or `rowspan` above 64 is read as 64; a table's grid is capped at 250 000 cells and its later rows are dropped; a document of more than 150 000 elements is read only up to that point; tables or headings nested deeper than 256 stop being structure; one element's text is read to 5 000 pieces or 100 000 characters; and a stray end tag costs nothing. Each of those five cuts (element cap, grid cap, span clamp, nesting drop, text cut) marks the page `truncated: true` in the parsed JSON, and every bound fails closed for redaction: every control, button and link after the last laid-out row of a cut grid is redacted and `sensitive`; a table with a clamped span that names a secret in its first row or column has every cell governed; a cell whose text was cut counts as secret-named. A truncated page is a failed read for `devices` (it falls back to `ipalloc`, and a truncated fallback reads nothing), `logs` and the save-notice readers (they raise instead of returning part of the page), and for `sweep` (the page is not `ok`, with a reason and `truncated: true` in its receipt). No router page comes near any of these bounds. A page is taken for the gateway's login page only when its `<title>` or first `<h1>` reads `Login` (or carries `Access Code Required`), or when it holds the login form itself (a form posting to `login.ha` with the `nonce` input and the password field); the same words in body text or help prose, for example on the Access Code page, do not count.

Several configuration pages have page-specific summaries built from current form state so default terminal output stays useful:

- `broadband configure`: source override and MTU values.
- `device access-code`, `device remote-access`, `device restart-device`: current form/action state with sensitive values redacted and restart surfaced as an explicit guarded action.
- `home-network configure`, `home-network ipv6`, `home-network wi-fi`, `home-network advanced-wi-fi`, `home-network mac-filtering`: compact current network form state, including per-port configured Ethernet modes, basic current channel rows, advanced radio/SSID controls, and per-radio MAC-filter state. Passwords, SSIDs embedded as defaults, and WPS PINs stay redacted in fixtures.
- `home-network subnets-dhcp`: gateway/subnet, DHCP range, lease, public subnet, inbound, and cascaded-router state.
- `firewall packet-filter`, `firewall custom-services`, `firewall nat-gaming`, `firewall public-subnet-hosts`: current firewall/NAT form state; NAT/Gaming shows selected service/device and available service/device counts instead of dumping every dropdown option.
- `firewall ip-passthrough`: allocation mode, default server, passthrough mode/MAC, and lease.
- `firewall firewall-advanced`: ICMP, Reflexive ACL, ESP ALG, and SIP ALG toggles.
- `diagnostics update`, `diagnostics resets`, `diagnostics event-notifications`: current action/form state without requiring browser clicks.

`page --raw`, `logs --raw`, `sitemap` and `coverage` apply the same test as the parsed commands: a 200 answer that is the Login page or the "Page not found" document is an unavailable page (exit 2, the error on stderr or in the structured result), never raw HTML, a sitemap or a coverage result. A public read (`check`, `sitemap`, `coverage`) whose answer is the gateway's "all web server sessions are in use" page is a full session pool, not data: exit 2 with the cooldown recorded, even when no session was ever held. `sitemap` and `coverage` also treat an answer with no `/cgi-bin/*.ha` link (blank, a "Please wait" page, an empty body) as an unavailable page: exit 2, never an empty sitemap or `liveCount: 0`. A pool-full seen by one of these local commands records the cooldown best-effort: a lock timeout or filesystem fault while recording is a warning on stderr, and the pool-full error (exit 2, `sessionPoolFull`, `waitedMs`, `retryCount`) is still what is reported.

Pages on this router can hang. Normal parsed page commands report a structured page-unavailable result (exit 2) instead of taking down broader workflows. `scan`, `schema`, and `audit` continue across failures so one broken AT&T page does not hide the rest of the router; a page that answers HTTP 401/403 on its own is recorded as that page's failure and the walk goes on. Only a failed login, a login-page bounce after authentication, or a full session pool stops the command.

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
- `--raw --pages <single-page>` to emit one raw HTML page. A single-page command (`page <id>`, `inspect`, a section tab) with `--raw --json` prints `{"page": <id>, "raw": <html>}` instead of bare HTML; `--raw` without `--json` prints the bare HTML. `--raw` is refused (exit 1) by `audit` and `readiness`.
- `--out <dir>` to write raw HTML and parsed JSON artifacts to disk while keeping stdout compact. `--out` is accepted only by `sweep`, `scan`, `schema`, `dump` and `fixtures-capture`; on any other command it is a usage error (exit 1) before the router is touched, and `audit`/`readiness` never create artifact directories. The output root (and `fixtures-capture --out`) must be a real directory owned by you: a symlinked or foreign-owned root, or an existing artifact directory or file that is a symlink or owned by another user, is refused (exit 2, entries preserved) before anything is written, so a refusal leaves no partial artifacts behind. A symlink in the path above the root (a `/tmp` that is itself a link) is fine. The tree is checked before the walk begins, not after it, every per-page file included (`router-html/<page>.html`, `parsed/<page>.json`, and the fixture `expected/` files): an unusable `--out` is refused (exit 2) before any page is read (`dump --out` refuses an existing directory, a symlink, a path through a regular file or a dangling symlink, or an unwritable directory before any page is read: exit 2, nothing written), and an empty `--out` is a usage error (exit 1) on every command that accepts it.

The `devices` page of a sweep is `ok` when `devices.ha` answered (even with zero online devices) or when the IP Allocation fallback produced devices; a fallback with nothing to show did not answer and the page is reported failed. The fallback counts as read only when `ipalloc.ha` shows the IP Allocation table header (`IPv4 Address / Name`, `MAC Address`): a blank or "Please wait" page without it is an unreadable fallback (`fallbackError`, exit 2 for `devices`), while a header-only table is a real empty list (exit 0). When `ipalloc.ha` could not be read either, the page keeps that reason as `fallbackError` next to `error`, as `devices --json` does. `--delay <ms>` paces the walk between pages; there is no wait after the last page. `sweep --raw --pages <page>` for a page that cannot be read prints `error: page <page> could not be read: <reason>` on stderr, leaves stdout empty and exits 2.

`--out <dir>` and `--raw` write the gateway's raw HTML, which contains nonces, hashed passwords, Wi-Fi keys and SSIDs; artifact files are created with mode 0600, and redaction applies to stdout output only, except `--raw`, whose stdout output is the unredacted page. `sweep`, `scan`, `schema`, `audit` and `readiness` exit 2 when every requested page failed (including when login never succeeded); partial failures are recorded per page and the exit code stays 0.

`scan` is retained as the compatibility command for compact sweep metadata. `schema` is sweep with parsed/form detail. `audit` and `readiness` are sweep plus the health/usefulness summary.

`device status` first tries `home.ha`. If that page hangs, it falls back to a concise summary from System Information, Broadband Status, and Firewall Status. Use the section-specific commands for deeper output such as `broadband fiber-status` or `home-network status`.

`devices` lists online devices by default; `--all` keeps offline (remembered) devices too. It first tries `devices.ha`. If that page hangs, fails, or answers HTTP 401/403 on its own (a failed login, a login-page bounce or a full session pool still ends the command), it falls back to `ipalloc.ha` and returns degraded device records with IP, name, MAC, status, and allocation. Treat `last activity` as a timestamp reported by the gateway, not proof of a disconnect. An HTTP 200 `Page not found` answer is an unavailable page on either read, never an empty list: on `devices.ha` it triggers the fallback, and on `ipalloc.ha` it leaves nothing read; so is a 200 `devices.ha` body that has no device table at all (no `MAC Address` header or key, such as a "Please wait" page), while a device table without rows is an empty list. In the fallback table a bare value that is not an IPv4 or IPv6 address (a host name such as `watch`) is the device name, not an IP, and `devices.ha` rows split `<address> / <name>` the same way (a trailing ` /` is an unnamed device; a name that itself contains a slash stays whole). `devices` exits 2 when the router session is lost during the `ipalloc.ha` fallback, and also when nothing could be read: `devices.ha` failed and `ipalloc.ha` could not be read either (a page-level 401/403 or a failed fetch), in which case the JSON result keeps `error` and adds `fallbackError`. A fallback that was read and lists zero devices exits 0.

`home-network status` first tries `lanstatistics.ha`. If that page hangs, it falls back to LAN configure, Subnets & DHCP, IP Allocation, and Wi-Fi configuration data.

When a composite view (`device status`, `home-network status`, `firewall security-options`) falls back and none of its fallback sections could be read either, nothing was read: the command exits 2, with the JSON result still printed.

`firewall security-options` first tries `securityoptions.ha`. On firmware that advertises that page but returns Page not found, it falls back to Firewall Status and Firewall Advanced.

The router can also refuse login when its tiny web session pool is full. By default the CLI fails fast with a clear error (exit 2) so normal commands do not appear hung. To wait only for that exact condition:

```bash
bgwcli sweep --wait-for-session
bgwcli sweep --wait-for-session --session-wait-timeout 120000 --session-wait-interval 10000
```

The `waitedMs` reported with a pool-full outcome is the wait actually observed (sleeps plus retry round trips), not the configured budget. `check` honours the flag as well: when its session probe meets a full pool it polls the login page within the same budget (still without logging in), then probes the held session once more. Waiting does not retry bad access codes, random connection failures, or parser failures. An active five-minute local pool cooldown still fails fast with `--wait-for-session`; do not clear it simply to force another authentication attempt. `bgwcli session clear-cache` acquires the same per-router lock and is intended only for explicit local-state troubleshooting.

## Timeouts

A request that never connects fails with `Timed out connecting to <url>`; one that connected but whose response did not arrive in time fails with `Timed out reading response from <url>`. The timeout is a total deadline per request: it bounds the connect (every address tried), the TLS handshake, the status line, the headers and the body as well, so a gateway that answers one byte at a time is cut at the deadline. The default request timeout is 15 s. `--timeout <seconds>` accepts seconds, including fractions such as `--timeout 1.5` (1500 ms); the minimum is 0.001 s (1 ms), the maximum is 3600 s, and finer fractions are truncated to whole milliseconds. Values above 3600 are rejected before the router is touched (exit 1) with a hint naming the seconds equivalent, so an unmigrated `--timeout 15000` fails fast instead of waiting 15000 s per request. `BGW_TIMEOUT_MS` and the internal client API remain in milliseconds; `BGW_TIMEOUT_MS` has the same one-hour ceiling (at most `3600000`; a larger value is a usage error, exit 1, naming the variable). Two pages on this gateway legitimately take longer (measured 2026-09-20: home.ha 17-18 s, lanstatistics.ha 23-29 s), so the client applies a 45 s floor to them (`SLOW_PAGE_TIMEOUT_MS`) when the timeout is the implicit default. An explicit `--timeout` or `BGW_TIMEOUT_MS` disables those floors. Everything else keeps the 15 s default so a dead page still fails fast in sweeps. A response whose body ends before its declared `Content-Length` is a connection error (`Response from <url> ended early: received N of M declared bytes`, exit 2), never parsed as a complete page. The deadline also covers host-name resolution and every connect attempt: a resolver that hangs ends in `Timed out connecting to <url>` at the deadline, and no request is sent after it.

CLI unit migration: replace earlier `--timeout 45000` with `--timeout 45` and `--timeout 1500` with `--timeout 1.5`. Existing `BGW_TIMEOUT_MS=45000` settings still mean 45 s. Other options explicitly documented in milliseconds, such as `--session-wait-timeout`, retain their units.

## Environment Variables

Identical to the TypeScript CLI, so one shell setup serves both:

| Variable | Meaning | Default |
| --- | --- | --- |
| `BGW_HOST` / `ROUTER_IP` | Router host (`--host`): an optional scheme, a host name or address and an optional port, nothing else; credentials, a path, query or fragment, whitespace, an empty or invalid host name, a bad port or an invalid IPv6 literal is a usage error (exit 1) before any request; an explicitly supplied empty or whitespace-only `--host` is a usage error too and never falls back to `BGW_HOST`, `ROUTER_IP` or the default (leaving `--host` out is unchanged); an empty or whitespace-only `BGW_HOST` or `ROUTER_IP` that is set is a usage error too (unset it to fall back) whenever the environment is the host source: an explicit `--host` takes precedence and `help` never consults it | `192.168.1.254` |
| `BGW_ACCESS_CODE` | Device access code; the usual way to authenticate (`--access-code-stdin` is the alternative for scripts and wins when both are given) | unset |
| `BGW_FALLBACK_ACCESS_CODE` | `autorestore` only: the sticker code a factory reset reverts to; tried once when the primary code is rejected (also after a cached primary session is refused); needing it corroborates a reset-shaped difference or an unfinished recovery, and on its own only warns | unset |
| `BGW_TIMEOUT_MS` | Request timeout in milliseconds (CLI `--timeout` uses seconds); at most `3600000` | `15000` |
| `BGW_INSECURE_TLS` | `0` enforces TLS validation (same as `--strict-tls`) | accept self-signed |
| `BGW_WAIT_FOR_SESSION` | `1` waits when the web session pool is full (`--wait-for-session`) | off |
| `BGW_SESSION_WAIT_TIMEOUT_MS` | Session wait timeout (`--session-wait-timeout`) | `120000` |
| `BGW_SESSION_WAIT_INTERVAL_MS` | Session wait poll interval (`--session-wait-interval`) | `10000` |
| `BGW_SESSION_CACHE_TTL_MS` | Local router session cache lifetime | `120000` |
| `BGW_SESSION_POOL_COOLDOWN_MS` | Local fail-fast cooldown after a pool-full response | `300000` |
| `BGW_SESSION_LOCK_TIMEOUT_MS` | Per-router lock wait | `300000` |
| `BGW_SESSION_LOCK_STALE_MS` | Age after which a legacy lock marker may be reclaimed (validated like the other numeric variables); our own crashed-writer lock temporaries (`.bgw-lock-` followed by exactly eight lowercase letters, digits or underscores, never another `.bgw-lock-*` file) and `*.tmp` files older than the larger of this and 60 s are removed when the cache directory is prepared | `900000` |
| `BGW_SESSION_CACHE_DIR` / `XDG_CACHE_HOME` | Session cache location (`~/.cache/bgw` by default) | |
| `BGW_DUMP_DIR` / `XDG_STATE_HOME` | Default `dump` output directory (`~/.local/state/bgw/dumps`) | |

Numeric variables follow JavaScript `Number()` for the documented inputs: decimal, fractional (`1500.5` is kept as a float), exponent notation and surrounding whitespace are accepted; empty falls back to the default; NaN/Infinity/non-numeric/below-minimum values are rejected with the same message as `bgw`. Hex (`0x10`) is not accepted.

Agent bursts reuse a short-lived local router session cache under a per-host lock to avoid filling the web session pool. Session and cooldown files (`~/.cache/bgw/<hash>.session.json|.cooldown.json`) retain the TypeScript CLI's format. Every session-wide failure (a Login-page bounce, also after the forced re-login and whatever its HTTP status, a `login.ha` answer with an HTTP error, a refused login or probe, a full pool) clears the cookies, the authenticated flag and the cached copy together. A cache or cooldown record with a non-finite or mis-typed stamp or cookie field holds nothing and the file is replaced; cookie values that are not text or that contain control characters are dropped, whether they come from the cache or from the router. `session clear-cache` refuses (exit 2, `owned by another user`) to delete a foreign-owned record, and recovery intent and checkpoint files are read through a no-follow descriptor: a symlinked, non-regular or foreign-owned one is a fault (autorestore exit 2, nothing sent). The cached session is also dropped when a command ends logged out; a fresh session the command obtained itself is persisted even when the command fails, while an imported session is not re-stamped by a run that raises. A successful run re-stamps the cache only when it used or replaced the session (it logged in, made an authenticated request, or ends holding different cookies); a run that never touched the router leaves the cached lifetime alone. `session status` applies the configured `BGW_SESSION_CACHE_TTL_MS` and `BGW_SESSION_POOL_COOLDOWN_MS` as a sanity cap, so a cooldown stamped further ahead than one configured length shows as expired. A page read that is retried after the forced re-login and still ends at `login.ha` (whatever its status or body) is a lost session: exit 2 and the cached session is dropped, never a page result. A login that gets no redirect to `home.ha` is verified with a protected page; if that probe is redirected to `login.ha` the login failed (`Login failed. Check the device access code.`). Session cache and cooldown records are written completely (short writes continue until every byte is out) or not at all: a failed write leaves the previous record in place. A session cache file of our own (a regular file owned by the current user) that cannot be read holds nothing usable: the command warns (`Ignoring unreadable cached router session <path>; it will be replaced`), logs in as if nothing were cached, and replaces the file. A foreign-owned cache or cooldown file (refused even when it is readable, and never imported or deleted), an uninspectable cache file, and any cooldown file that exists but cannot be read, stop the command before the router is contacted with `Cannot read local router session file <path>` and exit 2; `session status` also exits 2 on an unreadable cache file. An expired cooldown that cannot be deleted only warns and the command continues; a live cooldown still refuses it. A cache directory that cannot be searched, or a path component that is not a directory, is reported as `Cannot access local router session directory <dir>`.

The selected session cache directory must be owned by the current effective user and is set to exactly 0700 before command execution. Symlinks, foreign-owned entries and non-directories at that private leaf are refused without changing their targets or creating session files there. Parent aliases, including a symlinked `XDG_CACHE_HOME`, remain supported. Existing unmarked ancestor permissions stay unchanged. Newly created parents use the same private pending-publication markers as recovery state: a later invocation can finish owner-access repair after a process dies immediately following mkdir, including under restrictive umasks. Only validated pending markers authorize ancestor repair; foreign or unsafe markers stop cache preparation and are preserved. Parent aliases must already resolve to directories; dangling and cyclic aliases are refused before their targets or child entries are created. An explicitly selected `BGW_SESSION_CACHE_DIR=.` makes the current directory private; the filesystem root `/` and equivalent paths are refused. Permission repair uses the same no-follow and validated-descriptor fallback as recovery state. Failure to prepare the cache stops the command with its path and original cause.

After the router operation completes, a filesystem failure while persisting the session cache or pool cooldown prints a warning on stderr and preserves the command result or original pool-full error. A removed or invalid cache directory is never recreated during that operation because its original lock directory is no longer available. A pool-full outcome clears the in-memory session even when the local cooldown cannot be saved. A failure while recording the pool-full cooldown (a lock timeout, a filesystem fault or a malformed `BGW_SESSION_LOCK_STALE_MS`) is a warning on stderr; the pool-full error is always what is reported. The session pool counts as full only when the gateway answers with the "All web server sessions are in use" text on a login-shaped page; the same words inside a content page (a device name, a log line) are data. A write answered by a Login-titled page carrying the pool-full text is a full session pool even when a cached session is held and no access code was supplied: the cooldown is recorded and the pool-full error is raised with the write evidence, never "Access code required". A page READ answered with the pool-full text is likewise classified pool-full, with its cooldown recorded, before any login whenever `--wait-for-session` and an access code are not both present (so `--wait-for-session` without an access code gets the pool-full error, not "Access code required"). A `Set-Cookie` with `Max-Age` of zero or less, or an `Expires` in the past, deletes the cookie whatever its value; an `Expires` date that cannot be converted to a timestamp is ignored (the cookie is kept and `Max-Age` is still honoured). A cache record whose timestamp is an integer too large to convert to a float is unsound: nothing is cached and no `OverflowError` escapes. Reads of the recovery record, the session cache and the lock marker open the file non-blocking and without following symlinks and check that it is a regular file owned by the user before reading, so a FIFO in its place is refused instead of blocking `autorestore`. Stale-file cleanup in the session cache directory touches only the tool's own temporaries (`.bgw-lock-*` and `<key>.session|cooldown.json.<pid>.<uuid>.tmp`) and, in the recovery directory, `.recovery-*` temporaries older than an hour; any other file is left alone, so a shared `BGW_SESSION_CACHE_DIR` may hold unrelated files. Cache preparation failures before command execution remain fatal.

**Compatibility note:** Session caches now require ownership by the effective user. When `sudo` preserves `HOME`, or a setup shares a cache across users, set `BGW_SESSION_CACHE_DIR` to a separate cache owned by the effective user or run as the cache owner. Foreign-owned caches are never automatically chowned or reused across UIDs.

The session cache directory must be on a filesystem that supports exclusive hard links and kernel `flock` locks. Creating new cache parents or completing their pending publication also requires directory `fsync`; this synchronization remains mandatory even when the filesystem rejects it with `EINVAL`, `ENOTSUP`, `EOPNOTSUPP` or `ENOSYS`. Preparation then stops with the affected directory and its underlying error, and retains the pending marker for a later retry. A permanently failing directory `fsync` therefore keeps that `.bgw-publication-*.pending` marker in place, and every later run fails on the same parent until the cache is relocated or the marker is removed by hand after confirming the directory is fully published. Choose `BGW_SESSION_CACHE_DIR` on a local filesystem supporting these operations. Established, fully published cache parents need no directory synchronization; recovery checkpoint publication and removal retain their separate mandatory synchronization described above.

Hard-link publication, ownership-guard open and `flock` infrastructure failures report the operation, path and underlying cause and exit 2. Unsupported-operation errors (`ENOTSUP`, `EOPNOTSUPP` or `ENOSYS`) advise setting `BGW_SESSION_CACHE_DIR` to a private directory on a compatible local filesystem. Hard-link publication `EPERM` also gives conditional filesystem guidance because it can mean missing hard-link support or permissions/policy; `EPERM` alone does not establish missing support. Storage and path failures retain their actual cause. `ENOLCK` reports unavailable kernel lock resources. Infrastructure faults such as `ENOLCK` and `EIO` fail promptly. Lock contention waits until the configured deadline, with timeout exit 1. Filesystems without these primitives remain unsupported.

Python coordinates marker changes through a permanent `<hash>.lock.guard` file and holds a kernel advisory lock on its `<hash>.lock` marker for the entire operation. New markers include `ownership: "flock-v1"`; Python writes their complete ownership record to a private 0600 temporary inode, locks it, and publishes it with an exclusive hard link. A crash before publication cannot leave a partial reserved marker. A held lock cannot be reclaimed merely because it is old. After a crash or release, an unlocked protocol marker can be reclaimed immediately, even if its recorded PID was reused or cannot be inspected. Cleanup waits up to one second for a busy guard, independently of the acquisition timeout, and preserves the operation's result or error. If cleanup cannot remove the marker, closing its descriptor allows the next cooperating Python acquisition to reclaim it. Inode and token checks protect replacement owners. Fresh unreadable markers are preserved while acquisition waits until its configured deadline; the timeout includes the marker path and permission cause. Stale unreadable markers fail promptly with the same diagnostic evidence, without assuming their owners are dead. A proven abandoned marker that cannot be removed also fails promptly instead of waiting out the lock budget. Keep the permanent guard file in place.

These filesystem checks have bounded guarantees: release verifies the captured parent identity before opening the guard, but directory replacement or ancestor retargeting after that check remains possible. Publication, persistence and cleanup are not a complete descriptor-relative filesystem transaction; same-user namespace changes between checks remain outside these guarantees.

Legacy markers without this ownership protocol require stale age (`BGW_SESSION_LOCK_STALE_MS`, default 900000) and either a valid PID proven dead or no credible PID in an owned, single-link reserved marker. This recovers empty or malformed markers left by older writers that crashed during publication. Fresh ownerless markers, held kernel locks, foreign or shared ownerless inodes, and unreadable records are preserved. A legacy marker naming a reused or inaccessible PID needs explicit operator recovery after verifying no operation still owns it; age or a permission error alone is not that verification. Older Python and TypeScript clients do not participate in the new lifetime-lock protocol, so externally serialize mixed-version/tool runs; sharing cache formats does not guarantee safe concurrent coordination with those clients. The short cleanup wait reduces ordinary leftover markers but does not remove this mixed-client limitation.

## Router Fixture Pack

Parser ground truth belongs under:

```text
tests/fixtures/router-html/<page>.html
tests/fixtures/parsed/<page>.json
tests/fixtures/expected/<page>.json
```

Generated fixture files are gitignored on purpose, so the tests that read them skip on a fresh checkout (the skip message names `bgwcli fixtures-capture`); once a pack is present, a command page missing from it fails instead of passing. A small committed set of synthetic, real-shaped pages under `tests/fixtures-synthetic/` always runs through the parser, login detection and the fixture sanitizer. They are sanitized, but they can still reveal local topology, device names, firmware behavior, and configuration shape. Keep them local unless you have manually reviewed them.

Capture sanitized fixtures from the real router with the hidden `fixtures-capture` command (the port of `bun run fixtures:capture`):

```bash
bgwcli fixtures-capture [--out tests/fixtures] [--pages diag,logs] [--delay 750]
```

With `--json` the per-page progress lines go to stderr and stdout carries one JSON object: `{"out": <dir>, "captured": <n>, "total": <n>, "pages": [{"page", "ok", "captured", "error"}, ...]}`. When every page fetch failed the command exits 2 (the failure receipts are still written).

The capture is sweep-backed, serialized through the same per-router session coordinator, paced at no less than 750 ms between pages, and read-only at the configuration level: it performs GET requests plus the login POST required by the router. It redacts access-code-adjacent fields, Wi-Fi identifiers, device identifiers, hashes, MAC addresses, and IP addresses before writing owner-only fixture files (UTF-8, mode 0600, fixture directories 0700), and refuses to write a page whose sanitized output still looks sensitive. The captured markup is parsed once; tables are laid out on the parser's column grid (colspan and rowspan included), so every cell the parser redacts is redacted in the file: the values of a row with a secret label (including a `rowspan` label carried over several rows), the cells under a secret column header, and the text a secret label cell holds itself. The refusal check re-runs the same rule on the sanitized markup, so a cell the sanitizer missed gets that page refused (see below) instead of being certified `secretsRedacted`. Form controls are found by parsing the markup, so unquoted attributes, controls named only by `id`, `type="password"` inputs, textarea content, every option value and label of a sensitive `<select>`, and values containing quotes are all redacted; the refusal check re-parses the sanitized HTML with secrets included and fails if any sensitive control (selects included: the selected value, option values and labels) still carries a value. A page that failed in a capture run never overwrites a real captured fixture with a failure placeholder: its previous fixture is kept and the page is listed as `degraded` in the closing summary line and in the `--json` receipt (a `degraded` list and a per-page `degraded` flag); the exit code stays 0 on partial success. A failed page with no previous fixture still gets the placeholder and the `error` receipt. A control inside a value cell of a secret-labelled row, or inside a label cell that names a secret, is sanitised like the parser redacts it, and so is the text around it in that cell (hint words, a copy of the secret, a button caption) and a button's value; a secret query value in a form `action` or link `href` is redacted (names judged percent-decoded). The sanitizer's name patterns cover the parser's name rule (`pwd`, `pw`, `psk`, `community`, `token`, `secret`), and its patterns are length-bounded so a hostile 100 KB input sanitises in linear time. The sanitizer reads the document under the parser's bounds (elements, nesting, a table's grid, spans, one cell's text) without copying text into every enclosing cell; a page past a bound is refused. A refused page (a bound, or residue that survives sanitising, including any secret value found in the original that still appears literally in the result) is never certified `secretsRedacted` and never aborts the run: it keeps its previous fixture (listed as `degraded`) or gets the placeholder and `error` receipt, like a failed page. The `expected/` receipt of a truncated page carries `truncated: true`.

## Diagnostics

After a committed ping/traceroute/nslookup the gateway answers 302 with an empty body and fills its progress window asynchronously. `bgwcli` polls `diag` every 500 ms for up to 15 s until the output is non-empty and stable, then prints it as the result. The 15 s is elapsed time, read time included, and no poll starts after it ends. Output already on the `diag` page the gateway redirected to (read while checking the answer for a rejection banner) is the poll's first state and is kept even if the gateway clears it on the next read. A poll that ends without any output is still a successful start (exit 0): the JSON result carries `resultAvailable: false` (`true` when output was read) and the text says the result is not yet available and suggests `bgwcli page diag`. When the result poll itself fails (an HTTP error, a connection error or a page-level 401/403 while reading `diag`), the run is still reported as committed with the POST's evidence and the missing result is "no answer": exit 2.

A committed `action` or `diagnostics` run whose POST fails with an HTTP error or a connection error reports a structured `failed` result (`committed: false`, `writeAttempted`, `writeResponseReceived`, `statusCode`) and exits 2 on every action kind. The five form-button actions (`detect-wifi-congestion-2.4`, `detect-wifi-congestion-5`, `clear-connection-statistics`, `clear-lan-statistics`, `find-best-channel-5`) post the page's live form and inspect the answer: one whose follow-up page carries no banner at all (no `Changes saved`, no rejection, no `No changes detected`) is committed (exit 0) without an acknowledgement (`acknowledgementObserved` is absent), because these buttons are not Save buttons and the gateway does not always notify; one whose POST got no usable answer (an HTTP error, a lost connection, a refused session) reports the same structured `failed` result with exit 2 in JSON mode, and an explicit rejection banner from the gateway is a negative answer with exit 1. Every other action (`restart*`, `reset-*`, `clear-device-list`, `run-speed-test`, `run-full-diagnostics`, `send-diagnostics`, `diagnostics-*-details`, `packet-filter-*`, `restart-wifi-*`, `restart-broadband`) posts its fixed payload and is reported committed (exit 0) on a 2xx/3xx answer, unless that answer (inline in a 200, or on the page a 3xx redirects to) carries the gateway's rejection banner: that is a structured rejection with exit 1, like a form-button action. A redirect target that cannot be read (an HTTP error, a lost connection, a page-level 401/403) means the answer is unknown: the structured `failed` result carries the POST's evidence and exits 2, never committed success. The exception is the actions whose effect takes the gateway's web server down (`restart`, `restart-from-resets`, `reset-ip`, `reset-connection`, `reset-wifi-config`, `reset-firewall-config`, `factory-reset` and `restart-broadband`) and the actions that take down the client's own radio (`restart-wifi-2.4`, `restart-wifi-5` and `find-best-channel-5`): the page that would carry the answer cannot be served, so it is not read, and the result is `committed: true` with `answerRead: false` and a note saying the answer page was not read (exit 0). A committed `diagnostics` run whose POST is answered with a rejection banner (inline in a 200, or on the `diag` page a 3xx redirects to) is a structured rejection with exit 1 and is not polled.

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
  "action": "run-speed-test",
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

LAN Statistics also exposes four guarded actions. Each dry-run shows its confirmation token:

| Action | Aliases | Commit token |
| --- | --- | --- |
| `detect-wifi-congestion-2.4` | `congestion-2.4`, `congestion-24` | `CONGESTION-2.4` |
| `detect-wifi-congestion-5` | `congestion-5`, `congestion-5ghz` | `CONGESTION-5` |
| `clear-connection-statistics` | `clear-connection-stats` | `CLEAR-CONNECTION-STATISTICS` |
| `clear-lan-statistics` | `clear-statistics`, `clear-lan-stats` | `CLEAR-LAN-STATISTICS` |

```bash
bgwcli action detect-wifi-congestion-2.4
bgwcli action detect-wifi-congestion-5 --commit --confirm CONGESTION-5
bgwcli action clear-connection-statistics --commit --confirm CLEAR-CONNECTION-STATISTICS
bgwcli action clear-lan-statistics --commit --confirm CLEAR-LAN-STATISTICS
```

The congestion actions select the 2.4 GHz and 5 GHz detection buttons on `lanstatistics.ha`.
The clear actions reset the corresponding statistics counters and are marked dangerous.
On commit, the CLI checks that the selected button is present, uses its live value,
and fetches a fresh nonce before posting. The owning form's enabled data controls and fresh nonce
are submitted with only the selected submit button; the other submit buttons are excluded. These actions have
synthetic transport coverage; the real buttons have not been pressed during validation.
A POST uses the nonce inputs of the form whose action is the target, whatever the order of their `name` and `value` attributes; another form's nonce is never sent: the form posting to the target supplies the nonce (its own; the page-level nonce only when that form is the page's only form), several such forms are told apart by a control of theirs named like a posted field and are refused when that does not single one out and their nonces differ, a page whose forms all post elsewhere is refused, and a page with no form at all uses its page nonce. A refusal sends nothing. Form, nonce and title extraction are single forward scans, so hostile pages (tens of thousands of unclosed tags, or a 4 MiB body) are read in linear time.

Every `action` command is dry-run by default. `actions` prints the confirmation token required to commit each one. Reset/restart/factory-reset actions are marked dangerous and should be treated as destructive router operations.

Sensitive values are redacted by default. A value is sensitive when its control is a `type="password"` input, or when its name marks a secret: passwords and passphrases, WPA keys/PSKs/passphrases, WEP, network and (pre-)shared and security keys, passkeys, `key` as a whole token (`key`, `key11`, `homeSSID_key`, camel-case `ssidKey`, `wifiKey`, `encryptionKey`, and all-caps compounds ending in `KEY` such as `SSIDKEY` or `APIKEY`, and lowercase compounds of a key holder: `ssidkey`, `wifikey`, `wlankey`, `encryptionkey`, `networkkey`, `sharedkey`, `securitykey`, `apikey`, `radiuskey`, `passkey`), access codes, nonces, hashed passwords, WPS PINs, phone numbers and caller information. Names with `pw`, `pass`, `passcode`, `psk` or private-key words glued to a prefix are secrets too (`newpw`, `oldpass`, `adminpass`, `pppoepw`, `userpass`, `authpass`, `privpass`, `wlpw`, `dynpass`, `privatekey`, `privkey`, `ikekey`, `sshkey`, `snmpv3auth`, `snmp_priv`), while `bypass`, `passive`, `pass_through`, `power`, `tokenizer`, `token_id`, `keyboard`, `keyword`, `monkey`, `turkey` and `hockey` stay plain. Names that only contain `wpa` or `key` inside another word, such as `wpaversion` (WPA version), `keyrotation` or `keyRotation`, are not secrets and are shown. A key token followed by another word, directly or after `_`, `-` or `.`, names a setting about the key rather than the key, so `wpakeyrotation`, `wpa_key_interval`, `wepkeyindex`, `WEPKeyIndex`, `key_rotation`, `key_id`, `keyId`, `keyindex`, `ssidkeyrotation`, `wifikeyindex` and `apikeyid` are shown too. The `type="password"` rule holds on every output path, including `set`/`submit` dry-run payloads, post-save mismatch reports, `diff` and `restore` plans (see `formSecrets` above). Use `--include-secrets` only when intentionally inspecting local output; it reveals values in `diff` text and `--json` output alike. Debug/schema output still redacts secrets by default.

`restore --commit` is a multi-POST operation behind a single `--confirm RESTORE`; review the dry-run plan first.

Router text is untrusted on the terminal: human output strips ANSI escape sequences, C0/C1 control characters and Unicode format characters (bidi embeddings, overrides and isolates, zero-width characters, the BOM), so a device name cannot recolour, reorder or hide what is printed. `--json` keeps every value but writes DEL, C1 controls and format characters as `\uXXXX` escapes; other Unicode stays as-is. Every default-mode (neither `--json` nor `--raw`) line that embeds router-derived text is sanitised where it is printed, including allocation preflight reasons, rescan and autorestore log lines, post-restore verification messages and fixture-capture progress. `--raw` is the exception: it writes the page exactly as the gateway sent it, so a raw page can carry escape sequences; do not send it to a terminal you do not trust.

The client never uses an HTTP(S) proxy: `http_proxy`, `https_proxy`, `all_proxy` and system proxy settings are ignored, so the access code and session cookies go only to the router.

The router normally presents a self-signed certificate, so the CLI currently accepts it by default. This encrypts traffic without proving router identity; use `--strict-tls` only after installing or pinning a certificate that the runtime can validate, and otherwise run the CLI only from a trusted local network.

## Exit Codes

| Code | Meaning |
| --- | --- |
| `0` | Success (`diff`: identical; `restore --commit`: converged; `autorestore`: no reset, converged, or router unreachable; `set`/`submit`: `Changes saved` observed and, for `set`, the live form reads back as requested or the requested `ipalloc` reservation was seen as a Fixed Allocation row (`submit` does not re-read the form after a plain `Changes saved`; it does when a requested reservation or LAN setting is not visible afterwards), or `No changes detected. Save not performed.` and the live form was re-read and already matches the request). |
| `1` | Negative answer: `diff` found differences, `restore --commit` did not converge, `autorestore` needs or did not finish a restore, usage errors (including an empty `--include`/`--pages` list and `--raw` with `audit`/`readiness`), confirmation refusals, a session lock wait that timed out, a write the gateway explicitly rejected with an error banner (the banner text is quoted), a `set` whose live form reads back differently after `Changes saved`, and a `set`/`submit` answered `No changes detected` while the router ignored or normalised the change (the differing fields are listed). `submit` is re-read after `Changes saved` only when a requested reservation or LAN setting is not visible afterwards; otherwise it has no read-back mismatch. A systemd unit that treats exit 1 as success (`SuccessExitStatus=1` in `deploy/`, so `not-converged` does not mark the unit failed) hides usage errors and lock-wait timeouts too, since they share exit 1; check the journal. |
| `2` | Could not answer: authentication/connection failures, an HTTP error answer from the router on any command (`sitemap` and `coverage` included), `check` when the gateway did not answer at all, a 200 answer that is the Login page or a `Page not found` document where a page was expected (`page --raw`, `logs`, `sitemap` and `coverage` included), a log or form page without its structure (no log table, no form control), an unexpected internal fault, session pool full, `status` or a composite view that could read nothing, unreadable dump file, snapshot extraction failure, a page that could not be fetched, or a configuration write whose `Changes saved` acknowledgement was never observed, or whose `No changes detected` answer could not be checked against a live re-read even after the 3 retried attempts, or (`set`) whose `Changes saved` acknowledgement was observed but the verification re-read could not be completed (`committed: true`, `verified` absent, warning; check `committed` before re-running) (the router state is unknown; the same mapping applies to every `set`/`submit` save: Wi-Fi, LAN and generic pages). `restore --commit` and `autorestore` also exit 2 when any write in the run had no answer (a timeout or connection loss, an HTTP error status, a refused session, a full session pool, or an answer that never carried `Changes saved`), even when the closing diff is readable and differs: the JSON says `writeUnanswered: true` and the reason names the write. A restore step that failed before any write was sent because of an HTTP or transport fault (the nonce read, say) is exit 2 too, with `writeUnanswered` false. A write the gateway did answer (a rejection banner, `No changes detected`, or `Changes saved` with a differing state) stays exit 1 (`not-converged`). `autorestore` additionally exits 2 when the same failure ended three consecutive runs of one recovery. `autorestore`: a connection error or an HTTP error answer before anything was written is `router-unreachable`, exit 0. |

Ctrl-C ends the run with exit 130 and `Interrupted` on stderr; with `--json` the output is an object with `errorType` `Interrupted`, `exitCode` 130 and the write evidence (`writeAttempted`, `writeResponseReceived`, `writeAttempts`, `statusCode`), and when a configuration POST may have been sent the message says the write state is unknown and the gateway must be re-read before retrying. After an `autorestore` login with `BGW_FALLBACK_ACCESS_CODE` the evidence is read from both the primary and the fallback client, and a client with a sent POST decides `writeAttempted`.

With `--json`, an error that ends the command (including a usage error) still prints a JSON object on stdout: `{"ok": false, "error": ..., "exitCode": ..., "errorType": ...}`, plus `writeAttempted` when the error was raised by the write step itself (the nonce read, the POST, or the acknowledgement/result read): `true` if a POST was observed leaving, `false` if the step failed before sending; an error raised before the write step, for example while reading the page to build the plan, carries no `writeAttempted` key; a full session pool keeps its own object (`sessionPoolFull`, `waitedMs`, `retryCount`) with the same `exitCode`. Except for the session-pool-full object, the error text is also written to stderr; router text inside it (a banner, a page title, a table cell) is stripped of escape sequences, control characters and bidi overrides first, like every other string that came from the router, and an unterminated escape sequence is dropped only to the end of its line.

## Differences from bgw

`bgwcli` is a behavior-for-behavior port of the TypeScript `bgw` CLI; the deliberate exceptions are:

- Unknown `--flags` are rejected (`unrecognized arguments: --bogus`, exit 1). `bgw` silently treats unknown flags as positional arguments (`bgw tabs --bogus` prints the tabs and exits 0). Strict rejection catches typos such as `--jsno` before a mutation runs.
- `--flag=value` is accepted for value-taking options; abbreviated long options (`--jso`) are rejected like unknown flags (exit 1).
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

2026-10-06 final validation ran on the Raspberry Pi with Python 3.13.5: the full suite passed, with
the captured-router fixture tests skipped because the fixtures were absent. Ruff and independent reviews passed.
Failure cases use synthetic transports and local HTTP servers. The installed public CLI also
passed a live unchanged Wi-Fi Save: the gateway returned `No changes detected. Save not performed.`,
and the CLI reported `unchanged`, `verified: true`, and `writePerformed: false`.

WPS was turned Off on both Wi-Fi bands and verified. A new private backup differs from the
preceding backup only in those two WPS fields; the final live comparison matched the new backup.
The four LAN Statistics actions passed installed CLI dry-runs; their real buttons were not pressed.
No system services or timers were enabled or started.

The earlier live write tests below exercised the October 5 build.

2026-10-05, gateway firmware 6.35.8, Raspberry Pi ARM64 / Python 3.13.5: the final automated suite
passed 831 tests on both the Mac and Pi (40 skipped because captured-router fixtures were absent).
Live checks used a fresh schema-2 backup:

- Restored a deleted WireGuard custom service and its NAT/Gaming entry; the operator confirmed
  a fresh external handshake and working tunnel traffic.
- Restored a released fixed reservation, requiring both `Changes saved` and the expected MAC/IP
  row. After client DHCP renewal, ping and neighbor lookup verified the reserved address and MAC.
- Restored the service, forward, reservation and a changed Firewall Advanced WAN ICMP setting
  together: four applied steps, no failures, and a clean full backup comparison. The operator
  confirmed external WireGuard traffic after DHCP renewal.
- Restored the 2.4 GHz Wi-Fi channel from 10 to 11: save acknowledgement observed and Wi-Fi
  configuration comparison clean. The deployed Continue-handler success and timeout tests also passed.
- Repeated a full restore against the unchanged gateway: zero applied writes, one documentary
  packet-filter skip, and a clean comparison.
- With another MAC actively holding the requested IP and a fixed reservation, the earlier version
  sent one Clear, exhausted its 180-second ownership-check window, and reported the conflicting holder.
  The current version refuses that fixed assignment promptly without Clear.
  The HTTP trace contained no allocation or NAT writes, and the pre-test configuration comparison
  remained clean.
- After that client's fixed reservation was released and the client disconnected, the gateway
  still reported its old MAC/IP as a DHCP allocation. Automatic recovery sent one Clear, removed
  the stale ownership, and restored the intended MAC/IP with `Changes saved`. The first Allocate
  POST started about 78 seconds after the Clear response. Client DHCP renewal then activated the
  reserved address, verified by ping and neighbor MAC; the full original-backup comparison was clean.

The fixed reservation did not immediately replace the client's existing DHCP lease. Also, releasing
the reservation initially removed that device from IP Allocation while Devices still listed it
online. One manual Clear and Rescan plus a 60-second wait did not restore its Allocate control;
client DHCP rediscovery after reboot did. Save-timeout and malformed-response handling are covered
by automated tests, without live fault injection. The earlier reported NAT failure was not
reproduced, and these checks do not establish its cause.

2026-09-20, gateway firmware 6.35.8: `bgwcli dump` diffed clean against a `bgw` dump in both directions. A dump with a throwaway `zz_test` service (TCP 61000) and a `zz_test -> host-a` forward restored in one `bgwcli restore --commit` run (service applied, deferred forward applied after the NAT/Gaming dropdown re-read, convergence diff clean); the TypeScript `bgw diff` agreed with the resulting state; `bgwcli restore <baseline> --prune --commit` removed forward then service in one run; final diffs from both CLIs reported no differences.

By default `bgwcli dump` keeps only Fixed Allocation rows in the documentary `tables.ipalloc` block, so the dump carries no trace of DHCP-only devices; `--all-clients` keeps every client row. The `reservations` list that `diff`/`restore` act on is always fixed-rows-only regardless of the flag. Same behavior in the TypeScript `bgw`.

## Radio and broadband restarts

home.ha carries per-radio Restart buttons whose forms post to their own CGI scripts (`wrestart.ha?1`, `wrestart.ha?2`, `crestart.ha?1`) with the nonce served on home.ha. They are exposed as named actions:

```
bgwcli action restart-wifi-2.4 --commit --confirm RESTART-WIFI        # aliases restart-wifi-24, restart-2.4ghz
bgwcli action restart-wifi-5   --commit --confirm RESTART-WIFI
bgwcli action restart-broadband --commit --confirm RESTART-BROADBAND  # dangerous: drops WAN
```

Clients on the restarted band drop for a few seconds; run it from a wired host or a client on the other band. The answer page is not read (the client's own radio is what restarts): the result is `committed: true` with `answerRead: false`, exit 0, like the other restart actions. Actions with a `post_path` go through `client.post_form(nonce_page, post_path, fields)`; everything else still posts to `<page>.ha`.

Two gateway facts shape `post_form` (proven live 2026-09-20 with `restart-wifi-2.4`): home.ha is a public page, so fetching it never triggers the auto-login - `post_form` authenticates first when no session exists and re-logins + retries once if the POST answers with the Login page; and each form on home.ha carries its own nonce(s) (the Restart forms carry two, in nested markup), so the nonces of the form whose action matches the target path are posted, never the page's first nonce.

home.ha often takes longer than the default 15 s to answer; pass `--timeout 45` with these actions:

```
bgwcli action restart-wifi-2.4 --commit --confirm RESTART-WIFI --timeout 45
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
bgwcli action find-best-channel-5 --commit --confirm CHANSCAN --timeout 45   # aliases chanscan5, find-best-channel
```

5 GHz clients drop briefly while the radio re-tunes; run it from a wired host or a 2.4 GHz client. Dry-run without `--commit`. Not yet run live. Its answer page is not read either (`committed: true`, `answerRead: false`, exit 0), since the client's own radio re-tunes. The generic `submit` and `set` commits share the same Wi-Fi Warning handling.
