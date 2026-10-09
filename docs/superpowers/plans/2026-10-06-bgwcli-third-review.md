# Third review and follow-on CLI work

All required fixes and test commands are authorized. Tests run on neo via the `neoext` SSH alias
in isolated private checkouts; no system services or timers may be enabled or started. The active
installation was last verified at `1120e42`, with 94 matching files and passing startup checks.

## Completed before this review

- `--timeout` now accepts seconds; explicitly named millisecond environment variables retain
  their units. Exact conversion and truncation passed 1,128 tests on neo (40 fixture-pack skips).
- WPS was disabled on both bands (`wps=off`, `wps_5=off`) and verified. The private backup
  `/home/pi/bgw320/bgw-20261006-wps-off.json` differs from its predecessor only in those two fields;
  a subsequent live comparison was identical.
- The actual Wi-Fi no-change response was captured: Save POST returns 302, and the redirected
  GET contains `No changes detected. Save not performed.` The next GET clears the message.
- Reviewed Wi-Fi no-change handling (`8b1ae7e` -> `05455dc`) reports unchanged/no write separately
  and verifies known requested values. Its isolated neo suite passed 1,161 tests, 40 skipped.
- Reviewed LAN Statistics actions (`ffa3dac` -> `03793c7`) map the four live buttons, with dry-run
  and confirmation gates. The 151 affected tests passed on neo. No real buttons were pressed.

## Third-review workstreams

| Item | Required outcome | Owner |
| --- | --- | --- |
| T1 | Unreadable lock marker is unknown ownership; wait within acquisition deadline rather than crash or remove it. | Session |
| T2 | Release waits briefly for normal guard contention, remains bounded, preserves original result/error, and closes lifetime ownership safely. | Session |
| T3 | Mask/DHCP-only empty-body 302 saves perform verification at the unchanged address; an actual address change still avoids old-origin reads. | Write evidence |
| T4 | Public typed observation replaces private client counter access. Unsupported observers report unknown delivery on exceptions, never falsely sent or safely unsent. | Write evidence |
| T5 | Validate directory boundaries/root termination; cleanup failure must not replace the original fsync error. | Recovery |
| T6 | Reuse shared pool metadata normalization without losing error/result metadata or coherent observed pairs. | Write evidence |
| T7 | Keep exact Decimal conversion. The suggested `scaleb(3)` replacement was disproved on neo. | Resolved by evidence |
| T8 | Consolidate overlapping autorestore execution scans while preserving failure, reconnect, pool, and mutation semantics. | Recovery |
| T9 | Route checkpoint cleanup through shared transition handling while preserving the primary failure and reporting cleanup failure. | Recovery |

On neo, `1.000999999999999999999999999999999` converts exactly to 1000 ms; `scaleb(3)` produces
1001 ms. With decimal precision 2, `1.001` converts exactly to 1001 ms; `scaleb(3)` produces 1000 ms.
This is a tested reason to reject T7's proposed simplification.

The separate checkable-input correction is under review: a checkbox/radio with no value attribute
uses the browser default `on`, while an explicitly empty value remains empty. Captured MAC-filter
fields demonstrate the issue, but causation of the firmware's enumeration error has not been proven.
Do not bypass disabled controls or add MAC-filter entries as a workaround.

## Completion gates

- [x] Review and integrate the checkable-input correction with its compatibility assessment.
- [x] Reproduce and fix the actionable third-review findings on neo.
- [x] Independently review each change and the combined interfaces.
- [x] Run the combined full suite and Ruff on neo, then deploy with rollback and hash verification.
- [x] Verify Wi-Fi no-change behavior through the public CLI and preserve the WPS-off configuration.
- [x] Verify the four LAN actions' dry-runs without invoking the real controls.
- [x] Record final outcomes and limits; no services or timers.


## Final result

Tested code revision: `f7aa47f`. The exact committed archive was verified on neo and passed
**1,246 tests, 40 skipped, in 34.65 seconds**, followed by a clean Ruff run. All scoped reviews
and the final integration review approved the changes. The installed revision matched all
99 tracked file hashes and passed import/CLI startup checks.

The public Wi-Fi command was tested live using the already-desired WPS Off values. It returned
exit 0 with `outcome: unchanged`, `committed: false`, `verified: true`, `writeAttempted: true`, and
`writePerformed: false`. The subsequent live comparison matched the WPS-off backup.

All four LAN Statistics commands produced the expected guarded dry-runs with the exact live
button names and distinct confirmation tokens. No live LAN Statistics button was pressed.
The existing `clear-device-list` action remains available for Device List's Clear and Rescan.

T1-T6, T8, and T9 were addressed. T7's suggested Decimal simplification was rejected by the
reproductions recorded above. The checkbox correction was approved; the inspected existing
backups were unaffected, and ambiguous historical values were not silently rewritten.

No system services or timers were enabled or started. Target deployment metadata retains the
rollback archive and source revision; the private execution ledger records the final transfer.
