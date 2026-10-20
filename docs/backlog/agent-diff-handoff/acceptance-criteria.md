# Acceptance criteria — Agent diff hand-off

Verifies [requirements.md](requirements.md) as built by [design.md](design.md).

Shared fixture for the scenarios below unless stated otherwise: a temporary git repository on a `new` task branch cut from `main`, with one tracked file modified, one tracked file deleted, one renamed, and one **untracked** new file created by the editing node; the packaged `implementation` flow.

## Scenarios

### AC-1 — the overview is written with the diff (FR-1, FR-5)

- **Given** the shared fixture
- **When** the `implementation` node finishes and the post-edit guard runs
- **Then** `.worc/logs/<task-id>/change-overview.txt` exists beside `current.diff`, and its whole-task section lists exactly the paths `current.diff` contains — the untracked file included, nothing under `.worc/` — each with kind `M` / `D` / `R old → new` / `A` and the same `+`/`−` counts the diff's hunks give; its header totals equal `summarize_diff(current.diff)`.

### AC-2 — the base is a full SHA in every branch mode (FR-2, FR-5)

- **Given** the shared fixture in `new` mode, and the same edits on an `existing` chain branch whose tip moved before the task started
- **When** the overview is written
- **Then** its header names a 40-hex SHA; in `new` mode it equals `git rev-parse main`, in `existing` mode the recorded task start commit; and `git diff <that sha>` over the same pathspec reproduces `current.diff`'s path set.

### AC-3 — every later node receives overview, base and diff (FR-1, FR-2, FR-3)

- **Given** a run that has passed `implementation`
- **When** `review` (evaluator), `fixing` and `documentation` (agents) build their requests
- **Then** each `request.json` has `change_overview_path` and `diff_base` under `context_paths` beside `diff_path`; each rendered prompt's footer lists `- change overview:` before `- diff:`, then `Change base: <sha>`, then one guidance sentence; and the exchange copy of the overview sits under `.worc-io/<task-id>/`.

### AC-4 — before any edit, nothing new appears (FR-3)

- **Given** a fresh task
- **When** `refinement` and `planning` build their requests
- **Then** none of the three new fields is set, and the footer is byte-for-byte what it is today.

### AC-5 — the guidance follows the route's shell capability (FR-4)

- **Given** three routes for `review`: Codex only; Claude `read-only` only under `strict_isolation: true` with no `git_evidence`; Codex primary with a Claude `read-only` fallback
- **When** the request is built
- **Then** the Codex-only route gets the shell sentence (start from the overview, `git diff <base> -- <path>`); the Claude-only and the mixed route get the no-shell sentence (read the full diff, the overview is its map).

### AC-6 — unknown capability means no shell (FR-4)

- **Given** a route whose fallback provider has no config block or no bound shell check
- **When** `every_provider_grants_shell` is asked
- **Then** it returns false and the node gets the no-shell sentence — while `any_provider_grants_shell` on the same inputs still returns true for the detection bracket.

### AC-7 — a decomposed run carries both scopes (FR-7, D-5)

- **Given** a decomposed task whose first subtask was committed by the orchestrator, and the second subtask's `implementation` edited two further files
- **When** the second subtask's overview is written
- **Then** the whole-task section lists every file of both subtasks; a second section `Since the orchestrator's last commit <sha>` (that subtask commit) lists only the two new files; and before the first orchestrator commit the second section is absent.

### AC-8 — packaged prompts use the hand-off (FR-8, D-6)

- **Given** the packaged flows
- **When** their role prompts are rendered for a run that has an overview
- **Then** `implementation/review.md` starts from the overview and, in its subtask block, judges the since-last-commit section; `fixing.md` / `fixing.continue.md` start from the review findings and the check log and refer to the overview only to locate files; `documentation.md` names the overview and per-file diffs; and `blog_article/tone_style.md`, `blog_article_revise/tone_style.md`, `content_chapter/story_critic.md`, `content_translate/en_critic.md` still name `{diff_path}` and say explicitly to read the whole change.

### AC-9 — flow authors can reference it (FR-9)

- **Given** an operator flow whose role prompt contains `{?change_overview_path}Overview: {change_overview_path}, base {diff_base}.{/change_overview_path}`
- **When** it is rendered before and after the first edit
- **Then** before: the block disappears; after: both values are substituted; and `worc validate-flow` raises no verbatim-render warning for either name.

