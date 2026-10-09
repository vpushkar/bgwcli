# Running `bgwcli autorestore` from a systemd timer (Raspberry Pi)

Goal: after AT&T factory-resets the gateway (firmware update, support call), a Pi on the LAN
notices within 15 minutes and replays `~/bgw-baseline.json`.
`bgwcli autorestore` only acts when EVERY dumped service, forward and reservation is missing, or
when an unfinished recovery of the same dump is recorded; ordinary drift is reported as `no-reset`
and left alone. Needing the fallback sticker code to log in is no reset signal on its own (see
section 3). It never uses `--prune`.

Before restore writes, a requested IP held by another MAC's fixed reservation blocks recovery
until that reservation is released manually. Other ownership conflicts can trigger **Clear and
Rescan for Devices** once. The watchdog waits at least 60 seconds, checks both Device List and
IP Allocation within a 180-second window, and rebuilds the plan from refreshed pages. Incomplete
ownership readings are retried within that window and never prove that an address is free.
Hostnames do not determine ownership. A dry-run reports whether planning is pending or blocked.
Configuration writes wait for `Changes saved` and verify their known requested values; allocation
saves verify the intended MAC and fixed IP. A changed LAN address requires final verification
after reconnecting to the new address. An in-flight HTTP request can extend a polling window by
its request timeout.

Recovery intent is saved before writes, so a later invocation can resume a matching partially
completed recovery using fresh router data. Read-only preflight failures do not create new intent.
Ordinary unrelated drift remains `no-reset`; see the main README for checkpoint identity and storage.

Within one run a write the gateway acknowledged (`Changes saved`, or `No changes detected`) is never
posted again: later passes only re-read the gateway and retry steps that were blocked or never
sent. If the values still do not stick, the run ends `not-converged` (exit 1) and the next timer
run starts over from a fresh read. A write that got no answer at all (a timeout, an HTTP error, a
refused session or a full session pool after it was sent) ends the run `error` (exit 2, a failed
unit) with `writeUnanswered: true`, even when the closing diff can be read: the gateway may or may
not have applied it. The recovery intent counts consecutive runs that end with the
same failure (a write that does not take effect, a step that keeps failing, or IP conflicts that a
Clear and Rescan does not resolve); the third identical failure exits 2, and from then on the timer
sends nothing for that recovery and exits 2 until the gateway is found converged or you remove the
intent file named in the message (after checking the gateway, or after a manual `bgwcli restore`).

## 1. Install bgwcli for the service user

```
curl -fsSL https://raw.githubusercontent.com/vpushkar/bgwcli/main/install.sh | sh
bgwcli check
```

`~/.local/bin/bgwcli` must exist afterwards (the unit calls it by that path).

The installer puts only the command on disk: the unit, timer and environment template that
section 3 copies from `deploy/` are not part of the installed package. Fetch them from the same
branch the installer installed (`main`), either as a checkout:

```
git clone --depth 1 https://github.com/vpushkar/bgwcli ~/bgwcli-src
cd ~/bgwcli-src                              # section 3 runs its cp commands from here
```

or as the three files alone:

```
mkdir -p ~/bgwcli-src/deploy && cd ~/bgwcli-src
for f in bgw-autorestore.service bgw-autorestore.timer autorestore.env.example; do
    curl -fsSL -o "deploy/$f" "https://raw.githubusercontent.com/vpushkar/bgwcli/main/deploy/$f"
done
```

Installed with `BGWCLI_REPO` pointing at another branch or tag? Fetch the deploy files from that
same ref, so the unit matches the command it runs.

## 2. Take the baseline

```
export BGW_ACCESS_CODE='<your access code>'
bgwcli dump --include etherlan,dhcpserver,ippass --out ~/bgw-baseline.json
bgwcli diff ~/bgw-baseline.json            # must print "No differences."
```

The unattended baseline deliberately leaves out Wi-Fi MAC Filtering modes (`wmacauth`): a factory
reset also wipes the MAC filter list, which bgwcli records but never restores, and an `allow` mode
restored without that list locks every Wi-Fi client out. (A restore blocks such a mode when the live
filter table lacks the dumped rows; leaving the page out of the timer's baseline avoids the blocked
step on every run.) Restore MAC filtering by hand after re-entering the list.

