# bgwcli audit fixes implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development to implement each task with regression tests and independent review. Tasks run in separate worktrees; the controller integrates their commits.

**Goal:** Fix all nine reproduced audit findings and the ten additional Claude review items without regressing the tested gateway restore flow.

**Architecture:** Repair client/session boundaries, add a private recovery checkpoint around the existing autorestore engine, and make snapshot selection/convergence consistent. Preserve fresh planning before writes and structured evidence after writes. Five independently tested workstreams share CLI integration points and were combined with independent review.

**Tech Stack:** Python 3.10+, standard library, pytest, Ruff, macOS/Linux.

**Spec:** `docs/superpowers/specs/2026-10-06-bgwcli-audit-fixes.md`

## Global Constraints

- Python 3.10+; standard-library runtime only; supported platforms are macOS and Linux, including Raspberry Pi ARM64.
- Preserve schema-2 backups, existing CLI commands, explicit commit confirmation, default secret redaction, and the current Save/Continue acknowledgement gates.
- Preserve the two-source MAC ownership preflight, single Clear per recovery invocation, 60-second settle, 180-second polling budget, and no new ownership read after its deadline.
- No live router calls, SSH, credential reads, or `scripts/e2e.py` from workers or reviewers. All tests run locally. No further copies to neo. Only the controller performs authorized read-only gateway smoke checks from the Mac.
- Do not refactor unrelated code or reformat whole files. Commit only task-owned changes. Do not spawn additional reviewers; the controller owns review dispatch.
- CLI integration overlaps are resolved by the controller when combining worktrees; preserve the other tasks' behavior.

## Review Focus

- Sensitive mismatches in both JSON and text; explicit secret-output opt-in must still work (Task 1).
- A live owner aged beyond the lock threshold, replacement ownership, and two reclaimers contending for a dead lock (Task 1).
- Changed router/baseline/scope and corrupt or unwritable recovery state must not authorize unrelated writes (Task 2).
- Authentication/extraction failure after confirmed writes must retain applied-step evidence and invalidate final-state claims (Task 2).
- Dependencies required for forwarding identity must remain available while unrelated sections are excluded; unknown firmware fields must not create impossible convergence (Task 3).

## Test command convention

```sh
env -u BGW_ACCESS_CODE -u BGW_FALLBACK_ACCESS_CODE -u BGW_HOST -u ROUTER_IP PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q --tb=short -p no:cacheprovider
ruff check --no-cache .
git diff --check
```

Use focused test files while implementing. The full suite includes loopback HTTP servers; the controller can run it with the existing approved escalation. No dependency installation is needed for this standard-library project and the available test environment.

### Task 1: Client and session correctness

**Files:** Modify `src/bgwcli/client.py`, `src/bgwcli/session.py`, `src/bgwcli/pages.py` as needed, and the `set`/`submit` portions of `src/bgwcli/cli.py`. Test in `tests/test_client.py`, `tests/test_session.py`, `tests/test_cli.py`, and focused new test files if clearer.

**Interfaces:** Preserve `get_cgi_page`, `post_cgi_page`, `post_form`, `with_router_session`, and command syntax. Share canonical page validation between CLI routing and direct client calls. Preserve explicit action `post_form` paths such as `wrestart.ha?1`.

- [x] Add and run failing regressions for all five client/session findings. The reproducer assertions now live in maintained pytest tests. Include text-output secret redaction, malicious page spellings before any transport call, HTTP 401/403/5xx probes, and lock contention.
- [x] Compare raw requested/live values before redaction, then sanitize public mismatch output using the existing secret-name rules and the explicit `include_secrets` option.

```python
raw_mismatches = {name: (wanted, live.get(name)) for name, wanted in requested.items()
                  if live.get(name) != wanted}
# Public records must use redact_value(name, value, include_secrets), not raw values.
```

- [x] Canonicalize permitted raw CGI page identifiers before the dangerous-page test and before direct client URL construction. Reject queries, paths, encoding, and traversal in page identifiers; retain explicit action routes separately.
- [x] Require a successful login/protected-page probe status before setting authenticated state. Preserve meaningful auth versus transport/response errors.
- [x] Keep an existing cooldown refusal outside the handler that records a newly observed pool-full event. Test that a remaining 10-second deadline stays 10 seconds without router access.
- [x] Repair lock acquisition/reclamation/release ownership. A live PID cannot be displaced by age alone, and release must prove current ownership. Serialize reclaim/replacement decisions where needed so ownership checks do not introduce another deletion race. Retain private files and bounded acquisition.
- [x] Run focused tests, Ruff, and diff checks; commit with `fix: harden client authentication and session coordination`. Write the implementation report with failing-before/passing-after evidence and exact commit IDs.

### Task 2: Durable recovery and post-write reporting

