# Acceptance criteria — Tracker connector

Verifies [requirements.md](requirements.md). `AC-C*` run in the connector repository against a fake `gh` and a fake `worc`; `AC-W*` run here. "Real run" means one labelled issue on a throwaway GitHub repository with a real `worc watch`.

## Scenarios

### AC-1 — poll and single pass (FR-C1)

- **Given** a valid configuration with `poll_interval_seconds: 300` and a fake `gh` returning two open items
- **When** `worc-connect watch --once` runs
- **Then** exactly one `gh issue list` call is recorded, the tick completes, and the process exits 0 without writing a PID file; with `watch` (no `--once`) a PID file exists while it runs and a second `watch` refuses to start.

### AC-2 — gate is explicit and fail-closed (FR-C2)

- **Given** `gate.labels: [worc]`, items #1 (labelled `worc`), #2 (unlabelled), #3 (labelled `worc` by an author outside a non-empty `gate.authors`)
- **When** a tick runs
- **Then** only #1 is staged; #2 and #3 leave no row beyond the watermark and no tracker call with side effects.
- **And given** a configuration with `gate.labels: []`, `gate.authors: []` and no `allow_all: true` → the connector refuses to start with exit 2 naming `gate`.

### AC-3 — exactly one task per item, across a crash (FR-C3)

- **Given** item #142 gated and a connector that is killed after writing `tasks/preparing/gh-142.md` and before `worc promote` returns
- **When** the connector restarts and ticks
- **Then** `worc promote gh-142` is run again, the fake `worc` moves the one file, one row exists with `task_id = gh-142`, `phase = queued`, and no second file was ever written.
- **And given** #142 later reaches `done` and the trigger label is removed and re-added → the next task id is `gh-142.2`, never `gh-142`.

### AC-4 — the generated task passes worc's gate unchanged (FR-C4)

- **Given** an item whose title is `-rm -rf /; echo | $(whoami)` and whose body is 300 000 bytes with one 10 000-byte line
- **When** the task is built
- **Then** the title contains none of the rejected tokens and does not start with `-`; the body is truncated under 262 144 bytes and 8 192 bytes per line with a visible marker and the item URL; the front matter contains only keys from worc's allowed set; and the file, fed to worc's real validation gate in a test, passes Phase A.
- **And given** an item title that sanitizes to nothing → the title is `Issue #<n>`.
- **And given** a long title → `branch_name` is a valid ref of at most 50 characters and not equal to the base branch.

### AC-5 — provenance (FR-C5)

- **Given** a gated item
- **When** the task body is built
- **Then** it opens with a line naming the tracker, the item number, the URL and the author, followed by the item text; no `## Acceptance criteria` section is synthesized.

### AC-6 — dispatch fields come only from configuration (FR-C6)

- **Given** `task.commit_type_by_label: { bug: fix }` and no `auto_merge` key in the configuration
- **When** an item labelled `bug` is built
- **Then** the front matter carries `commit_type: fix` and no `auto_merge` key; with `task.auto_merge: true` configured the key appears as `auto_merge: true`.

### AC-7 — handoff only through the public ingress (FR-C7)

- **Given** a full tick on a gated item
- **When** the recorded process launches are inspected
- **Then** the only `worc` invocation is `promote <id>` as an argv list, no `git` process was launched, and the only path written under the clone is `tasks/preparing/<id>.md`.

### AC-8 — dry run writes nothing (FR-C8)

- **Given** three gated items
- **When** `watch --once --dry-run` runs
- **Then** stdout names the three items, their task ids, branch names, labels and comments; no file exists in `tasks/preparing/`, the state database is unchanged, and the fake `gh` recorded no `edit`, `comment`, `close` or `label create` call.

### AC-9 — one state label at a time, idempotent (FR-C9)

- **Given** an item carrying `worc:queued`
- **When** the connector moves it to `in-progress`, then reconciles the same state again
- **Then** the first tick records `--remove-label worc:queued --add-label worc:in-progress`; the second records no label call.

### AC-10 — comments (FR-C10)

- **Given** the lifecycle of one item
- **When** the task is queued, its PR is found, and (in a second scenario) it ends in `manual_action_required`
- **Then** three comment bodies exist in the recorded `--body-file` files: one naming `gh-142`, one containing the PR URL, one naming `manual_action_required` and `worc status gh-142`; none contains the item's body text, a log line, or a diff.

### AC-11 — close on merge is configurable (FR-C11)

- **Given** the PR for the task branch reports `mergedAt` set
- **When** a tick runs with `close_on_merge: true` → `gh issue close` is recorded with a comment file; with `close_on_merge: false` → no `close` call, the label still becomes `worc:done`.

### AC-12 — status from the sources of truth (FR-C12)

