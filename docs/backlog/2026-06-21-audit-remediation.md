# Audit Remediation Plan — 2026-06-21

Remediation plan for [2026-06-21-audit.md](2026-06-21-audit.md). Every finding was **re-verified against the code** (not the audit prose) by reading the cited locations and grepping for usages across `src/`. Each entry carries a verdict, the corrected facts where the audit drifted, a concrete minimal fix, a verification step, and an effort size (S = <1h, M = a few hours, L = day+). Blocking decisions are collected in **Phase 0** — answer those before starting the phase that depends on them; everything else has a safe recommended default.

## Status (updated 2026-06-22)

- ✅ **Done:** **Phase 7 #A** (full `Stage`-enum flow-identity removal) + **#L1** (rendered-prompt overwrite) — Q-A resolved → option 1. Implemented, ruff + mypy + full pytest green, docs synced. 3 minor follow-ups recorded in [follow_ups.md](follow_ups.md) (node_interaction_path redundancy; `Stage`'s home in `providers/base.py`; supervisor `node_run_id=0` dir collision).
- ✅ **All Phase 0 decisions made (2026-06-22, operator):** recorded in the decisions table at the top of Phase 0. Q-10 → **wire** the supervisor session via `node_lineage`; Q-12 → **shell-string** check seeding via the resolver; Q-19 → **wire** the skill-dedup feature; Q-4/Q-5/Q-8/Q-13/Q-17/Q-18/Q-26/Q-B → the recommended defaults. **Code not yet changed.**
- ⬜ **Not started (now unblocked):** **Phases 1–6** (all other findings — High #1–3, Medium, Low, docs/tests, §-anchors). Each is scoped above with a concrete fix; none is begun.

## Verification outcome

The audit is high quality: **24 of 32 findings + both transitional items + all 5 by-design items are CONFIRMED as written.** Eight findings needed correction or downgrade — read these before planning effort, because three of them are smaller (or nearly non-issues) than the audit implies.

### Corrections to the audit (read first)

| # | Audit said | Verification found |
| --- | --- | --- |
| 7 | Provider duplication "~600-700 lines each" | **Exaggerated.** That's total file size. Real shared surface = a byte-identical 22-line tail + two footer helpers + parallel (not identical) skeletons. Still worth extracting, but it's an **M**, not a rewrite. |
| 13 | `RedactionFilter` is framed as a secret catch-all but isn't | **Confirmed but mitigated.** The logging contract already constrains call sites to ids/enums/counters. Recommended fix is a docstring tightening (S), not a secret registry. |
| 18 | `PublishResult` **and** `ManualActionRequired` are dead/misplaced | **Partial.** `PublishResult` is genuinely dead (delete it). `ManualActionRequired` is **live** — raised by the orchestrator at 3 sites, caught at 2. Only its _location_ is debatable; do **not** delete it. |
| 21 | `require_gh`/`GhNotAvailableError` "dead within the installer" | **Wrong framing — not dead.** 3 production callers (`cli.py:620,711,952`) + caught at `cli.py:1297`. The accurate part is pure code-placement (runtime gate living in a detection module). Low-value cleanup, **not** a deletion. |
| 24 | `open(stdout_path)` is _outside_ the try, so it crashes | **Backwards.** `open()` is at `process.py:80`, **inside** the try — an unwritable path degrades to `launch_error`, it does not crash. The only real defects are the redundant `OSError` subclasses and a misleading error message. S, cosmetic. |
| 29 | `_install_atomic_write` temp-name mismatch | **Confirmed but harmless.** No collision, no leftover, correct cleanup — the only artifact is a transient `.config-*.yaml`-named temp next to `.md` files. Purely cosmetic; optional. |
| 31 | List of stale docstrings | **Two are not stale:** `core/orchestrator.py:1-8` ("→ summary → publishing") and `ledger.py:9` (§5.2 "summary stage") are accurate — `summary`/`publishing` are still canonical. `cli.py:354-360` and `validation_gate.py:96` are _imprecise_, not removed-concept references. The rest are genuinely stale. |
| 32 | Brittle tests incl. `tests/test_cli_init.py` | **Stale reference + scope.** `tests/test_cli_init.py` does not exist (renamed `tests/test_cli_install.py`, already path-portable). Project is **POSIX-only** (CI is ubuntu-only), so the `SIGKILL`/path-separator findings are non-issues. Only the `shell=False` substring assertion is a genuine latent risk. The `endswith("…/SKILL.md")` pattern recurs in 2 more files the audit missed (`test_recovery.py:550`, `test_orchestrator.py:2102`). |

One **latent bug** surfaced during verification that the audit did not separate out: in the `Stage`-enum shim, `observability.py:113` writes `stages/<identity>/rendered-prompt.md` keyed by stage identity **without** a run-id, so two same-identity nodes (e.g. two agent nodes in a research/audit flow that both default to `IMPLEMENTATION`) overwrite each other's rendered prompt. Tracked as item **L1** in Phase 2.

### Finding → phase map

| # | Finding | Verdict | Phase | Effort | Decision? |
| --- | --- | --- | --- | --- | --- |
| 1 | Resume crashes on corrupt normalized artifact | Confirmed | 1 | M | — |
| 2 | `config.example.yaml` drops `gh pr merge` denial | Confirmed | 1 | S | — |
| 3 | `pr_title` lost across crash-resume | Confirmed | 1 | S | — |
| 6 | Research/audit global budget inert | Confirmed | 2 | S | confirm 12/8 |
| 8 | Evaluator "medium" treated two ways | Confirmed | 2 | S | **Q-8** |
| 9 | `partition_decomposition` `StopIteration` | Confirmed | 2 | M | — |
| 10 | Supervisor session not durable | Confirmed | 2 | M / S | **Q-10** |
| 11 | HITL cleanup misses `timeout`/`invalid_response` | Confirmed | 2 | S | — |
| 14 | PID-recycling window | Confirmed | 2 | M | — |
| L1 | Rendered-prompt overwrite (same-identity nodes) | ✅ done | 2 | S | — |
| 4 | Decorative `decomposition.gate` fields | Confirmed | 3 | S | **Q-4** |
| 5 | Decorative commit flags | Confirmed | 3 | M | **Q-5** |
| 15 | Dead `Stage.PUBLISHING` | Confirmed | 3 | S | — |
| 16 | Vestigial `LedgerRecord.summary_gist` | Confirmed | 3 | S | — |
| 17 | Vestigial `LoopCounters.stage_attempts` | Confirmed | 3 | M | **Q-17** |
| 18 | Dead `PublishResult` (+ `ManualActionRequired` placement) | Partial | 3 | S | **Q-18** |
| 19 | Unwired `compute_skill_dedup` | Confirmed | 3 | M | **Q-19** |
| 20 | Dead CLI flag `--keep-branch` | Confirmed | 3 | S | — |
| 22 | Vestigial `split_command` | Confirmed | 3 | S | — |
| 23 | `_WORC_HOME` duplicated literal | Confirmed (benign) | 3 | S | — |
| 7 | Provider adapter duplication | Partial (smaller) | 4 | M | — |
| 12 | `detect_checks` duplicates B23 resolver | Confirmed | 4 | M | **Q-12** |
| 21 | `require_gh` placement (NOT dead) | Partial | 4 | S | — |
| 27 | Redaction min-length constant duplicated | Confirmed | 4 | S | — |
| 13 | `RedactionFilter` defense-in-depth gap | Partial (mitigated) | 5 | S / M | **Q-13** |
| 24 | Safe-runner error handling | Partial (cosmetic) | 5 | S | — |
| 25 | Fragile git-status first-letter match | Confirmed (latent) | 5 | S | — |
| 26 | Fragile Phase-B completeness substring | Confirmed | 5 | S | **Q-26** |
| 28 | Dangling `--sandbox` accepted | Confirmed (low risk) | 5 | S | — |
| 29 | `_install_atomic_write` temp name | Partial (harmless) | 5 | S | — |
| 30 | `quarantine_folder` example drift | Confirmed | 6 | S | — |
| 31 | Stale docstrings | Partial | 6 | S | — |
| 32 | Test brittleness | Partial | 6 | S | — |
| B | `§NN` spec anchors (481 across 73 files) | Confirmed | 6 | M | **Q-B** |
| A | `Stage` enum shim removal | ✅ done | 7 | L | Q-A→opt1 |

---

## Phase 0 — Blocking decisions (answer before the dependent phase)

Each question lists options with the **recommended** one first. **All decided 2026-06-22 (operator)** — recorded in the table below; the per-question detail follows for context. **Code not yet changed** — these decisions unblock Phases 1–6.

| # | Status | Decision |
| --- | --- | --- |
| Q-A | ✅ done | option 1 — full `Stage`-enum flow-identity removal (implemented, green) |
| Q-4 | ✅ resolved | (a) **remove** the decorative `decomposition.gate` fields |
| Q-5 | ✅ resolved | (a) **remove** `commit_per_subtask` / `commit_each_subtask` / `min_size_signal` |
| Q-8 | ✅ resolved | (a) `medium` is **non-blocking** — align `Finding.blocking` down (no runtime change) |
| Q-10 | ✅ resolved | (a) **wire** the supervisor's own session via `node_lineage` |
| Q-12 | ✅ resolved | (b) installer seeds **shell strings via the resolver** (`shlex.join`) |
| Q-13 | ✅ resolved | (a) **tighten the docstring** (no secret registry) |
| Q-17 | ✅ resolved | (a) **delete** the always-0 `tasks.stage_attempts` field + column |
| Q-18 | ✅ resolved | (a) **leave** `ManualActionRequired` in place; delete only the dead `PublishResult` |
| Q-19 | ✅ resolved | (a) **wire** `compute_skill_dedup` — the §2.2 operator-text-precedence dedup is wanted |
| Q-26 | ✅ resolved | (a) **section-only** Phase-B completeness (drop the `"acceptance"` substring fallback) |
| Q-B | ✅ resolved | (b) **strip bare `spec §`, convert `.md §N`** anchors to functional-map links |

### Q-4 — Decorative `decomposition.gate` fields: remove or wire? (gates Phase 3 / #4) — ✅ RESOLVED → (a) remove

- **(a) Remove (recommended).** Delete `gate_min`/`gate_max`/`linear_depends_on` from schema, parser, and `implementation.yaml`. The hardcoded floor-of-2 + always-linear already match `min:2, linear_depends_on:true`, so removal is a no-op at runtime. Config `agents.decomposition.max_subtasks` stays the single source of truth — which matches the deliberate invariant that a flow cannot weaken the core's gate.
- (b) Wire it — let a flow's `gate.max`/`gate.min` override the config bound. Contradicts the "flow can't weaken the gate" invariant; no operator has asked for split per-flow bounds.
- **Recommend (a)** — greenfield MVP, speculative config.

### Q-5 — Decorative commit flags (`commit_per_subtask`/`commit_each_subtask`/`min_size_signal`): remove or wire? (gates Phase 3 / #5)

- **(a) Remove all three (recommended).** Subtask commit is unconditional today and the per-subtask recovery/resume model keys off per-subtask SHAs, so "skip commit" would break resume idempotency. The example-YAML comments overstate them.
- (b) Wire `commit_per_subtask: false` as a real disable switch. Adds a mode nobody wants and undermines decomposition recovery.
- **Recommend (a).** (Loader must _tolerate-and-strip_ these keys from existing YAML per the config-version convention, not reject them.)

### Q-8 — Should a `medium` evaluator finding drive `rework`? (gates Phase 2 / #8)

- **(a) Medium is NON-blocking (recommended).** Align `Finding.blocking` (`engine.py:73-76`) down to high/critical only, matching the live routing in `_is_blocking`. **No runtime change** — routing already ignores medium; this just makes the carried `Finding.blocking` flag agree with the decision and collapses the duplicate `_HIGH_SEVERITIES` set.
- (b) Medium IS blocking — add `medium` to `_BLOCKING_SEVERITIES` so reviews rework on medium. Changes routing: every medium review finding triggers a fix loop. That's a product decision, not a bug fix.
- **Recommend (a).**

### Q-10 — Supervisor session durability: wire or defer? (gates Phase 2 / #10) — _consequential_

- **(a) Wire via `node_lineage` (recommended).** Persist `_own_session_id` with a sentinel `node_id` (e.g. `"__supervisor__"`), reusing the exact pattern proven in `evaluator.py:208-231`. Project memory records supervisor cross-step durability as a genuine P2 goal; the infra exists; the docstring already promises it. This is the one place where wiring (not removing) is right.
- (b) Defer — fix the overclaiming docstring (`supervisor.py:17,74-75`), accept in-memory session, log a follow-up. (S)
- (c) Drop `resume_own_lineage` for the supervisor entirely (stateless per turn).
- **Recommend (a)** if supervisor quality across resume matters; (b) if you want to close the phase cheaply now. Note: wiring stores a raw provider session id — it must live **only** in `state.db` and be redacted everywhere else.

### Q-12 — Installer check seeding: structured argv or shell strings? (gates Phase 4 / #12) — _consequential_

- (a) Persist structured `{name, argv}` entries — exact parity with the resolver, but `config.yaml` becomes less hand-editable.
- **(b) Emit shell strings derived from the resolver's argv via `shlex.join` (recommended).** Kills the algorithm divergence (installer's first-match `npm test` vs resolver's lockfile-aware `pnpm test`/`uv run pytest`) while keeping `checks.commands` operator-friendly. The argv→string→argv round-trip is lossless for these simple commands.
- **Recommend (b).**

### Q-13 — `RedactionFilter`: tighten docstring or add a secret registry? (gates Phase 5 / #13)

- **(a) Tighten the docstring to the real structural coverage (recommended).** Call sites are already constrained to ids/enums/counters (`logging.py:9-11`), so the token-shape + `NAME=VALUE` net is adequate defense-in-depth. (S)
- (b) Add a task-scoped mutable secret registry feeding the process-global filter. Couples a global filter to per-task lifecycle and introduces a stale-secret leak surface for marginal gain. (M)
- **Recommend (a)** unless a concrete leak-through-logging path is demonstrated.

### Q-17 — `tasks.stage_attempts`: delete or wire to real data? (gates Phase 3 / #17)

- **(a) Delete the field + the `tasks` column (recommended).** The flow engine made `stage_attempts` an inherently per-node quantity (it lives correctly in `node_runs`); collapsing it to one task-level integer has no well-defined meaning, and no surface depends on it being non-zero. Leave `node_runs.stage_attempts` untouched.
- (b) Wire `_sync_counters_from_run_state` to populate it (e.g. last/max node). Invents a meaning for a number nobody reads.
- **Recommend (a).** (Dropping a `tasks` column is fine under the greenfield no-migration DB policy.)

### Q-18 — Relocate `ManualActionRequired`? (gates Phase 3 / #18)

- **(a) Leave it in `git_manager.py` (recommended).** It's live and effectively part of that module's public surface (orchestrator imports it; `nodes/base.py:60` references it as "legacy"). Moving it is cosmetic churn. **Just delete the genuinely-dead `PublishResult` dataclass** — that part needs no decision.
- (b) Move it next to where it's raised (orchestrator / a shared exceptions module). Churns imports for no behavioral gain.
- **Recommend (a).**

### Q-19 — `compute_skill_dedup`: wire or remove? (gates Phase 3 / #19) — _consequential, product call_

This is intended-but-unfinished code (documented §2.2, tested, renderer has a matching branch), not accidental litter. Is heading-level skill/operator-text dedup still a wanted product behavior?

- **(a) Wire it (recommended if §2.2 is still wanted).** In `_engine_apply_skills`, pass the operator's appended planning text + selected skill bodies to `compute_skill_dedup` instead of the empty tuple at `orchestrator.py:1206`. Small, deterministic, already tested — only the call is missing. Changes operator-visible `plan.md` output.
- (b) Remove the feature — delete `compute_skill_dedup`, the `_render_skill_section` `dedup` branch, `SkillDedupEntry`, and their tests (YAGNI).
- **Recommend (a)** if the product still wants it; **(b)** if not. Needs a product owner's call.

### Q-26 — Phase-B completeness: structured section only? (gates Phase 5 / #26)

- **(a) Section-only (recommended).** Require the `## Acceptance criteria` section; drop the `"acceptance" in description.lower()` substring fallback that lets "no acceptance criteria yet" classify as COMPLETE and skip refinement. Tightening routes more tasks through refinement — the safe direction (refinement never rejects).
- (b) Section OR a stricter phrase regex. (c) Leave as-is and document.
- **Recommend (a).**

### Q-B — `§NN` spec anchors: strip or convert? (gates Phase 6 / #B)

481 occurrences across 73 files reference a superseded external spec.

- **(b) Strip bare `spec §N`, convert `<file>.md §N` to `docs/functional/` links (recommended).** Preserves real cross-references while removing dangling pointers to the dead spec. (M, mechanical)
- (a) Strip all. (c) Leave as-is.
- **Recommend (b).** Map the `.md §N` anchors first, then bulk-strip the bare form, to avoid destroying useful cross-refs.

### Q-A — `Stage` enum removal scope? (gates Phase 7 / item A) — _consequential, large_ — ✅ RESOLVED → option 1 (full removal done 2026-06-22)

The enum plays **two independent roles**; only the first is the shim:

1. **Flow-engine request identity** (`wiring.py`, `nodes/*`, `hitl.py`, `observability.py`, `supervisor.py`, `checks/agent.py`, `routing/router.py`, `providers/base.py`) — the removable shim.
2. **Legacy state-machine skip vocabulary** (`config/schema.py:SKIPPABLE_STAGES`, `orchestrator.py` `effective_skip`/`p.skip`, `validation_gate.py` review-skip) — load-bearing domain names (the sanctioned per-task stage-skip exception). **Keep these.**

- **(2-then-1) Fix the latent bug now, schedule the full removal (recommended).** Do item **L1** (run-id-key the rendered-prompt path) in Phase 2 — that's the only real bug. Strip the misleading "lands with P4" comments now. Schedule the full role-1 removal (per-node identity strings + node capability descriptor for schema/audit-dir selection) as a deliberate standalone refactor (L), since it has no behavioral payoff beyond per-node audit dirs.
- (1) Do the full removal now (L).
- (3) Accept the shim as permanent debt and just strip the stale comments.
- **Recommend (2-then-1).**

---

## Phase 1 — High: data-loss & security (no decisions needed)

**#1 — Resume crashes on a corrupt/missing normalized artifact.** Confirmed · `core/orchestrator.py:727,733` (reads before the `try` at `:742`) + `task/parser.py:231,237` (unguarded `json.loads` + `data["id"]`/`data["title"]`) · **M**

- Fix: guard the pre-`try` reads in `_resume_task`. Build a degraded pipeline context from the existing `TaskRow` fields (title/branch/slug/status, `:705-708`) and catch `(json.JSONDecodeError, OSError, KeyError, ValueError)` before `:729`, returning `_go_terminal(..., MANUAL_ACTION_REQUIRED)`. The fiddly part is that `_go_terminal`/`_Pipeline` currently need manifest-derived fields — confirm which fields it dereferences on a terminal-without-run path.
- Verify: new test — write a truncated `task.normalized.json`, register one active task, call `orch.resume()`, assert `manual_action_required` (mirror `test_resume_more_than_one_active_marks_manual`).

**#2 — `config.example.yaml` drops the `gh pr merge` denial.** Confirmed · `src/wastech_orchestrator/templates/config.example.yaml:71-74` (+ root mirror `config.example.yaml`); loader default `config/loader.py:431` · **S**

- `denied_commands` semantics are **replace, not extend** (`_str_tuple`, `loader.py:147-162`), so a copied example silently loses the merge denial.
- Fix: add `- "gh pr merge"` to both example files; add a one-line comment that `denied_commands` replaces (not extends) the default.
- Verify: add a test loading the example template and asserting it matches the loader default for `denied_commands` (guards future drift); extend `tests/install/test_config_writer.py:88-89` to assert the merge denial too.

**#3 — Custom `pr_title` lost across crash-resume.** Confirmed · `task/parser.py:206` (`write_normalized` omits it) → restored `None` → `orchestrator.py:884` falls back to `title` · **S**

- Fix: add `"pr_title": task.pr_title,` to the `write_normalized` dict and `pr_title=data.get("pr_title"),` to the `load_normalized` `NormalizedTask(...)`. `.get(...)` keeps legacy manifests loading as `None` — no migration.
- Verify: `test_pr_title_round_trips` parametrized over `["Custom PR", None]`, mirroring `test_auto_merge_round_trips`.

---

## Phase 2 — Medium correctness bugs

**#6 — Research/audit global budget inert.** Confirmed · `deep_research.yaml:80` (`global_revision_iterations: 12`), `security_audit.yaml:61` (`: 8`); engine reads only `global_fix_iterations` (`run_state.py:37`, `engine.py:399`) · **S** · _confirm 12/8 are intended_

- Fix: rename the key to `global_fix_iterations` in both flows (and any co-design references under `docs/backlog/flows/`). Do **not** make `_global_cap` read arbitrary key names — the reserved key is the single accounting hook every rework edge increments. Effective cap is `min(flow_cap, agents.max_total_fix_iterations)`, so with the example `max_total_fix_iterations: 30` this **starts** clamping at 12/8 (a real, tighter change — confirm intended).
- Verify: drive a flow past 8/12 cumulative rework edges, assert the stop fires at the flow ceiling.

**#8 — Evaluator "medium" treated two ways.** Confirmed · `evaluator.py:42,44,274-277` vs `engine.py:73-76` · **S** · **Q-8**

- Fix (per Q-8a): change `Finding.blocking` to `severity == "high"` (or a shared predicate); delete the duplicate `_HIGH_SEVERITIES`, use `_BLOCKING_SEVERITIES` in `_to_finding`; fix the `:43` comment.
- Verify: medium-only verdict → `accept` and `Finding.blocking is False`; high → `rework`.

**#9 — `partition_decomposition` `StopIteration` on disconnected decomposition.** Confirmed · `engine_driver.py:64,69` (`next(...)` no default); validator (`validator.py:259-268`) checks references resolve but not region connectivity · **M**

- Fix: extend the validator's decomposition block to assert (1) some edge from `proposed_by` lands in `set(sub_flow)` (region entry) and (2) some forward edge (`outcome not in _REWORK_OUTCOMES`) leaves a region node to a non-region node (region exit), else emit a `FlowValidationError`. Belt-and-suspenders: change the two `next(...)` to `next(..., None)` and raise a typed error if `None`.
- Verify: feed a disconnected-decomposition flow; assert `FlowValidationError` (not `StopIteration`); packaged `implementation.yaml` still passes.

**#10 — Supervisor session not durable.** Confirmed · `supervisor.py:76` in-memory; rebuilt fresh at `orchestrator.py:879`; docstring `:17,74-75` overclaims "durable in P2.2" · **M (wire) / S (defer)** · **Q-10**

- Fix (per Q-10a): add `get_node_lineage`/`upsert_node_lineage` to `SupervisorStorePort`, hydrate `_own_session_id` on first use and upsert after each turn (after `:169`) with sentinel `node_id="__supervisor__"`, `subtask_order=None`. Gate by provider match like `evaluator.py:204`. Session id stays in `state.db` only.
- Verify: resume a task; assert the supervisor's second-run request carries the persisted `session_id`.

**#11 — HITL cleanup misses `timeout`/`invalid_response`.** Confirmed · `hitl.py:413,432` match only `("waiting","transport_error")`; `write_answer` (`:375`) persists all of `AskFailure = {timeout, transport_error, invalid_response}` · **S**

- Fix: add `_RESETTABLE_STATUSES = ("waiting","transport_error","timeout","invalid_response")` and use it at both `:413` and `:432`; update the docstrings.
- Verify: write `timeout` and `invalid_response` artifacts; assert `reset_pending_interactions` unlinks them and `consume_pending_interactions` marks them `consumed`; add an end-to-end check that `rerun --continue` re-asks instead of raising `NodeManualRequired` (`nodes/agent.py:138`).

**#14 — PID-recycling window.** Confirmed · `process_control.py:50` writes bare PID; `is_running` (`:79`) probes signal 0 only (docstring `:75-77` admits it) · **M**

- Fix: persist process identity alongside the PID (start-time, optionally Linux `boot_id`). Switch the PID file to small JSON `{pid, start_time}`; `is_running(pid, expected_start)` returns True only on a start-time match. On darwin prefer `psutil.create_time()` if it's already a dep, else a `sys.platform` branch (`/proc/<pid>/stat` field 22 on Linux; `ps -o lstart=` on macOS). Keep every seam injectable. No legacy-format migration (greenfield).
- Verify: inject fake `kill_fn` + start-time reader — same PID + different start-time → stale (False); matching → True.

**#L1 — Rendered-prompt overwrite for same-identity nodes (latent bug from item A).** ✅ DONE (2026-06-22, folded into Phase 7 #A)

- Fixed by keying observability on `node_id` instead of a collapsed stage identity: `write_rendered_prompt` now writes `stages/<node_id>/rendered-prompt.md` (`observability.py`), and the `Stage`-shim that collapsed distinct nodes to one identity (`wiring.build_stage_map`) is deleted entirely. Distinct node ids → distinct dirs, so same-capability nodes in research/audit flows no longer overwrite each other.
- Verified by the full suite (green).

---

## Phase 3 — Dead config & dead code removal

Mostly deletions; net code reduction. Group #4 and #5 together (same `DecompositionConfig`/`implementation.yaml`).

**#4 — Decorative `decomposition.gate` fields.** Confirmed · `schema.py:134-136`, `snapshot.py:425-427` (+ `_GATE_FIELDS` at `:126`), `implementation.yaml:89` · **S** · **Q-4**

- Fix (per Q-4a): delete the three fields + parser handling + the YAML `gate:` block (and any co-design flow reference). No runtime change.
- Verify: grep clean of `gate_min`/`gate_max`/`linear_depends_on`; `pytest`/`ruff`/`mypy`.

**#5 — Decorative commit flags.** Confirmed · flow `commit_each_subtask` (`schema.py:137`, `snapshot.py:428`, `implementation.yaml:91`); config `min_size_signal`/`commit_per_subtask` (`config/schema.py:124-125`, `loader.py:356,361-362`, `install/config_writer.py:96-97`); example comments `config.example.yaml:34-35` · **M** · **Q-5**

- Fix (per Q-5a): delete all three across schema/loader/writer/two example YAMLs/`implementation.yaml`; subtask commit stays unconditional. Loader must tolerate-and-strip these keys, not reject.
- Verify: grep clean; loader/`config_writer` golden tests updated; `pytest`/`ruff`/`mypy`.

**#15 — Dead `Stage.PUBLISHING`.** Confirmed · `providers/base.py:25`, zero usages · **S**

- Fix: delete the member. Verify no JSON/DB payload reconstructs `Stage("publishing")` (none found); grep + `mypy`/`pytest`.

**#16 — Vestigial `LedgerRecord.summary_gist`.** Confirmed · `ledger.py:48` + serialized `:79`, never assigned (5 constructors checked) or read · **S**

- Fix: delete the field + its `to_json` entry. JSONL `.get(...)` reads make it forward/backward-compatible.

**#17 — Vestigial `LoopCounters.stage_attempts` (always 0 on `tasks`).** Confirmed · `loop_control.py:31`; `_sync_counters_from_run_state` (`orchestrator.py:938`) never sets it; real data lives in `node_runs` · **M** · **Q-17**

- Fix (per Q-17a): remove the field from `LoopCounters`, from `save_counters`/`get_counters`/the `LoopCounters(...)` construction (`state_store.py:660,719,731`), and drop the `tasks.stage_attempts` DDL column (`state_store.py:138`). **Leave `node_runs.stage_attempts`** (same name, different table). Update the `loop_control.py` docstring.
- Verify: grep that no `tasks`-table read references it; `mypy`/`pytest`.

**#18 — Dead `PublishResult` (+ `ManualActionRequired` placement).** Partial · `PublishResult` `git_manager.py:121-127` dead; `ManualActionRequired` `:152` is **live** · **S** · **Q-18**

- Fix (per Q-18a): delete `PublishResult` only. Leave `ManualActionRequired` where it is.
- Verify: grep `PublishResult` clean; `mypy`/`pytest`.

**#19 — Unwired `compute_skill_dedup`.** Confirmed · `core/skills.py:200`; `_engine_apply_skills` passes empty tuple at `orchestrator.py:1206` · **M** · **Q-19**

- Fix: per Q-19 outcome — (a) wire the call with operator text + skill bodies, or (b) remove `compute_skill_dedup` + the `_render_skill_section` `dedup` branch + `SkillDedupEntry` + tests.
- Verify: (a) `plan.md` renders the dedup block when headings collide; (b) grep clean, `mypy`/`pytest`.

**#20 — Dead CLI flag `--keep-branch`.** Confirmed · `cli.py:287-289` declared, never read; not mutually exclusive with `--delete-branch` · **S**

- Fix: delete the flag (`keep` is already the documented default of `--delete-branch`).
- Verify: no test passes `--keep-branch`; `pytest`. (Minor public-surface change — acceptable given greenfield.)

**#22 — Vestigial `split_command`.** Confirmed · `check_runner.py:207-209`, only a security test calls it; duplicates `checks/model.py:110` · **S**

- Fix: delete `split_command` + the now-unused `import shlex`. Repoint `tests/security/test_no_shell_interpolation.py` to assert `normalize_check_command("npm test").argv == ("npm","test")` etc. (strengthens the test — pins the real path).
- Verify: `pytest tests/security/test_no_shell_interpolation.py`; `ruff` (catches unused import).

**#23 — `_WORC_HOME` duplicated literal.** Confirmed (benign — values match) · `cli.py:71` vs `core/orchestrator.py:142` · **S** · _lowest priority, safe to defer_

- Fix (optional): move `WORC_HOME = ".worc"` to a genuine leaf module both import (avoid reintroducing the circular import the comment warns about). Verify single definition via grep; `mypy` confirms no new cycle.

---

## Phase 4 — DRY / structural

**#7 — Provider adapter duplication.** Partial (smaller than stated) · `providers/claude.py` / `codex.py` · **M**

- Real shared surface: byte-identical tail `_read_text`/`_redact_result_session`/`_parse_version` (`claude.py:688-709` ≡ `codex.py:606-627`); identical `build_context_footer`/`build_effective_prompt`; near-identical `__init__`/`preflight`/`run()` skeleton/`_scrub_raw_session`/`_write_request`/`_finalize_failure`/`_extra_secrets`/`_secret_env_values`. Genuinely different (keep per-provider): argv builders, signature tables, parsers, `map_permission` (Claude), effort map + last-message-file (Codex).
- Fix: extract a `providers/_adapter_base.py` (NOT core) — move the identical tail + footer helpers verbatim (zero behavior change), then a `BaseCliProvider` mixin with subclass hooks `_build_argv`/`_signatures`/`_parse`/`_executable_label`. The base must never name a CLI flag (preserves the "core/base doesn't know CLI syntax" invariant). Accommodate the two small deltas: the "not found" message string and the `reasoning` key in `_request_representation`.
- Verify: `pytest` provider tests; `mypy`; add a test asserting both providers still emit their distinct argv (guards against the mixin collapsing provider-specific behavior).

**#12 — `detect_checks` duplicates the B23 resolver.** Confirmed · `install/detect.py:135-163` (first-match shell strings) vs `checks/` resolver (lockfile-aware argv); only caller is `install/wizard.py:166` · **M** · **Q-12**

- Fix (per Q-12b): in `wizard._resolve_checks`, seed defaults through `RepositoryInspector(repo_root).collect()` → `CheckCandidateDetector().detect(...)` and render the candidate argv to strings via `shlex.join`; delete `detect_checks`/`_node_checks`. Consider a shared `checks.detect.propose_default_commands(repo_root)` entry point. Note: at run time `CheckResolver` defaults to `mode=configured` and trusts `checks.commands` as-is, so a wrong installer seed sticks — this is a real divergence fix.
- Verify: update `tests/install/test_detect.py:100-116` and `tests/install/test_wizard.py:64` to the new seam; add a pnpm/uv fixture test that the proposed command is lockfile-aware.

**#21 — `require_gh` placement (NOT dead).** Partial · `install/detect.py:111-132`; live callers `cli.py:620,711,952`, caught `:1297` · **S** · _low value, do only within a broader detect.py cleanup_

- Fix (optional): move `require_gh` + `GhNotAvailableError` to a CLI-adjacent preflight module that calls `detect.has_gh()`; update the 4 `cli.py` refs + 2 test imports. **Do not delete** — it's live.
- Verify: `ruff`/`mypy`; existing `test_require_gh_*` pass.

**#27 — Redaction min-length constant duplicated.** Confirmed (asymmetry is intentional) · `redaction.py:42,47`; `>= 8` hardcoded in `claude.py:684` / `codex.py:602` · **S**

- Fix: export `_MIN_DENIED_SECRET_LEN` and import it in both adapters' `_secret_env_values` (replace the literal `8`). Zero behavioral change. Leave the 4-vs-8 asymmetry (documented, justified).
- Verify: `mypy`; redaction tests unchanged.

---

## Phase 5 — Robustness / fragility hardening

**#13 — `RedactionFilter` defense-in-depth gap.** Partial (mitigated) · `observability/logging.py:111,118,125` pass no `extra_secrets`; docstring `:103-105` overstates · **S** · **Q-13**

- Fix (per Q-13a): tighten the docstring to the real structural coverage (token shapes + `NAME=VALUE`, not arbitrary literals); update/extend `test_logging`.

**#24 — Safe-runner error handling.** Partial (cosmetic — audit's "outside the try" is backwards) · `process.py:80` (`open` is **inside** the try), `:99` (`OSError` + its three subclasses, redundant) · **S**

- Fix: drop the redundant subclasses (`except OSError as exc:`). Optionally give the stdout-open failure a distinct message naming the path (today an unwritable path mislabels as "could not launch `<argv[0]>`"). Confirm the runner's contract is "never raise, report via `launch_error`" before changing behavior.
- Verify: tests for an unwritable `stdout_path` and a missing binary — both return a `ProcessResult`, not raise; the unwritable-path message no longer blames argv[0].

**#25 — Fragile git-status first-letter match.** Confirmed (latent) · `core/dangerous_diff.py:88,90` `startswith("D")/("R")`; sole producer `git_manager.py:418-435` emits real codes · **S**

- Fix: match on `code = status[:1]` against known codes (`code == "D"`, `code == "R"`), or exact where appropriate. Behavior-equivalent for today's producer.
- Verify: add cases `status="D"`, `"R100"`, and a non-delete `"MM"` asserting `"MM"` is not treated as a deletion.

**#26 — Fragile Phase-B completeness substring.** Confirmed · `task/validation_gate.py:350-352` `or ("acceptance" in description.lower())` · **S** · **Q-26**

- Fix (per Q-26a): drop the substring fallback — `has_acceptance = extract_section(task.description, "Acceptance criteria") is not None`. Tightens classification toward refinement (safe).
- Verify: body "no acceptance criteria yet" (no header) → `NEEDS_ENRICHMENT`; real `## Acceptance criteria` section → `COMPLETE`. Update any test relying on the substring branch.

**#28 — Dangling `--sandbox` accepted.** Confirmed (low risk — defense-in-depth, can't weaken isolation) · `security/forbidden_args.py:50-53` · **S**

- Fix: flag a `_SANDBOX_FLAGS` token with empty/missing value (last token, or trailing `=`) with a "missing value" reason.
- Verify: `["--sandbox"]`, `["-s"]`, `["--sandbox="]` each yield a reason; `["--sandbox","workspace-write"]` and `--sandbox danger-full-access` unchanged.

**#29 — `_install_atomic_write` temp name.** Partial (harmless) · `cli.py:1105-1115` hardcodes `.config-*.yaml`; `upgrade-docs` routes `.md` through it (`:484`) · **S** · _optional/cosmetic_

- Fix (optional): derive the temp name from the target — `prefix=f".{path.stem}-", suffix=path.suffix`, or a neutral `".worc-tmp-"`. Keep the temp gitignored under `.worc/`.
- Verify: `pytest tests/test_cli_install.py tests/test_cli_upgrade_docs.py`.

---

## Phase 6 — Docs & test hardening

Do the docstring/anchor work in one `/sync-docs`-adjacent pass to satisfy the Stop docs-sync gate. `src/` is prettier-excluded, so `.py` docstring edits don't need prettier; this doc and any `docs/` markdown do.

**#30 — `quarantine_folder` example drift.** Confirmed · example `./tasks/rejected` (`templates/config.example.yaml:83` + root mirror) vs loader default `./.worc/tasks/rejected` (`loader.py:462`) · **S**

- Fix: change both example files to `./.worc/tasks/rejected`.

**#31 — Stale docstrings (corrected list).** Partial · **S**

- Fix the genuinely stale ones: `state_store.py:3-4` (add `evaluations`/`editing_lineage`/`node_lineage`); `core/decomposition.py:9` (drop the non-existent per-task `decompose` tri-state); `task/model.py:1,18` (de-phase P1/P5 "will populate"); `cli.py:3` (`init` → `install`); `notify/telegram.py:90` (returns bare username; caller prepends `@`); optionally reorder `check_runner.py:1-5` to lead with the resolver path.
- **Leave** `core/orchestrator.py:1-8` and `ledger.py:9` (accurate). `cli.py:354-360` and `validation_gate.py:96` are imprecise wording — reword only if touching them anyway.

**#32 — Test brittleness (scoped to the real risk).** Partial · **S**

- Fix the genuine latent risk: `tests/security/test_no_shell_interpolation.py:38-40` asserts the literal substring `"shell=False" in source` — strengthen to assert _behavior_ (monkeypatch `subprocess` in `providers/process.py` and assert it received `shell=False`).
- Skip the SIGKILL/path-separator items — project is POSIX-only (CI ubuntu, no Windows). Optional: swap the three `.endswith("…/SKILL.md")` (`test_skills.py:37`, `test_recovery.py:550`, `test_orchestrator.py:2102`) for `Path(...).parts[-2:]` if OS-agnostic tests are ever wanted.

**#B — `§NN` spec anchors.** Confirmed · 481 occurrences across 73 files (densest: `orchestrator.py` 55, `git_manager.py` 32, `cli.py` 23) · **M** · **Q-B**

- Fix (per Q-Bb): convert `<file>.md §N` anchors to `docs/functional/` links first, then bulk-strip bare `spec §N`. Leave legitimate prose `§` uses. Zero runtime risk.

---

## Phase 7 — Large refactor — ✅ DONE (2026-06-22, Q-A option 1)

**#A — Full `Stage`-enum removal (role 1 only).** ✅ DONE · Confirmed · **L** · Q-A resolved → **option 1 (full removal now)**

- **Done as implemented:** the flow-engine request identity is now the flow node's own `node_id: str` (threaded through `AgentRunRequest`/`AgentRunResult`/`ResolvedRoute`/router/providers/observability/HITL). The typed-output schema is selected by a derived per-node `OutputContract` (`none`/`human_input`/`planning`) in `AgentNodeRunner._contract` — the `planning` contract is the decomposition proposer (`decomposition.proposed_by`), `human_input` is any node with `hitl`; **no new YAML field** (derived from declared structure). `build_stage_map`/`_stage_identity`/`NodeServices.stage_for_node` deleted; `core/hitl.stage_output_schema`→`typed_output_schema`, `parse_typed_stage_output`→`parse_typed_output`; supervisor identity `Stage.SUMMARY`→`"supervisor"`; check-discovery `Stage.PLANNING`→`"check-discovery"`. **Role 2 kept** — `Stage` still backs the skip vocabulary (`SKIPPABLE_STAGES`, `effective_skip`, `Stage.REVIEW in p.skip`, validation gate).
- **#L1 fixed as part of this** (not deferred): observability keys `stages/<node_id>/` and the prompt-audit step file by `node_id`, so same-capability nodes in research/audit flows no longer overwrite each other's `rendered-prompt.md`.
- **Verified:** ruff + mypy (95 files) clean; full pytest suite green. Touched 16 `src/` files + ~20 test files. Flow contract unchanged.

---

## By design — no action (recorded so they are not re-flagged)

All five re-verified as genuinely intentional, none is a mislabeled bug:

- **BD1** — fix-budget `min(flow_budget, config_cap)` clamp is the non-weakenable ceiling with a safe runtime fallback (`engine.py`; validator decision `validator.py:24-32`).
- **BD2** — a failed primary's partial diff is never auto-rolled-back; the absence of any restore method on `SnapshotHook` _is_ the guarantee (`routing/snapshots.py`).
- **BD3** — `SESSION_UNAVAILABLE` consuming two `stage_attempts` is the deliberate bounded same-provider safety net (`routing/router.py:250-296`).
- **BD4** — `dependency_scan` always returns `passed=True`; it is evidence, not a gate — gating is the flow edges' job (`checkers/dependency_scan.py`).
- **BD5** — DB schema refusal for any non-zero older version is intentional greenfield fail-closed (`state_store.py:99-116`).