**Files:** Create `src/bgwcli/recovery_state.py` and `tests/test_recovery_state.py`; modify `src/bgwcli/autorestore.py`, CLI autorestore construction and closing restore verification in `src/bgwcli/cli.py`, related tests, and autorestore documentation.

**Interfaces:** Add an optional checkpoint argument to `run_autorestore` without breaking current callers. A small checkpoint abstraction should support `is_active()`, `begin()`, and `finish()`. Bind the file-backed implementation to router origin, actionable desired configuration fingerprint, and normalized page selection. The CLI supplies it; tests can inject a temporary file-backed implementation. Do not change snapshot-selection calls owned by Task 3.

- [x] Add and run failing tests for the recovery review: partial recovery must resume in a second invocation; post-write auth/extraction exceptions must return structured evidence. Also test changed identity, dry-run state preservation, corrupt state, and failure to persist before any write.

```python
# Two invocations must share a real temporary checkpoint, not the same process's loop state.
# First invocation restores some entries and returns not-converged.
# Second invocation sees a partial configuration, resumes the checkpoint, and restores remaining entries.
# A different baseline/router/selection retains ordinary no-reset behavior.
```

- [x] Implement private atomic checkpoint storage under the application's state directory. Persist recovery intent before the first recovery write (including a required Clear), recompute remaining work from fresh pages on each invocation, and remove a matching checkpoint only after verified convergence. Exclude timestamps, documentary tables, and secrets from stored metadata; store only a fingerprint of desired values.
- [x] Preserve the current-invocation stop after a failed write. Resumption in a later invocation must always refetch and replan; never store/replay raw POST payloads. Ordinary drift without a matching checkpoint still does not trigger recovery.
- [x] Record execution/pass evidence before closing verification. Catch supported authentication, transport, and extraction failures after writes, return error status/exit 2 with execution evidence, and use an unavailable final diff rather than stale or guessed state. Cover both `restore --json` and `autorestore`.
- [x] Document checkpoint location, identity matching, lifecycle, and intentional operator abandonment of recovery. Keep credentials out of the state file.
- [x] Run focused tests, Ruff, and diff checks; commit with `fix: persist autorestore recovery intent and execution evidence`. Write the implementation report with exact test and commit evidence.

### Task 3: Scoped snapshots and achievable convergence

**Files:** Modify `src/bgwcli/snapshot.py`, `src/bgwcli/snapshot_diff.py` if metadata handling requires it, `src/bgwcli/restore.py`, the fetch/extract portions of CLI diff/restore and autorestore, related tests, and scope documentation.

**Interfaces:** Preserve full-snapshot defaults. Introduce a shared selected-page/dependency resolver rather than duplicating fetch rules across commands. An optional `selected_pages` extraction argument may project selected data while retaining dependency pages for MAC lookup. Task 2 owns recovery checkpoint/reporting logic; do not change it in this worktree.

- [x] Add and run failing regressions for scoped restore/diff/autorestore with an unrelated malformed forward, a selected forwarding section needing MAC lookup dependencies, and a live-only form field.

```python
live = replace(saved, forms={**saved.forms,
    "dosprotect": {**saved.forms["dosprotect"], "new_firmware_option": "on"}})
diff = diff_snapshots(saved, live, pages=("dosprotect",))
assert not build_restore_plan(diff, saved, pages, RestoreOptions(pages=("dosprotect",)))
assert restore_converged(diff, False)
```

- [x] Resolve the selected read set and project extraction consistently for all three commands. Fetch only requested pages and genuine dependencies. Missing metadata must not be invented or reported as a firmware change. Keep missing-requested-page warnings and excluded-allocation preflight guards.
- [x] Treat only form differences with a requested backup value as unmet convergence. Ignore live-only fields that planning intentionally preserves, including with `--prune`; requested missing/different fields still block convergence.
- [x] Run focused tests, Ruff, and diff checks; commit with `fix: isolate selected snapshot sections and convergence`. Write the implementation report with failing-before/passing-after evidence and commit IDs.

### Task 4: Accurate save confirmation and write outcomes

**Files:** `src/bgwcli/restore.py`, `src/bgwcli/autorestore.py`, relevant CLI/form verification integration, operation formatting/types, targeted tests and documentation.

**Interfaces:** Build on reviewed Tasks 1–3. Preserve durable recovery checkpoints and structured verification reports. Add explicit write-phase/outcome metadata only where needed; do not infer whether a write occurred from a generic failed label alone. Preserve public redaction.