- **Given** a state database deleted between ticks, an item carrying `worc:pr-open`, and `worc list --format json --all` reporting `gh-142` as `done`
- **When** the connector ticks
- **Then** the row is rebuilt from the label and `worc list`, the PR is looked up by `gh pr list --head worc/gh-142-...`, and no duplicate task is created.

### AC-13 — tracker-agnostic core (FR-C13)

- **Given** the core package
- **When** an import-boundary check runs
- **Then** no module under `worc_connect/core/` imports from `worc_connect/trackers/` except `trackers/base.py`, and a second, in-test `TrackerAdapter` implementation drives a full tick without a GitHub-specific code path.

### AC-14 — adapters are optional installs (FR-C14)

- **Given** the package built without the `github` extra
- **When** the configuration names `tracker: github`
- **Then** startup fails with a message naming the extra to install; with the extra, the adapter is resolved through the entry-point group, not by a hard-coded import.

### AC-15 — triage switch (FR-C15)

- **Given** `triage.enabled: false` (default)
- **When** a gated item is processed → one task is created and no flow file is written into `.worc/flows/`.
- **Given** `triage.enabled: true` and `install-flow` has been run
- **When** a gated item is processed → the first task carries the triage `task_type` and `priority: high`; when the fake `worc` reports it `done` with a report whose verdict is `actionable`, the same builder produces the implementation task; with verdict `needs-info` no implementation task is created and the item receives `worc:needs-info` and a comment.

### AC-16 — the owner edits and merges the PR by hand (FR-C16)

- **Given** a row in `pr-open` with PR number 201 stored, and a fake `gh` whose `pr view 201` now reports a changed title, two more commits, `state: MERGED` with `mergedAt` set, and whose `pr list --head <branch>` returns nothing because the branch was deleted
- **When** a tick runs
- **Then** the item is closed (if configured) and labelled `worc:done`; the decision needed no `pr list --head` call; no `git` process was launched.

### AC-17 — follow-up on an open PR (FR-C17)

- **Given** a row in `pr-open` whose PR is `OPEN`, and the trigger label removed and re-applied
- **When** a tick runs
- **Then** the new task `gh-145.2` carries `branch_mode: existing` and `branch_ref` equal to the previous branch and no `branch_name`; **given** the PR is `MERGED` instead → the new task carries a fresh `branch_name` and no `branch_ref`.

### AC-W1 — `references:` validated and appended (FR-W1)

- **Given** a task with `references: ["Fixes #142", "https://example.test/AB-7"]`
- **When** the flow reaches `publish` with a PR to open
- **Then** the body handed to `gh pr create` ends with a `## References` section listing both lines verbatim, and the `<id>.summary.md` finalize wrote is byte-identical to the summary before publishing.
- **And given** `references: "Fixes #142"` (not a list), `[]`, `[""]`, `["a\nb"]`, a 201-character entry, or 17 entries → the gate rejects the task with `INVALID_REFERENCES` (the shape check in `_check_field_types` runs before the injection scan); **given** `["-flag"]` or `["a; b"]` → `INJECTION_SUSPECTED` from the scan; in every case the file is quarantined and no branch is created.
- **And given** an open PR already on the task head (the chain-PR case) → the body appended by the reuse path carries the block too.
- **And given** a task with `publish: push` → no references block is built and no PR is opened.

### AC-W2 — `pr_url` in the JSON listing (FR-W2)

- **Given** a store with one task that opened a PR (a completed `pr` publish-op row with the URL as `result_ref`) and one that did not, plus one file in `tasks/pending/`
- **When** `worc list --format json --all` runs
- **Then** the first entry carries `"pr_url": "<url>"` and the second `"pr_url": null`;
- **When** `worc list --format json` (default view) or `--pending` runs
- **Then** the file-derived pending entry carries `"pr_url": null` (the `--all` view holds DB rows only, so the pending entry is asserted where it appears).

### AC-W3 — `rejected` section in the `--all` listing (FR-W3)

- **Given** a ledger with two validation-reject records for `gh-9` (the latest with `validation_reason: injection_suspected`) and no `tasks` row for it, a task `gh-10` with one reject record **and** a `tasks` row (re-submitted under the same id and run), and a task `gh-11` with a row only
- **When** `worc list --format json --all` runs
- **Then** exactly one `rejected` entry exists, for `gh-9`: `status: "rejected"`, `validation_reason: "injection_suspected"`, `rejected_at` equal to the latest record's `finished_at`, `title`, `branch` and `pr_url` all `null`; `gh-10` and `gh-11` appear only as ordinary rows.
- **And when** `worc list --all` (table) runs → a `rejected:` section lists `gh-9` with its reason; **and when** the default view or `--format ids` runs → no rejected id is printed.
- **And given** no ledger file → the `--all` listing has no `rejected` entries and exits 0.

