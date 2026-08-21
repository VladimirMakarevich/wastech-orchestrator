# Testing rules

The source of truth is the code (`src/wastech_orchestrator/`, `tests/`).

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
- frozen control bundles on both platform branches: POSIX keeps one executable per tool, while Windows preserves the same-name launcher and payload with separate manifest digests;
- custom-tool outcome classification: a silent stderr-only crash parks without charging a fix iteration, an ordinary non-zero linter report still fails as quality, JSON outcomes remain authoritative, and a second identical no-finding failure stops the loop;
- retry / fallback / fix-cycle limits, the global fix-iteration budget, and the stuck condition;
- repeated stage execution uses distinct persisted stage-run artifact paths, including an integration case with two fixing cycles whose provider attempt counters both start at `1`;
- the `refinement` skip decision (already-complete task vs. needs enrichment);
- the single-active-task slot (a new task does not start while another is active);
- terminal cleanup and auto mode: config default/rejects, checkout to `base_branch` before next pickup, auto off leaves the next task pending, unsafe cleanup blocks continuation;
- the `watch` poll loop: `poll_interval_seconds` default/`>=0` validation, the loop refreshes (`refresh_base`) before each tick, `0` is a single pass, and a bounded loop sleeps between ticks but not after the last;
- the decomposition accept/reject decision and per-subtask vs. global counter semantics;
- the validation gate: each Phase-A reason code, required/optional fields, duplicate-id, the injection-token scan, and Phase-B classification;
- `init` idempotency (a second run is all-skipped; never overwrites `config.yaml`; `--dry-run` is a no-op);
- the git footprint: scoped staging excludes `tasks/`/`logs/`/`workspace/` (and, under in-repo, the root runtime files `state.db`/`config.yaml`), the `.git/info/exclude` append is idempotent and writes one rule per assigned path-list element (never the unsplit list), the audit commit is orchestrator-only, the preflight rejects tracked artifacts under `external`/`exclude_local` but is **skipped** under `commit`, and the validator rejects illegal mode pairings;
- environment-policy branches: Windows name merging and `SystemRoot` checks are tested with an injected platform, path-list parsing covers both `:` and `;` on every host, and `run`/`watch`/`rerun` are covered at the validated-config entry point rather than only through helper calls;
- the `summary` stage: the handoff artifact is produced, and a provider failure falls back to the deterministic report rendered from the run's recorded facts without blocking publishing — that report is a pure function of `state.db` plus the task's artifacts, so two renders of one run must be byte-identical (the same contract the finalize packet keeps);
- on-disk retention: `logs clean` reaches every entry of the logs root (task dirs **and** the daemon logs) while keeping the ledger unless `--all`, accepts no flag it then ignores, refuses while a task is active, and holds the daemon logs back while a daemon is live; automatic run-artifact eviction fires only on a successful terminal with the switch on and a cleanly sealed exchange, never touches quarantined evidence, and the `rerun` status precondition that makes it safe is pinned so widening it fails loudly; `install --reconfigure` bounds its own `config.yaml.bak-*` / `flows.bak-*` / `tools.bak-*` series and never matches the operator's `state.db*.bak*`.

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
- a large task with decomposition enabled → `n` subtasks, `n` sequential commits on one branch, one PR; a restart resumes at `k` without a duplicate commit;
- a broken task → quarantined to `tasks/rejected/` as `failed`, writes `validation_report.json`, with no branch/provider;
- test configuration paths with side effects, including `validation.quarantine_folder`, are isolated under the test's temporary directory and never write into the repository checkout;
- in every git footprint mode the code commit excludes `tasks/`/`logs/`/`workspace/`;
- a successful task produces `summary.md`, which becomes the PR body — the supervisor layer's synthesis when it wrote one, otherwise the deterministic report (changes, steps, checks, gate verdicts, follow-ups, skipped nodes), and on **every** terminal the same single renderer writes it;
- a synthesis that **collapsed** is not published as one: prose below the finalize floor is discarded (no `summary.md`, so the run is degraded and the report becomes the body), and the `supervisor_final` row's `summary_written` states what actually reached disk rather than what the turn returned — the guard used to be "does the file exist", which a four-byte summary passed;
- a follow-up an operator can act on: the reviewer's `fix` reaches `action_hint`, a derived `title` is not a truncated copy of its own `rationale`, and when a reused chain PR's body is compacted to fit GitHub's limit the follow-up sections are surrendered **last** and the stub names the run host, not a repository path;
- the same follow-ups **accumulate** in `.worc/follow-ups.md` across tasks — append-only (no entry lost or overwritten, and the same item found by two tasks appears twice), fed from both producers (the supervisor's finalize and the deterministic derivation with the layer off), writing nothing at all for a task that left none, and LF on every host;
- exhausting a fix loop or the global fix-iteration budget → `manual_action_required` + failure report; an unrecoverable error → `failed`.

## Principles

- Tests are deterministic and isolated (no network calls and no real CLIs in unit/integration).
- External processes and time are mocked/injected.
- **The suite must pass on Windows, Linux, and macOS.** No POSIX-only assumptions in tests: compare paths via `pathlib`/`Path.as_posix()` (never a hardcoded `/`), normalize newlines in byte-compares, guard `signal.SIGKILL` and other POSIX signal constants with `getattr`, and exercise **both** platform branches of any `os.name`-split logic by injecting the platform seam rather than depending on the host OS. See the cross-platform rules in [coding-style.md](coding-style.md).
- Every behavior change is accompanied by a test.
- The goal is high coverage of critical paths (router, fallback, state machine, security/redaction), not a percentage for its own sake.
- A green `pytest` is a mandatory precondition for committing and for transitioning between implementation stages.
- The suite runs in parallel by default (`pytest-xdist`, `addopts = "-n auto"`). Run `pytest -n0` for a serial run when debugging (`--pdb`, `-s` streaming, deterministic ordering).
- Heavy integration files (real git/subprocess/daemon/process-tree) are tagged `pytestmark = pytest.mark.slow`. For a fast inner loop while developing, run `pytest -m "not slow"` (~12 s vs a few minutes); CI still runs the whole suite. Markers are registered in `pyproject.toml` and `--strict-markers` is on, so a typo'd marker is an error.