## Edge cases & error states

- **AC-E1 (partial-diff fallback, D-7)** — given an infrastructure failure on the primary that changed files, the fallback attempt's request has `diff_path` set to the partial diff and `change_overview_path`, `diff_base`, `change_guidance` all `None`; its footer is today's footer.
- **AC-E2 (resume, NFR-3)** — given a task stopped after `testing` and resumed, the resumed `review` receives an overview whose content and `diff_base` equal what the uninterrupted run handed it.
- **AC-E3 (git failure)** — given a git error while writing the overview, the node fails the same way a failing `write_current_diff` does today (a Git error, routed by the existing rules, never a fallback), and no request is built with a half-written overview.
- **AC-E4 (bound, D-8)** — given a change touching 250 files, each section lists 200 paths and ends with `… and 50 more paths (+A −R) not listed`, where A and R make the header totals exact.
- **AC-E5 (empty change)** — given an editing node that changed nothing, the overview is written with `0 files, +0 −0` and the node still receives all three fields.
- **AC-E6 (reconsider pass)** — given a dangerous diff whose approval is denied, after the reconsider re-run both `current.diff` and the overview are rewritten from the reconsidered tree.
- **AC-E7 (denied path)** — given a changed path matching `security.denied_read_paths`, it appears in the overview with `[denied]` and counts only, and the shell sentence tells the node not to inspect paths so marked.

## Non-functional checks

- **AC-N1 (NFR-1)** — the overview renderer produces identical bytes for the same diff on Windows and POSIX (Windows-style path fixture; `\n` newlines asserted on the written file); the native-Windows Claude shell answer yields the no-shell sentence without a platform branch in `core/`.
- **AC-N2 (NFR-2)** — a secret value present in a changed path or in a hunk never appears in either copy of the overview (the overview carries no hunk content at all); no adapter's argv changes (`--tools`, sandbox, `extra_args` ceiling identical before and after for every route in AC-5).
- **AC-N3 (NFR-3)** — rendering the overview twice from the same diff text and facts yields identical bytes.
- **AC-N4 (NFR-4)** — the overview of the largest recorded diff (≈1.1 MB, `wastech-mdlint`) is under 20 KB; after the change has run on real tasks, re-running the problem's measurement shows shell-enabled Claude `review` runs that read the full diff **and** run git well below 32/32, with no review that missed a changed file in its findings scope.
- **AC-N5 (NFR-5)** — `request.json` and the prompt audit of every node in AC-3 show the overview path, the base and the sentence it got.
- **AC-N6 (NFR-6)** — `lint-imports` passes; `core/` imports no concrete adapter; the guidance text names only `git` verbs.

## Verification method

| AC | How it is verified | Where |
| --- | --- | --- |
| AC-1, AC-2, AC-7, AC-E4, AC-E5, AC-E7 | Unit tests on a temporary git repository driving the Git Manager | `tests/` Git Manager tests |
| AC-3, AC-4, AC-5, AC-E6 | Node-runner tests with fake services; one fake-CLI `implementation` run through `review` asserting the received prompt | `tests/` flow node + fake-CLI integration |
| AC-6 | Unit tests on `security/shell_reach.py` | `tests/` security tests |
| AC-8 | Render the packaged role prompts with fixture variables; assert the clauses | `tests/` packaged-flow tests |
| AC-9 | Renderer test plus `worc validate-flow` on a fixture flow | `tests/` prompt + flow validation tests |
| AC-E1 | Router test with a fake `SnapshotHook` returning a `PartialChange` | `tests/` routing tests |
| AC-E2 | Orchestrator resume test | `tests/` recovery tests |
| AC-E3 | Git Manager test with a failing git call | `tests/` Git Manager tests |
| AC-N1 | Parametrized Windows/POSIX path fixtures; CI matrix | `pytest`, CI |
| AC-N2, AC-N3 | Unit tests (redaction, determinism, argv snapshot per route) | `tests/` |
| AC-N4 | Size assertion on a large fixture diff; the measurement is a real-run follow-up recorded in the PR or a later post-mortem | `tests/` + real runs |
| AC-N5 | Assertions on `request.json` and the prompt-audit artifact in the fake-CLI run | `tests/` |
| AC-N6 | `lint-imports` gate | CI |