## Edge cases & error states

- **AC-E1** — `gh` exits with an auth error → the tick is skipped, logged as `TrackerAuth`, no state changes, the process stays up for the next tick.
- **AC-E2** — `worc promote` reports "already in pending — not overwriting" → the row becomes `queued`, no error is surfaced to the tracker.
- **AC-E3** — worc's gate rejects the task (the fake `worc` moves the file out of `pending/` and its `list --format json --all` fixture has no row for the id — which is what a real reject looks like from outside `.worc/`) → the row becomes `failed`, the label is `worc:failed`, and the comment names the task id and `worc status <id>` and contains no worc text. With a fake `worc` whose `--all` listing carries a `rejected` entry for the id (FR-W3, adopted in phase 06) the comment additionally names its `validation_reason` — that string and nothing else from the entry.
- **AC-E4** — the PR is closed without merge → `failed` with a comment; nothing is re-queued automatically.
- **AC-E5** — the item loses the trigger label while `queued` → nothing changes on the task; a log line notes it.
- **AC-E6** — the watermark is ahead of an item's `updatedAt` because of clock skew inside the overlap window → the item is still listed and, having a row, is not duplicated.
- **AC-E7** — `.worc-connect/connect.stop` appears mid-sleep → the loop exits before the next tick and removes its PID file.
- **AC-E8** — `tasks/preparing/` does not exist → the connector creates it under `paths.tasks_dir` as read from worc's `config.yaml` (or the default `tasks`), then proceeds.
- **AC-E9** — the PR is closed without merge, then reopened → the row goes `failed` with one comment, then back to `pr-open` on the next tick that sees `state: OPEN`; no second failure comment is posted for the same closure.

## Non-functional checks

- **AC-N1 (NFR-1)** — the suite runs on Windows and POSIX in CI; task files are LF with `newline=""`; launcher resolution finds `gh.exe` / `worc.cmd` in a Windows test; stop is by sentinel only.
- **AC-N2 (NFR-2)** — a test greps every recorded argv, every log line and every comment body for a sentinel string planted in the item body and finds it only in the task file.
- **AC-N3 (NFR-3)** — no configuration key, state column or log line holds a token; the connector forwards no additional environment to `gh` or `worc`.
- **AC-N4 (NFR-4)** — the builder's allowed-key set is a strict subset of worc's `ALLOWED_TASK_KEYS` and never includes `nodes`, `subtasks`, `decomposition`, `trust_level` or `prompt_audit`.
- **AC-N5 (NFR-5)** — an unparseable configuration, an unknown `tracker`, or unparseable `worc list` JSON each stop the tick / process with exit 2 and a named reason, and change no state.
- **AC-N6 (NFR-6)** — a tick with nothing new records no writes; the connector's dependency set contains no model SDK.
- **AC-N7 (NFR-7)** — a tick while the clone is checked out on a task branch stages and promotes normally; no `git` process is ever launched by the connector.
- **AC-N8 (NFR-8)** — every action emits one log line with `item=`, `task=`, `action=`, `result=`; `worc-connect status` lists rows, phases and the watermark.

## Verification method

| AC | How it is verified | Where |
| --- | --- | --- |
| AC-1, AC-2, AC-8, AC-9, AC-10, AC-11, AC-E1, AC-E7 | fake-`gh` integration tests (recorded argv, JSON fixtures) | connector `tests/` |
| AC-3, AC-7, AC-12, AC-E2, AC-E3, AC-E4, AC-E8, AC-N7 | fake-`gh` + fake-`worc` integration tests | connector `tests/` |
| AC-16, AC-17, AC-E9 | fake-`gh` + fake-`worc` integration tests (PR fixtures by number) | connector `tests/` |
| AC-4 | unit tests for sanitizer/truncation + a test that imports worc's real `task.validation_gate` against the generated file | connector `tests/` (worc as a test dependency) |
| AC-5, AC-6 | unit tests on the builder | connector `tests/` |
| AC-13 | import-linter contract in the connector repository | connector CI |
| AC-14 | packaging test with and without the extra | connector CI |
| AC-15 | fake-`worc` integration test with a fixture report | connector `tests/` |
| AC-W1 | gate unit tests; publish-node + `GitManager` test with a recorded `gh` runner | `tests/` here |
| AC-W2, AC-W3 | `cmd_list` tests with a seeded store and a seeded ledger | `tests/` here |
| AC-N1 | CI matrix (Windows + Linux) | connector CI; worc CI already runs both |
| AC-N2 … AC-N6, AC-N8 | targeted unit tests and a dependency check | connector `tests/`, connector CI |
| End to end | one real run: a labelled issue on a throwaway repository, `worc watch` and `worc-connect watch` side by side, through merge and close | manual, recorded in the connector README |
