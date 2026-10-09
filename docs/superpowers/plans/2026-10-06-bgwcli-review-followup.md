# Second bgwcli review follow-up

Baseline: `3b3bf9b`, reviewed source already copied to neo. The user authorized repairing findings
and running necessary commands. Validation initially ran on the Mac; the user subsequently required tests on neo through
`neoext`. No system services or timers may be enabled or started.

## Requirements and ownership

| Review item | Required outcome | Workstream |
| --- | --- | --- |
| R1 | Include optional form pages when verifying requested saved fields. | Save contracts |
| R2 | Retain acknowledgement and matching-state evidence before deciding a LAN change requires reconnection. Never poll a known moved address for a full timeout. | Save contracts |
| R3 | Read-only preflight failure, fixed-owner refusal, and empty/blocked planning must not manufacture new unfinished recovery intent. Persist intent before a mutation; preserve earlier or uncertain recovery. | Recovery |
| R4 | New locks must identify actual ownership despite PID reuse. Never evict an owner merely because a PID probe is denied or its marker is old. Document conservative legacy handling. | Session locks |
| R5 | Lock cleanup must remain bounded and must not replace the operation's result or exception. Preserve replacement owners and allow abandoned-marker reclamation. | Session locks |
| R6 | LAN-save output must distinguish applied state, rejected writes, known pre-send failures, and uncertain attempts. | Save contracts |
| R7 | Preserve session-pool wait/retry metadata through confirmation and preflight error context and coordination. | Save contracts / recovery |
| R8 | Treat the observed Wi-Fi no-change notification as a separate no-write outcome, verifying known requested values. Preserve Changes saved checks elsewhere. | Save contracts |
| R9 | Bound directory durability work to the state-storage boundary and newly created entries. Preserve private owned directories, existing ancestor permissions, retry durability, and errors from required sync operations. | Recovery |

R8's assertion that the acknowledgement is modeled only by synthetic fixtures contradicts the
user's live observations for allocation, services/NAT, firewall advanced, and Wi-Fi after Continue.
Subsequent live inspection established the Wi-Fi-specific no-change notification after a Save
redirect. It is now handled as a verified no-write outcome; the other save gates remain.

## Execution

- [x] Reproduce valid findings with focused tests before changing behavior.
- [x] Implement the three isolated workstreams and run their affected tests and Ruff.
- [x] Review each independently, including real client exception boundaries.
- [x] Integrate approved commits and run the combined local suite and Ruff.
- [x] Record each finding's disposition, validation, and remaining limitations.
- [x] Copy the validated revision to the existing neo checkout under the prior deployment authorization, retaining rollback data. Enable or start no services or timers.

No implementation should weaken uncertainty reporting to make a test pass. Losing a one-shot
acknowledgement remains unconfirmed; errors from required durability operations remain errors.
The small crash window between durable local intent and a remote write cannot be made atomic.

## Validation and disposition

Final tested source: `244a7ce`. Combined local suite: **1,094 passed, 40 skipped in 22.44 seconds**;
skips require absent captured-router fixtures. Ruff and diff checks passed. Independent task reviews
and final integration review approved all changes, including the manual autorestore merge boundary.

All nine items now have a resolution. R8 was refined by later live evidence: Wi-Fi returns
`No changes detected. Save not performed.` for an unchanged Save. The deployed CLI reports
`unchanged`, verifies the requested state, and does not claim a configuration write. Firewall
Advanced and other configuration writes retain their Changes saved acknowledgement checks.

New lock markers use an OS-held lifetime lock. Ambiguous legacy markers remain conservative;
permission errors are not proof that an owner died. Required checkpoint durability failures still
stop recovery, and the intent-to-remote-write crash window remains unavoidable.

Final review correction: actual LAN address changes retain applied/acknowledgement/state evidence
and the next address while final comparison requires reconnection. Mask/DHCP-only acknowledged
changes can still receive a closing comparison. An inactive internal LAN flag is omitted from
ordinary public plans to preserve their previous shape.


Final follow-on validation is recorded in `2026-10-06-bgwcli-third-review.md`: all features and
third-review repairs were integrated, independently reviewed, tested on neo, and deployed.
