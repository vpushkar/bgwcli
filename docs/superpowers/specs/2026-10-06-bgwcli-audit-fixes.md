# bgwcli audit fixes

The user authorized fixing all nine findings from the October 6 review. The earlier save-confirmation and MAC-ownership work is checkpointed in `3cc679e`, with 831 passing automated tests on Mac and Pi and completed live recovery tests. This change repairs existing command behavior while retaining that baseline.

## Required behavior

1. Failed `set` verification compares raw values internally but redacts sensitive wanted/live values in default JSON and text output. Explicit `--include-secrets` retains its documented opt-in behavior.
2. `autorestore` remembers an unfinished recovery across invocations. A new run resumes only a matching router, desired configuration, and selection, and always builds actions from a fresh snapshot. It clears progress after verified convergence. Ordinary unrelated drift continues to return `no-reset`.
3. Generic page commands cannot bypass dangerous-page guards through extensions, query strings, URL paths, encoding, or mixed spelling. Valid CGI identifiers and documented aliases remain usable. Explicit restart actions retain their supported query-bearing `post_form` routes.
4. A running process retains its session lock even beyond the stale-age threshold. A former owner cannot delete a replacement owner's lock. Dead stale owners can be reclaimed without allowing concurrent writers.
5. A command refused by an existing local cooldown preserves its deadline. A newly observed gateway pool-full response starts or refreshes cooldown.
6. Failed HTTP login/protected-page probes cannot mark the session authenticated or cache authenticated state.
7. Explicit `--include` scopes fetching and extraction to selected sections and necessary dependencies. An unrelated malformed forward must not block a firewall-only comparison or restore. Default full capture/restore remains compatible.
8. Failures during verification after writes retain execution evidence and return a structured error. Unknown final state is not represented as a clean or stale final diff.
9. Live-only form fields absent from the backup do not prevent restore convergence. Missing or different requested values still prevent convergence, with or without `--prune`.

## Design boundaries

- Python 3.10+; standard-library runtime only; supported platforms are macOS and Linux, including Raspberry Pi ARM64.
- Preserve schema-2 backups, existing CLI commands, explicit commit confirmation, default secret redaction, and the current Save/Continue acknowledgement gates.
- Preserve the two-source MAC ownership preflight, single Clear per recovery invocation, 60-second settle, 180-second polling budget, and no new ownership read after its deadline.
- Recovery progress is private local state, not a saved POST queue. Store only recovery identity/metadata, never passwords or complete backup contents. Publish state atomically with owner-only permissions. Read-only dry-runs do not start or clear recovery state.
- Recovery identity ignores capture timestamps and documentary tables, and changes when the actionable desired configuration or scope changes. A different host or baseline must not inherit recovery intent.
- Failed writes still stop the current invocation. A later invocation uses fresh state before any retry; this work does not add an unrelated permanent manual-suspension workflow.
- Perform all dangerous-route, authentication-failure, lock-race, and recovery-error reproductions offline. All tests run on the local Mac; make no further copies to neo. No gateway reset, security-setting changes, or unattended timer enablement is part of these fixes.
- Keep current code and data APIs compatible where possible. Add optional recovery-store injection for tests/library callers; the real CLI must use durable recovery state.

## Validation

Each finding needs a regression that fails on `3cc679e` and passes with its fix. Retain the existing tests. Use fake transports and temporary state directories for failure cases, including actual cross-invocation recovery, owner replacement, HTTP error statuses, scoped extraction, and post-write exceptions. Run the complete suite and Ruff locally. Use only authorized read-only gateway checks from the local Mac. Do not deploy or copy to neo. Do not repeat disruptive live experiments unless explicitly requested.

## Additional Claude review items

The user supplied ten additional items while implementation was starting. Apply these requirements after the first three workstreams are integrated:

- C1: A planned LAN address/mask/DHCP change can invalidate the current connection. Do not spend the acknowledgement budget polling an address known to be changing, or mislabel expected loss of reachability as a rejected Save. Preserve the sent-write evidence and return an explicit reconnect/verification-required outcome with the known next address. Never claim confirmed convergence without evidence or automatically change the local machine's network.
- C2: During post-write verification, transient connection failures and transient HTTP statuses such as 503 may retry reads within the existing budget. Never repeat the write. Terminal authentication/verification errors must retain the fact that the change was sent and guidance to verify its state before retrying.
- C3: For restore operations whose desired postcondition is known, a success banner alone cannot confirm a dropped write. Require the requested state as well as the acknowledgement before advancing. If a one-shot acknowledgement is lost, do not fabricate it: report the result as unconfirmed, distinguish observed state from observed acknowledgement, and do not automatically resubmit. The gateway exposes no operation ID, so absolute banner freshness cannot be manufactured client-side.
- C4: Preserve connection/authentication/session-pool exception types through initial allocation inspection. A connection failure before any write retains autorestore's router-unreachable behavior; a failure after Clear must not masquerade as a no-write outcome.
- C5: Distinguish failures before a final configuration write from ambiguous/failed writes. Known non-write conditions such as an unopened allocation entry may retry in bounded later autorestore passes after a fresh snapshot. A potentially sent final write still stops the invocation.
- C6: Remove the redundant failed-step/stopped-at checks while retaining one authoritative failure classification and useful error details.
- C7: A dry-run whose configuration plan depends on a rescan must report that the plan is pending, not a completed plan with zero actions. Unverifiable ownership may block planning, but read-only callers should receive a structured reason rather than misleading emptiness.
- C8: Share allocation-preflight report construction between restore and autorestore and use one Changes-saved text matcher.
- C9: After Clear, transient/incomplete ownership responses may be retried within the polling budget; they must never count as free ownership. A conflicting fixed allocation is a durable assignment that Clear does not release: fail promptly with its MAC/IP and a manual-release explanation, without issuing Clear. Do not infer a fixed reservation merely from Devices' `static` label.
- C10: Avoid a duplicate initial IP Allocation GET when a sufficiently fresh successful raw response from the same snapshot collection is available. Retain raw validation/provenance, never substitute a lossy parsed table for validation, and always refresh both sources after Clear. Do not expose raw HTML/credentials in public JSON. If safe reuse is unavailable, perform the read.