Re-run that `dump` after **every** deliberate change you make in the gateway UI. The dump is the
definition of "correct"; a stale baseline plus a factory reset would restore old settings, and a
baseline that no longer matches your intent is what makes the watchdog fight you.

## 3. Install the unit, timer and credentials

From the directory holding `deploy/` (section 1):

```
mkdir -p ~/.config/systemd/user ~/.config/bgw
cp deploy/bgw-autorestore.service deploy/bgw-autorestore.timer ~/.config/systemd/user/
cp deploy/autorestore.env.example ~/.config/bgw/autorestore.env
chmod 600 ~/.config/bgw/autorestore.env
$EDITOR ~/.config/bgw/autorestore.env        # BGW_ACCESS_CODE and BGW_FALLBACK_ACCESS_CODE
systemctl --user daemon-reload
systemctl --user enable --now bgw-autorestore.timer
loginctl enable-linger <user>                # user timers must run without a login session
```

`BGW_FALLBACK_ACCESS_CODE` is the code printed on the gateway's sticker. A factory reset reverts
to it; when the primary code is rejected (also when a cached session from before the reset is
refused) the run retries the login once with the fallback. Needing it is named in the reason when the
diff looks like a reset (or a recovery is unfinished), but on its own it is not a reset: if the
gateway still holds the baseline (someone set the access code back by hand) the run reports
`no-reset`, sends nothing and logs a `warning:` to set `BGW_ACCESS_CODE` to the gateway's current
code. The fallback session is cached, so the next run does not log in again.

Quote every value in `autorestore.env` with single quotes, as in the example. systemd's
`EnvironmentFile=` and `sh` (`set -a; . file`) both strip the surrounding single quotes and read
what is between them literally, so the unit and a shell see the same code; an unquoted value with
spaces, `#`, `;`, `$`, quotes or backslashes may be read differently by the two. A code containing
`'` cannot be written this way; change it on the gateway.

## 4. Check it works

```
systemctl --user list-timers bgw-autorestore.timer
systemctl --user start bgw-autorestore.service
journalctl --user -u bgw-autorestore -n 50
```

A healthy run logs a single `no-reset: no differences` line. After a real reset you will see
`factory reset detected: ...`, one `pass N/3: ...` line per pass, the step lines and finally
`converged after pass N` followed by the closing diff.

Exit codes: 0 no-reset / converged / router unreachable (the timer stays quiet while the gateway
reboots), 1 not converged (the next tick retries; the unit declares it a success so systemd does
not mark the unit failed), 2 a rejected access code with no usable fallback, a page the gateway
refuses with 401/403 on the initial read, a full web session
pool, an unreadable dump file, an ownership preflight failure (other than an unreachable router
before its Clear was sent, which is exit 0), a recovery checkpoint that cannot be read or written,
another unfinished recovery already recorded, the third consecutive run ending with the same
failure (and every later run of that stopped recovery), a configuration write that got no answer
(even when the closing diff is readable), or an error after the router was written to (shows up as
a failed unit).

`SuccessExitStatus=1` cannot tell exit 1 causes apart: a usage error (a mistyped option in the
unit, a bad value in `autorestore.env` such as `BGW_TIMEOUT_MS` above one hour) and a timeout
waiting for the local session lock (another `bgwcli` command holding it) also exit 1, so they too
leave the unit green. A mistyped or unreadable dump path is different: it is a dump file error and
exits 2, so the unit shows as failed. Read the journal after editing the unit or the environment
file: `journalctl --user -u bgw-autorestore` shows the usage, lock or dump file message instead of
a status line.

