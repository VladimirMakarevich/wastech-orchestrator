# Testing rules

The source of truth is the code (`src/wastech_orchestrator/`, `tests/`); see the [Functional Map](../../docs/functional/index.md).

## Levels

### Unit

Cover pure logic without external processes:

- configuration and task override validation;
- route resolution and the allowlist;
- each provider's command builder (without actually running the CLI);
- parsing of structured output;
- error classification (`ProviderError` → class);
- state machine transitions;
- secret redaction and path normalization;
- retry / fallback / fix-cycle limits, the global fix-iteration budget, and the stuck condition;
- repeated stage execution uses distinct persisted stage-run artifact paths, including an integration case with two fixing cycles whose provider attempt counters both start at `1`;
- the `refinement` skip decision (already-complete task vs. needs enrichment);
- the single-active-task slot (a new task does not start while another is active);
- terminal cleanup and auto mode: config default/rejects, checkout to `base_branch` before next pickup, auto off leaves the next task pending, unsafe cleanup blocks continuation;
- the `watch` poll loop (§8.3): `poll_interval_seconds` default/`>=0` validation, the loop refreshes (`refresh_base`) before each tick, `0` is a single pass, and a bounded loop sleeps between ticks but not after the last;
- the decomposition accept/reject decision and per-subtask vs. global counter semantics (§5.1);
- the §19 validation gate: each Phase-A reason code, required/optional fields, duplicate-id, the injection-token scan, and Phase-B classification;
- `init` idempotency (a second run is all-skipped; never overwrites `config.yaml`; `--dry-run` is a no-op);
- the git footprint (§21): scoped staging excludes `tasks/`/`logs/`/`workspace/` (and, under in-repo, the root runtime files `state.db`/`config.yaml`), the `.git/info/exclude` append is idempotent, the audit commit is orchestrator-only, the preflight rejects tracked artifacts under `external`/`exclude_local` but is **skipped** under `commit`, and the validator rejects illegal mode pairings;
- the `summary` stage (§5.2): the handoff artifact is produced, and a provider failure falls back to a deterministic minimal summary without blocking publishing.

### Integration

Use **fake CLI executables** (stub scripts) rather than the real Codex/Claude:

- a successful run;
- `binary_not_found`, `authentication_failed`, `rate_limited`, `timeout`, `process_crashed`, malformed output;
- an infrastructure error **after** files have been changed;
- a successful fallback;
- fallback being forbidden on a quality failure.

### End-to-end

On a temporary Git repository:

- a vague task triggers `refinement` (→ `task.enriched.md`); an already-complete task skips it;
- Claude performs planning/implementation, Codex performs review;
- failed checks trigger `fixing`;
- success → exactly one commit, push, and PR;
- terminal cleanup checks out the base branch after the terminal task;
- auto mode enabled → two pending tasks run sequentially with a base-branch checkout between them; auto mode disabled → the second task remains pending;
- a restart does not duplicate publishing;
- recovery continues each persisted checkpoint from `validated` through `fixing`; resuming `testing`, `reviewing`, or `fixing` must not invoke implementation again, and fixing context and counters must survive the restart;
- the completed-tasks ledger gains exactly one record per terminal transition;
- a large task with decomposition enabled → `n` subtasks, `n` sequential commits on one branch, one PR; a restart resumes at `k` without a duplicate commit (§5.1);
- a broken task → quarantined to `tasks/rejected/` as `failed`, writes `validation_report.json`, with no branch/provider (§19);
- test configuration paths with side effects, including `validation.quarantine_folder`, are isolated under the test's temporary directory and never write into the repository checkout;
- in every git footprint mode the code commit excludes `tasks/`/`logs/`/`workspace/` (§21);
- a successful task produces `summary.md` (what / how / integration / why) which becomes the PR body (§5.2);
- exhausting a fix loop or the global fix-iteration budget → `manual_action_required` + failure report; an unrecoverable error → `failed`.

## Principles

- Tests are deterministic and isolated (no network calls and no real CLIs in unit/integration).
- External processes and time are mocked/injected.
- **The suite must pass on Windows, Linux, and macOS.** No POSIX-only assumptions in tests: compare paths via `pathlib`/`Path.as_posix()` (never a hardcoded `/`), normalize newlines in byte-compares, guard `signal.SIGKILL` and other POSIX signal constants with `getattr`, and exercise **both** platform branches of any `os.name`-split logic by injecting the platform seam rather than depending on the host OS. See the cross-platform rules in [coding-style.md](coding-style.md).
- Every behavior change is accompanied by a test.
- The goal is high coverage of critical paths (router, fallback, state machine, security/redaction), not a percentage for its own sake.
- A green `pytest` is a mandatory precondition for committing and for transitioning between implementation stages.