- [x] Reproduce C1/C2/C3/C5 with synthetic transports: LAN-moving Save, a transient 503 followed by success, terminal auth after a sent Save, stale success banner with missing desired state, lost acknowledgement with known/unknown readback, and a pre-final-write failure that succeeds on a later pass.
- [x] Use a shared Changes-saved matcher (C8). Require known requested postconditions for restore confirmations without retaining duplicate secret payloads in public result objects. Keep `set` mismatch reporting/redaction intact.
- [x] Classify known non-writes separately from potentially sent final configuration writes. Permit bounded fresh-snapshot retries only for safe non-write cases. Simplify redundant failure checks (C6).
- [x] Handle expected LAN reconnection explicitly, with unavailable final verification and the next address where known. Never poll the old address for the full budget or silently report success.
- [x] Retry only transient verification reads within the deadline; keep auth/session-pool behavior and cooldown propagation correct. Terminal results must explain uncertain write state accurately.
- [x] Run covering tests, full local suite and Ruff; commit and report exact evidence for review.

### Task 5: Reliable and efficient allocation preflight

**Files:** `src/bgwcli/allocation_preflight.py`, snapshot fetch metadata if needed, CLI/autorestore preflight presentation, related tests and documentation.

**Interfaces:** Build on reviewed Tasks 1–3. Retain the union of both ownership sources, exact normalized MAC matching, raw structural validation, all-pending-request rechecks, and the existing rescan budget. Coordinate shared CLI/autorestore edits with Task 4 during integration.

- [x] Reproduce C4/C7/C9/C10: pre-write connection failure, pending dry-run display, malformed then valid rescan responses, persistent malformed responses, a different MAC's fixed allocation, cached initial raw response reuse, and mandatory fresh reads after Clear.
- [x] Preserve typed errors before writes; distinguish retryable post-Clear verification errors and never declare availability from a partial or invalid pair of sources.
- [x] Classify fixed allocations from IP Allocation evidence, retain that classification during source deduplication, and refuse them immediately without Clear. Ordinary stale DHCP/discovery conflicts still use one Clear and bounded polling.
- [x] Create one report builder shared by restore/autorestore (C8). Report pending/blocked planning explicitly; retain structured read-only failure reasons.
- [x] Reuse initial allocation raw HTML only with validated same-invocation freshness/provenance. Fetching allocation last or checking age can enforce freshness. Keep raw HTML internal and always refresh after mutation.
- [x] Run covering tests, full local suite and Ruff; commit and report exact evidence for review.

## Integration and delivery

- [x] Review each task independently against the spec and its focused tests.
- [x] Integrate reviewed commits on the original working branch, resolving the documented CLI/autorestore overlaps without dropping either behavior.
- [x] Run the full suite and Ruff; use a fresh whole-branch review and address concrete findings.
- [x] Validate the integrated code locally and perform authorized read-only gateway smoke checks from the Mac. Do not copy or deploy to neo. Keep all existing backups and credentials protected.
- [x] Record all nine finding outcomes, tests, deployment paths, and remaining limitations. Do not enable a timer, factory-reset the gateway, or publish/push without an explicit request.

## Completion evidence — 2026-10-06

All nine original findings and additional items C1–C10 are resolved on
`fix/ip-allocation-save-confirmation`. Final tested code commit: `d76705c`.

- Task 1 closed five client/session findings: secret mismatch redaction, canonical page guards,
  HTTP authentication validation, preserved cooldown deadlines, and process-owned session locks.
- Task 2 closed two recovery findings: durable matching recovery intent across invocations and
  retained execution evidence when closing verification fails.
- Task 3 closed two snapshot findings: selected-section isolation and achievable convergence
  when firmware exposes extra fields absent from the backup.
- Task 4 addressed C1/C2/C3/C5/C6 and C8's shared acknowledgement matcher: reconnect outcomes,
  transient verification retries, requested-state checks, honest lost acknowledgements, explicit
  write evidence, and safe retries of known non-writes.
- Task 5 addressed C4/C7/C9/C10 and C8's shared preflight report: typed errors, incomplete plans,
  fixed-owner refusal, bounded invalid-response retries, safe raw-response reuse, and retained
  rescan evidence and cooldown coordination through extraction/planning failures.

Final combined local suite: **1,031 passed, 40 skipped in 22.59 seconds**. The skipped tests require
captured-router fixtures that are absent. Ruff and diff checks passed. Every task received an
independent review; the final integration review and its focused correction were approved.

The Mac's read-only gateway comparison at `2dd47c0` returned `identical: true` against the October 6
core backup. The subsequent `d76705c` change only repairs failure reporting after a rescan;
its new regression and the full suite passed locally. No updated code was copied to neo.

A lost one-shot acknowledgement remains unconfirmed even if readback matches; the CLI does not
invent acknowledgement or blindly repeat the write. LAN changes can require reconnection, and a
client must renew DHCP to use a restored reservation. Configuration verification does not establish
end-to-end forwarded traffic. The earlier reported NAT failure's original cause was not reproduced.
