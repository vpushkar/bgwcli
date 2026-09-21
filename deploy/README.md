# Running `bgwcli autorestore` from a systemd timer (Raspberry Pi)

Goal: after AT&T factory-resets the gateway (firmware update, support call), a Pi on the LAN
notices within 15 minutes and replays `~/bgw-baseline.json` — and never touches anything else.
`bgwcli autorestore` only acts when EVERY dumped service, forward and reservation is missing (or
when the primary access code stops working and the fallback sticker code does); ordinary drift is
reported as `no-reset` and left alone. It never uses `--prune`.

## 1. Install bgwcli for the `pi` user

```
curl -fsSL https://raw.githubusercontent.com/vpushkar/bgwcli/main/install.sh | sh
bgwcli check
```

`~/.local/bin/bgwcli` must exist afterwards (the unit calls it by that path).

## 2. Take the baseline

```
export BGW_ACCESS_CODE='<your access code>'
bgwcli dump --include all --out ~/bgw-baseline.json
bgwcli diff ~/bgw-baseline.json            # must print "No differences."
```

Re-run that `dump` after **every** deliberate change you make in the gateway UI. The dump is the
definition of "correct"; a stale baseline plus a factory reset would restore old settings, and a
baseline that no longer matches your intent is what makes the watchdog fight you.

## 3. Install the unit, timer and credentials

```
mkdir -p ~/.config/systemd/user ~/.config/bgw
cp deploy/bgw-autorestore.service deploy/bgw-autorestore.timer ~/.config/systemd/user/
cp deploy/autorestore.env.example ~/.config/bgw/autorestore.env
chmod 600 ~/.config/bgw/autorestore.env
$EDITOR ~/.config/bgw/autorestore.env        # BGW_ACCESS_CODE and BGW_FALLBACK_ACCESS_CODE
systemctl --user daemon-reload
systemctl --user enable --now bgw-autorestore.timer
loginctl enable-linger pi                    # user timers must run without a login session
```

`BGW_FALLBACK_ACCESS_CODE` is the code printed on the gateway's sticker. A factory reset reverts
to it; when the primary code is rejected the run retries the login once with the fallback, and
needing it counts as a reset signal on its own.

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
not mark the unit failed), 2 error after the router was written to (shows up as a failed unit).

## 5. Dry-run by hand

```
set -a; . ~/.config/bgw/autorestore.env; set +a
bgwcli autorestore ~/bgw-baseline.json                # plan only, nothing is sent
bgwcli autorestore ~/bgw-baseline.json --json | jq .status
```

Exit 1 with `restore-needed` means a reset was detected and the printed plan is what
`--commit --confirm RESTORE` would send. `--on-any-diff` makes every difference count as a reset;
only use it if the Pi is the sole place configuration is ever changed from.

## Removing it

```
systemctl --user disable --now bgw-autorestore.timer
rm ~/.config/systemd/user/bgw-autorestore.{service,timer} ~/.config/bgw/autorestore.env
systemctl --user daemon-reload
```