The unit's `TimeoutStartSec=2580` is the run's worst case once it holds the session lock: 1800 s run
limit (`RUN_DEADLINE_SECONDS`) + 90 s for the one step that may still be in flight (15 s nonce read +
15 s POST + 60 s acknowledgement window) + 555 s as an upper bound for the closing fetch (the 11 snapshot
pages, 7 always plus up to 4 optional form pages, at the 15 s page timeout; the figure is kept
generous and keeps the 2580 s arithmetic unchanged) + a 135 s margin. The wait for the local
session lock (`BGW_SESSION_LOCK_TIMEOUT_MS`, default 300 s) happens before the run limit starts
counting and is not part of that sum, so a run that first waited up to 300 s for another `bgwcli`
command can overrun 2580 s (1800 + 90 + 555 + 300 = 2745 s before the margin is counted). When
`TimeoutStartSec` elapses systemd stops the run (SIGTERM, then SIGKILL after `TimeoutStopSec`, which
the unit leaves at the systemd default) and marks the unit failed with result `timeout`; `bgwcli`
installs no SIGTERM handler, so the process ends at once. A recovery intent already recorded stays on
disk; the next timer tick re-reads the gateway and rebuilds its plan from that fresh state (it never
replays saved POSTs). Raise `BGW_SESSION_LOCK_TIMEOUT_MS` only together with `TimeoutStartSec`.

## 5. Dry-run by hand

Run it with the unit's own environment file, read by systemd exactly as the timer reads it:

```
systemd-run --user --pipe --wait -p EnvironmentFile="$HOME/.config/bgw/autorestore.env" \
    "$HOME/.local/bin/bgwcli" autorestore "$HOME/bgw-baseline.json"          # plan only, nothing is sent
systemd-run --user --pipe --wait -p EnvironmentFile="$HOME/.config/bgw/autorestore.env" \
    "$HOME/.local/bin/bgwcli" autorestore "$HOME/bgw-baseline.json" --json | jq .status
```

Sourcing the file into a shell (`set -a; . ~/.config/bgw/autorestore.env; set +a`) gives the same
values as long as they are single-quoted as described in section 3.

When stdout is a closed pipe (a hand run piped to `head`), autorestore stops printing progress but
completes the run; its recovery bookkeeping and exit code are unchanged.

Exit 1 with `restore-needed` means a reset was detected and the printed plan is what
`--commit --confirm RESTORE` would send. If an IP conflict is found, the dry-run reports the planned
rescan; the remaining plan is rebuilt after the rescan during a committed run.
`--on-any-diff` makes every difference restore can act on count as a reset (router-only entries,
controls only the live page has and controls it renders disabled never do);
only use it if the Pi is the sole place configuration is ever changed from.

## A custom LAN subnet

If the baseline's Subnets & DHCP (`dhcpserver`) moves the gateway off the factory 192.168.1.0/24
network, a recovery needs two runs at two addresses. After a reset the gateway answers at
192.168.1.254; Subnets & DHCP is restored last, and the run that saves it ends with exit 2 because
its closing diff cannot reach the moved gateway. Reservations whose addresses lie outside the reset
subnet are reported `blocked` ("not offered") in that run: the gateway cannot allocate them until
the LAN has moved. They are restored by a second run against the new address, once the Pi has a
lease on the new subnet:

```
bgwcli restore ~/bgw-baseline.json --host <the baseline's LAN address>                    # read the plan
bgwcli restore ~/bgw-baseline.json --host <the baseline's LAN address> --commit --confirm RESTORE
```

Use `restore` for this second run, not `autorestore`: the recovery intent is recorded per gateway
address, so at the new address only the reservations are missing, which `autorestore` treats as
ordinary drift. The timer talks to one address (`BGW_HOST`, default 192.168.1.254), so with a custom
subnet only one side of the move is automatic.

Keep `BGW_HOST` at that factory address (leave it unset, or 192.168.1.254) even after the move.
A factory reset returns the gateway to 192.168.1.254, so it is the only address at which the timer
can notice a reset. Once the gateway lives at the baseline's custom LAN address, the configured
`BGW_HOST` no longer reaches it after a reset: if `BGW_HOST` were the custom address, every run
would end `router-unreachable` (exit 0, quiet) and a later factory reset would never be noticed.
For the same reason, while the gateway is at its custom address the timer's runs at 192.168.1.254
find nothing and also report `router-unreachable`; that is the expected quiet state, not a fault.

## Removing it

```
systemctl --user disable --now bgw-autorestore.timer
rm ~/.config/systemd/user/bgw-autorestore.{service,timer} ~/.config/bgw/autorestore.env
systemctl --user daemon-reload
```
