# Design review — agent-worc-read-isolation plan vs the codebase

Status: **review findings — no fixes applied** Date: 2026-07-21 Reviewer: Claude (deep review requested by owner)

Scope: all 14 documents of this cluster ([README.md](README.md), [happy-path.md](happy-path.md), WRI-001…WRI-012) were cross-checked against the current source tree, the installed provider CLIs (Claude Code 2.1.210, Codex CLI 0.144.4 — the exact versions the plan cites), and the official Claude Code sandbox documentation. Goal per the review request: find every contradiction against the codebase and the overall idea, and confirm that current orchestrator functionality would not silently suffer. This file only records findings; nothing in the plan or code was modified.

Bottom line: the plan's code-level premises are overwhelmingly accurate — 20+ specific claims verified TRUE against the source (see §6). The findings below are the exceptions: **1 critical functional break the plan does not address (F1)**, several factual errors or overstated claims in the ADR text, sequencing contradictions in the milestone/acceptance structure, and a set of regression vectors the plan should own explicitly.

---

## 1. Critical — the plan breaks a shipped feature

### F1. The packaged `security_audit` flow requires the **agent to write into `.worc/`**, and the plan leaves it no writable surface

- The `report` node's role prompt instructs the agent: write to exactly `{repo}/.worc/security-reports/{task_id}/report.md`, and that directory "is the **only** writable directory … Any write outside it fails validation" (`src/wastech_orchestrator/packaged/flows/security_audit/report.md:1-7`). The `private_control_workspace_report` output policy enforces `_PRIVATE_REPORT_DIR = ".worc/security-reports"` (`src/wastech_orchestrator/core/flow/output_policy.py:26`), and `security_audit.yaml` publishes from there.
- The decision record denies providers **both read and write** of the private/control surface (README §3 provider matrices: "Live control home: Deny; Private home: Deny"), defines the exchange as read-only for agents ("The agent may read it but must not mutate it", README §1), and WRI-005 classifies security reports as private runtime state. **No WRI task defines any agent-writable orchestration surface at all**, and WRI-001's exchange allowlist contains only orchestrator-published files.
- Consequence: under strict isolation the packaged `security_audit` flow cannot produce its report — the one flow whose contract is agent-written output into the private home is broken by the cluster with no migration story. Neither the README review-findings table nor WRI-001/002/003/005 mentions `security-reports` as an agent-**write** surface (WRI-001 mentions "security reports" only in the stay-private list, which is a read-side classification).
- Needed decision (one of): (a) convert the report node to the same orchestrator-captured structured-output mechanism every other artifact already uses (`postprocess.apply_output_artifact` / `write_node_output`) so the agent never writes it; (b) define a narrow agent-writable drop-box (which contradicts the read-only-exchange invariant and needs its own redaction/containment contract); or (c) explicitly deprecate the current report-node contract in the same change. The plan must pick one; today it silently breaks (a).

---

## 2. High

### F2. Claude `--permission-mode default` is **not removed** — the installed CLI still accepts it

- README finding row ("Critical", line 32) and WRI-002 claim the installed Claude Code 2.1.210 "no longer lists that mode" and treat `default` as "the removed `default` enum" that a preflight must keep from reaching "a paid/model invocation".
- Verified on the installed CLI: `--permission-mode` **choices** are indeed `acceptEdits, auto, bypassPermissions, manual, dontAsk, plan` — but `claude --permission-mode default --help` exits 0 while `--permission-mode bogus --help` exits 1. `default` is an undocumented-but-accepted legacy alias; choice validation runs and passes it.
- Consequence: the premise "the current adapter is broken against the supported CLI" is false — read-only nodes (`_PROFILE_MAP` in `providers/claude.py:83-86` maps read-only → `default`) still run today. The WRI-002 remap and parser preflight remain sensible hardening, but the finding's Critical severity, the "removed enum" wording, and the AC framing ("no test or shipped doc relies on the removed `default` enum") mischaracterize current behavior and overstate urgency. The fake-command tests encoding `default` are not "stale" against the installed parser — they encode a deprecated alias.

### F3. Milestone-0 tasks carry acceptance criteria that are only satisfiable after Milestone-1 lands

- WRI-010 (M0) AC-1: "No provider request can read or write live `.worc` control files under strict isolation" — provider enforcement is WRI-002/003 (M1), which depend on WRI-011 (M0), which depends on WRI-010. WRI-011 (M0) AC-2: "The earliest supervisor skill proposal **already runs behind the adapter isolation boundary**" — same inversion. WRI-001 (M0) AC: "WRI-009 integration proves a force-added exchange file cannot survive to any orchestrator commit" — WRI-009 depends on WRI-001.
- These are not dependency cycles in the work graph, but the AC checklists as written cannot be checked off in the declared milestone order, so "implementation tasks ready" is not yet true for the gating structure. Re-home the enforcement ACs into WRI-002/003 (or mark them explicitly as cluster-level exit criteria verified at Milestone 1), leaving each M0 task with criteria it can actually close.

### F4. WRI-011's write-deny on "the original task/lifecycle location" is underspecified, and the **task self-injection channel** is unaddressed

- The orchestrator itself must write to the task lifecycle paths during/after a run: `_relocate_task_file` moves `tasks/pending/<id>.md` → `tasks/{done,failed}/` via `src.replace(dest)` (`core/orchestrator.py:3016-3045`), writes `<id>.summary.md` alongside (`:2855-2874`), and the audit commit stages exactly those lifecycle pathspecs with `git add -A -- <stageable>` (`git_manager.py:1034-1056`). The plan's deny ("Deny provider writes to the original task/lifecycle location", WRI-011; "Source task/lifecycle path: Read" in the happy-path matrices) never states its scope: the single task file, its folder, or the whole `tasks/` tree.
- If the deny covers only the current task's file: a workspace-write agent can still **create new task files in `tasks/pending/` or `tasks/preparing/`** that the daemon will later validate and execute as fresh tasks. The validation gate checks form, not provenance (`task/validation_gate.py`), so agent-authored tasks become arbitrary future agent runs — a self-propagation vector squarely inside this cluster's threat model, covered by no WRI. If the deny covers the whole `tasks/` tree: tasks whose legitimate subject is managing task files break, and the plan must say so.
- Related gap: the audit commit publishes `tasks/done/<id>.md` **content** to the repository. WRI-011 freezes the task packet for prompts, and WRI-009's staged-set gate allowlists lifecycle paths — so an agent-rewritten task body still passes both and gets committed. Verifying the lifecycle file against the frozen packet digest at audit-commit time would close this cheaply.

### F5. WRI-003's network mapping contradicts an existing hard validator rule for Codex

- WRI-003's policy table allows workspace-write network: "Enabled only when the resolved node/provider policy requests it". But the current flow validator **forbids** a Codex workspace-write node with network enabled as a hard rule (`core/flow/validator.py:521-527`; the F17b rationale — network weakens the write sandbox).
- The plan neither preserves nor explicitly relaxes this rule. If preserved, the mapping row is dead for Codex and the table is misleading; if relaxed (permission profiles arguably make network orthogonal to filesystem policy), that is a deliberate weakening of an existing guard and needs its own justification, config-migration note, and tests. Must be reconciled either way.

### F6. Claude on native Windows: strict workspace-write loses Bash — real capability regression with no matching error-class contract

- The decision (WRI-002, README matrix) is honest, but its operational impact is unstated: the packaged `implementation`/`fixing` nodes rely on the agent running builds/tests/searches via Bash; Edit/Write-only operation on native Windows is a materially weaker implementation agent. The docs should state the expected operator posture (e.g., route Windows workspace-write to Codex by default) rather than only "provider fallback may handle it".
- Sharper contradiction: WRI-002 says a required-but-unavailable capability may reach provider fallback "only through the existing infrastructure-error contract" — but the existing taxonomy (`providers/base.py:35-100`, `FALLBACK_ELIGIBLE`) has **no class for "host capability unavailable"**: it is not `BINARY_NOT_FOUND`, not `UNSUPPORTED_VERSION`, and deliberately not a security/policy result either. Without defining the class and its routing, this branch is unimplementable as specified — and the repository invariant says fallback is only for provider infrastructure error classes.

### F7. WRI-003's orchestrator-controlled Codex home changes the auth topology — an operational break the plan understates

- Today Codex authenticates through the operator's own `CODEX_HOME` (passed via the env allowlist, `security/env.py:27-34`), matching the repository policy that credentials/auth stay outside the orchestrator (backlog: "Automatic CLI installation/authorization … Current policy keeps credentials and auth outside the orchestrator").
- WRI-003 requires a provider-owned Codex home under private state and forbids silently copying credentials, requiring "a supported login/credential-store flow for that home". That means: every existing install needs a new interactive `codex login` against the controlled home (per repo, and again after WRI-005 relocation if the home moves), headless/daemon setups need a defined re-auth procedure, and existing resume sessions in the old home are orphaned. None of this is in WRI-003's in-scope/AC text or the docs-update list. Spell out the operator flow (install/preflight step, failure mode when unauthenticated) and reconcile with the stated credentials policy.

### F8. Claude sandbox: built-in linked-worktree `.git` write allowance and cross-scope settings merge are load-bearing, unacknowledged details

- Official sandbox behavior: when cwd is a linked worktree, the sandbox **allows writes to the main repository's shared `.git` directory** (except `hooks/` and `config`) so `git commit` works. WRI-002/009 require `denyWrite` on the resolved gitdir/common dir. The plan must prove the deny actually overrides this built-in allowance (deny-vs-allow precedence at equal specificity is not documented for that carve-out) or record the residual; today the text assumes a clean deny.
- Sandbox filesystem arrays **merge across settings scopes** ("paths from every scope are combined, not replaced"), and `allowManagedReadPathsOnly` is managed-settings-only. So a user-scope `allowRead` can widen the adapter policy unless `--setting-sources` provably excludes user scope for sandbox keys. WRI-002 does require capability-testing the flag interaction — good — but the ACs should name the array-merge hazard explicitly, since a "positively safe" inventory that misses a merged user array would pass while the policy is wider than declared. (WRI-006 mentions "settings-array merge hazards" in the edge-case list; WRI-002's own AC list does not.)

---

## 3. Medium

### F9. WRI-003 problem statement overstates current network injection

"The current provider always launches Codex with legacy `--sandbox` and injects `sandbox_workspace_write.network_access`" — the `-c sandbox_workspace_write.network_access=true` config is emitted **only when the node has a network grant**; offline runs emit `-c web_search="disabled"` instead, and `network_access=false` is never emitted (`providers/codex.py:263-273`). `--sandbox` is indeed always present. The migration must preserve the offline `web_search` disable (WRI-003 does say so); the "always injects" wording should be fixed so the replacement doesn't hunt for an emission that does not exist.

### F10. README review-table row 25 ("the Codex adapter consumes neither list") is imprecise

Codex attempts **do** consume `security.denied_read_paths` — for redaction: `_adapter_base._extra_secrets` → `read_denied_secrets(...)` runs on every attempt (`providers/_adapter_base.py:602-611`). The enforcement-projection claim (nothing reaches Codex argv/config) is correct. The working-tree `.agents/rules/security.md` #11 already words this precisely; the README row should match it, since "consumes neither" contradicts the code as written.

### F11. "fresh/restart/continue" triad does not match the real CLI surface

The code has `rerun <id>` (fresh from base; **restarts in place** only for a pre-checkpoint task on an operator-owned branch, per the shipped rerun-dead-end ADR) and `rerun <id> --continue`; `restart` is a **daemon** command (stop + start the watcher, `cli.py:318`), not a rerun mode. WRI-001/007 use "fresh/restart/continue" normatively in their lifecycle contracts. Pin the exchange lifecycle to the actual verbs (`rerun`, `rerun --continue`, restart-in-place special case) so implementation doesn't invent a third rerun mode or misbind the daemon command.

### F12. Terminal sealing + relocation orphan the repo's own debugging/tooling surface

WRI-007 seals every terminal exchange into private audit; WRI-005 then moves the private home out of the repo. Both are deliberate, but every tool that documents or reads `<repo>/.worc/logs/...` must follow in the same change: the repo-local `.claude/skills/analyze-task-run` skill explicitly ingests "everything the run left behind under the target's `.worc/`" (`.claude/skills/analyze-task-run/SKILL.md:3,18,28-30`), plus `worc logs clean`/shell/`status` tooling and the packaged guide. The WRI docs-update lists cover `docs/` and `packaged/` but never the repo-local skills; add them, or the post-mortem workflow silently breaks after WRI-005/007.

### F13. WRI-011 stops at task title/description while adjacent untrusted-inline surfaces remain in supervisor prompts

The skill proposal inlines not just the task text but the **full skill inventory** (name/description/path per candidate — `core/supervisor.py:916`), and observe turns inline node `final_message` text. Skill descriptions come from repo `SKILL.md` frontmatter — agent-influenceable repository content injected inline into a supervisor prompt, i.e. the same class WRI-011 exists to close. The plan should either extend the freeze/paths treatment to the inventory metadata (it already requires "small allowlisted metadata" — bound it explicitly against description-sized injection) and acknowledge the observe-step inline surface, or record why they stay accepted.

### F14. WRI-012 kill-on-close containment changes orchestrator-crash semantics; the preserved-contract list should be explicit

Today a daemon crash leaves the in-flight agent process running to completion (no containment object exists); with kill-on-close Job Objects/cgroups the agent dies with the orchestrator. That is probably desirable (it is exactly the uncontained-writer fix), but it is a behavior change to crash recovery (parked task + half-finished provider work) that WRI-012 never states. The implementation must also preserve, by name: the `(pid,pgid)` children-file external hard-stop contract (`process_control.py:39-41,90-106`), `start_new_session` group-leader assumptions (`providers/process.py:143`), router cancellation-before-fallback (`routing/router.py:294-300`), and the pending-graceful-stop invariant (AC-6 covers only the last).

### F15. WRI-005's migration inventory omits `.worc/workspace/`

`install` creates `WORC_RUNTIME_DIRS = ("logs", "workspace", "tasks/rejected")` (`cli.py:129`). WRI-005 migrates DB/logs/memory/env/reports/HITL/PID/rejected but never classifies `workspace/`. Decide: private runtime (migrate), control (stays), or obsolete (remove) — otherwise it survives as an unclassified in-repo runtime path after the split.

### F16. Codex native-Windows sandbox claims should be re-verified on Windows before being encoded in ACs

The installed CLI (0.144.4) marks `elevated_windows_sandbox` and `experimental_windows_sandbox` feature flags as **removed** (`codex features list`), which may mean graduated-to-default or dropped — not verifiable from this macOS host. README/WRI-003 assert specifics ("elevated mode is preferred; unelevated behavior must be tested") that may describe a stale surface. The capability-probe contract already protects the runtime; the ADR text and ACs should avoid hard-coding elevated/unelevated semantics until verified on an actual Windows host.

### F17. WRI-002 uses approximate Claude settings key names and ignores `sandbox.credentials`

Actual schema nests the filesystem rules as `sandbox.filesystem.denyRead/denyWrite/allowRead/allowWrite` and network as `sandbox.network.*`; `failIfUnavailable`, `allowUnsandboxedCommands`, `excludedCommands`, `enableWeakerNestedSandbox` are top-level `sandbox.*` keys (all confirmed against current docs). WRI-002's flattened names (`sandbox `denyRead``, etc.) are close enough to mislead tests/ACs. Also unmentioned: `sandbox.credentials` (file/env deny + mask) is the purpose-built surface for exactly the env-file/secret-source denies WRI-002 specifies via `denyRead` — worth evaluating, and its mask/`tlsTerminate` semantics must not be accidentally admitted.

### F18. Operator check commands will traverse the exchange (non-git side)

`checks.command_sets` run with `cwd = clone_dir` (`check_runner.py:151`); repo-root commands (`pytest`, `prettier .`, linters) will walk `.worc-io/` unless the tool honors gitignore. Same class as `.worc` today, but the plan adds a second such directory containing plan/diff/findings copies — noisy or slow for naive check commands. WRI-001 covers the git-side classification; add one line of check-authoring guidance (docs + packaged guide) for the filesystem side.

---

## 4. Low

### F19. WRI-005 references a nonexistent module

"Likely implementation areas" lists `src/wastech_orchestrator/runtime/` — no such directory exists in the tree. If it is meant as a new module, say "new"; otherwise point at the real homes (`core/`, `process_control.py`, `providers/artifacts.py`).

### F20. File-attribution nits (do not change substance)

- Output slots live in `core/flow/postprocess.py:44-48` (`OUTPUT_SLOTS`), not in `output_policy.py` (which owns report-dir policies — still a legitimately affected file via F1).
- `NodeInputs` rehydration on resume lives in `orchestrator._restore_engine_inputs` (`core/orchestrator.py:1729-1764`); `core/recovery.py` only computes the `RecoveryPlan`. WRI-001/007 "recovery" references should name the real seam.
- `{memory_path}` is a per-node prompt variable computed in `agent.py:618-644`, not a `NodeInputs` field.
- The "provider footer" wording is accurate at the adapter level (`build_effective_prompt`, `providers/base.py:178-206`) — no change needed there.

### F21. WRI-010's control-home read-deny also blocks `.worc/guide/` for orchestrator-run tasks

An orchestrator-run task whose legitimate subject is authoring flows/config for the target repo (the guide + `worc-flow*` skills workflow) loses read access to the guide; the frozen bundle contains only the task's own flow/roles/tools. Edge case — acknowledge it (operator runs such tasks interactively, or the task snapshots guide excerpts as inputs).

### F22. WRI-002 does not mention composing with the existing F37 native-memory denies

The Claude adapter already emits `Write/Edit/Read` disallows for the Claude config dir unless `allow_native_memory` is set (`providers/claude.py:195-213`). The new adapter-owned settings policy must compose with (not duplicate or contradict) this mechanism and the `allow_native_memory` toggle; WRI-002 never names it.

---

## 5. External-surface verification (CLI/docs) — what checked out

- Claude Code 2.1.210 installed; permission-mode choices exactly as the plan lists (`manual, auto, dontAsk, acceptEdits, plan, bypassPermissions`) — but see F2 for the hidden `default` alias.
- `--safe-mode` (`CLAUDE_CODE_SAFE_MODE=1`), `--setting-sources`, `--settings`, `--strict-mcp-config`, and every flag in WRI-002's reserved list (`--add-dir`, `--file`, `--tools`, `--agents`, `--plugin-dir/--plugin-url`, `--chrome`, `--ide`, `--remote-control`, `--bare`, `--fork-session`, `--session-id`, `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`) exist on the installed CLI.
- Claude Bash sandbox: keys and semantics confirmed against current official docs, including `failIfUnavailable`, `allowUnsandboxedCommands: false` ("Strict sandbox mode"), `excludedCommands`, `sandbox.filesystem.*`, `sandbox.network.*`, macOS/Linux/WSL2-only (native Windows unsupported), Bash-and-children-only scope, and settings-file self-protection. WRI-002's platform matrix is accurate.
- Codex CLI 0.144.4 installed (matches the plan's cited version); `codex sandbox` with `-P/--permission-profile` exists; `codex execpolicy check` exists (hidden subcommand); `--ignore-user-config`, `--add-dir`, `-p/--profile`, `--enable/--disable` exist; `codex features list` works. `hooks` and `multi_agent` are **stable and enabled by default** on this CLI — WRI-003's requirement to disable them is well-founded and load-bearing.

## 6. Code premises verified TRUE (the plan's foundation is sound)

| Plan premise | Verified against |
| --- | --- |
| `worc_home_for` → `<repo>/.worc`; provider cwd = `repo.local_path`; `.worc` reconstructed independently in CLI/core/memory/git/output-policy/validation/install/process-control | `cli.py:1088-1095`, `core/orchestrator.py:180`, `memory/paths.py:57`, `git_manager.py:84`, `output_policy.py:26`, `config/loader.py:593`, etc. (WRI-004 premise) |
| Live checks failure never sets `NodeInputs.checks_path`; only resume rehydrates it (`{checks_path}` empty for first live `fixing`) | `check_runner.py:75,205-207` computes `first_failure_log` (consumed nowhere); `wiring.py:138` (`p.check_log` always None live); `orchestrator.py:1762-1764` resume-only |
| `run_process` kills the subtree only on timeout/interrupt; ordinary exit reaps root only; Windows relies on `taskkill /F /T`; no Job Object | `providers/process.py:143,157-175,245-262` (WRI-012 premise) |
| Codex always passes legacy `--sandbox`, `--ask-for-approval never`; Claude read-only → `default` + `Read,Glob,Grep`, workspace-write → `acceptEdits` + `...Edit,Write,Bash`; Claude projects `denied_read_paths` only into `Read(...)` disallows and passes no settings/MCP flags | `providers/codex.py:216-296`, `providers/claude.py:83-86,180-192,264-340` |
| Artifacts are produced by orchestrator capture of provider stdout/last-message (agent does not write `.worc` artifacts) — exchange read-only is therefore feasible | `postprocess.py:44-121`, `evaluator.py:181-218`, `codex.py --output-last-message`, `claude.py` stream-json. **Exception: security_audit — F1** |
| Supervisor inlines task title+description (8000-char cap) in the proposal and title in finalize; supervisor calls go through the same router/adapters, read-only profile | `core/supervisor.py:748-762,930,945`, `orchestrator.py:2680-2683` (WRI-011 premise) |
| Skill proposal is a provider call before any node runs; prompts receive live absolute repo `SKILL.md` paths | `orchestrator.py:2617-2712`, `skills.py:105-133` |
| Node ids get no path-segment validation (only reserved-name/`subtask` prefix checks on agent/tool nodes) yet become raw path components; task-id regex `^[a-z0-9][a-z0-9._-]{0,63}$` accepts `con`, `nul`, `com1`, trailing dot | `snapshot.py:315-334`, `providers/artifacts.py:72-98`, `task/model.py:20,93-95` (WRI-008 premise) |
| HITL hands the agent the **full durable record** incl. Telegram handle via `human_input_path` | `core/hitl.py:351-381`, `agent.py:118-154` (WRI-001 redacted-packet premise) |
| Role prompts re-read from live `.worc/flows/` on every call; tool nodes resolve live `.worc/tools/` at run time and execute as orchestrator subprocess (allowlisted env, no provider sandbox) | `prompt.py:26-55`, `agent.py:512`, `supervisor.py:1031`, `tool.py:107-122`, `tools_registry.py:51-89` (WRI-010 premise) |
| `git commit` commits the whole index; merge path `git add -A`; staged deletions accepted by design; no hooks isolation (`core.hooksPath` never set); env allowlist only | `git_manager.py:876-1008,308-343` (WRI-009 premise) |
| `strict_isolation` exists (default `true`); `danger-full-access`/`bypassPermissions` operator-gated by it, never absolutely forbidden; `--dangerously*`/`--yolo`/`--ignore-rules` forbidden wholesale | `config/schema.py:330`, `security/forbidden_args.py:25-95`, `core/flow/validator.py:547-563` |
| Explicit `--env-file` may live anywhere; env-file loads into parent env unrestricted, allowlist applies to child env only | `cli.py:193-196,900-918`, `env_file.py:28-36`, `security/env.py:90-102` (README line 50/51 premise) |
| CI runs Ubuntu-only | `.github/workflows/ci.yml:21` (WRI-006 premise) |
| `.worc-io` appears nowhere in `src/`/`tests/` (greenfield surface); working-tree rules (`security.md` #11, `git-workflow.md`) already record the Codex-blacklist and staged-set gaps accurately | grep + working-tree rule files |
| Session ids bound to provider only, never to instruction content; resume re-sends the full re-rendered prompt | `state_store.py:340-361,548-572`, `agent.py:512-543` (WRI-011 digest-binding is net-new, feasible) |
| `web_search` explicitly disabled for offline Codex nodes | `codex.py:268-273` |
| WRI-008's example node-id grammar (`[a-z0-9][a-z0-9_-]{0,63}`) is compatible with the renderer: `_VAR_RE = \{([a-z0-9_-]+)\}` accepts hyphens; dots are indeed incompatible | `core/prompts.py:45` |
| `follow_ups.md` duplicate-home entry and `archive/concurrent-task-worktrees.md` exist and are tracked | `git ls-files` |

## 7. Feature-regression map (does the plan protect current functionality?)

| Current feature | Impact of the cluster | Covered? |
| --- | --- | --- |
| Packaged prompts reading `{plan_path}/{diff_path}/{review_path}/{checks_path}/{memory_path}/{<node>_path}/{subtask_spec_path}/{predecessor_context}` (all under `.worc/logs/` today) | Paths move to the exchange | ✅ WRI-001 routes every writer/resolver; verified the artifact-production mechanism is orchestrator-side |
| `security_audit` report node (agent writes `.worc/security-reports/`) | **Broken** under provider write-deny | ❌ **F1 — unaddressed** |
| HITL (Telegram) | Redacted answer-only packet replaces full record | ✅ WRI-001; premise verified (F-none) |
| Memory packets / store | Packet → exchange; store stays private; relocation moves store | ✅ WRI-001/004/005 |
| Decomposition subtask specs / handoff briefs | Move to exchange | ✅ WRI-001 |
| Skills (`{skills_path}`, proposal) | Frozen snapshots replace live paths; proposal reordered behind freeze | ✅ WRI-011, with F13 residuals |
| Checks/fixing loop | `{checks_path}` gap actually **fixed** (improvement) | ✅ WRI-001; premise verified |
| rerun / finalize dirty-tree gates (`unaccounted_dirty_paths`) | New `.worc-io` dir must be gitignored **and** classified in `RUNTIME_EXCLUDED_DIRS`/`_is_artifact_path`, else rerun/finalize refuse and code commits sweep it | ✅ WRI-001 in-scope names both; implementation must hit `git_manager.py:84,853` |
| Operator check commands traversing the repo | New exchange dir in-tree | ⚠️ F18 — add authoring guidance |
| Provider fallback contract | Policy denials must not fall back — consistent with taxonomy; **capability-unavailable class missing** | ⚠️ F6 |
| Codex auth / resume (`CODEX_HOME`) | Controlled home breaks current auth topology | ⚠️ F7 — needs operator flow |
| Claude on native Windows (workspace-write) | Loses Bash under strict mode | ⚠️ F6 — intentional, impact understated |
| Codex workspace-write network validator rule | Contradicted by WRI-003 mapping | ❌ F5 — must reconcile |
| Stop semantics (`--force` soft / `--force-full` hard), children file, CANCELLED routing | Containment must preserve them | ✅ WRI-012 AC-6 + F14 explicit list recommended |
| Daemon crash behavior | In-flight agent now dies with orchestrator (was: survived) | ⚠️ F14 — decide/document |
| Task lifecycle moves, `<id>.summary.md`, audit commit | Orchestrator keeps write access; deny scope must exclude its own writes | ⚠️ F4 — scope undefined; self-injection open |
| `logs clean`, `status`, shell, analyze-task-run post-mortem | Artifact locations change (WRI-007 sealing, WRI-005 relocation) | ⚠️ F12 — repo-local skill not in doc lists |
| Memory feature docs ("files-first `.worc/memory/`") | Store moves out of tree | ✅ WRI-005 covers code; docs listed |
| Flow/tool customization (`.worc/flows`, `.worc/tools`) | Stays in-repo (control home), frozen per task; operator edits between tasks preserved | ✅ WRI-010 |
| Guide-driven task authoring by an orchestrator-run task | Guide becomes provider-denied | ⚠️ F21 — edge case, acknowledge |

## 8. Suggested next steps (no changes applied)

1. Resolve **F1** first — it is the only finding where the plan, as written, silently breaks a shipped packaged flow; the cheapest fix aligns `security_audit` with the existing structured-output capture path and deletes the only agent-write-into-`.worc` contract in the product.
2. Rewrite the two overstated Critical rows (F2, F9/F10) so the corrections table stays trustworthy as the cluster's factual baseline.
3. Fix the milestone/AC ownership (F3) before starting implementation, otherwise M0 tasks cannot be closed as specified.
4. Add explicit decisions for F4 (deny scope + task self-injection), F5 (network rule), F6 (capability error class), F7 (Codex home auth flow) — each is a one-paragraph decision in the respective WRI, but all four change implementation shape.
5. Fold the Medium/Low editorial items (F11–F22) into the affected WRI files in one editing pass.

---

## 9. Decisions applied (2026-07-21, owner-approved)

All findings were folded back into the cluster documents on the same day. Owner decisions on the five shape-changing findings:

| Finding | Decision | Where applied |
| --- | --- | --- |
| F1 | The `security_audit` report node migrates to the standard orchestrator-captured structured output; no agent-writable surface exists in either root. | README corrections table + §2 note in [happy-path.md](happy-path.md); WRI-001 in-scope, AC, and implementation areas |
| F2 | Downgraded to High and reworded: `default` is an undocumented-but-accepted legacy alias on 2.1.210; the remap/preflight is hardening, not an outage fix. | README corrections table; WRI-002 in-scope and AC |
| F3 | Cross-task acceptance criteria are marked **cluster exit criteria** verified when the dependent task lands. | README implementation-plan note; WRI-001 and WRI-011 ACs |
| F4 | Provider write deny covers the **entire `tasks/` lifecycle tree** (reads allowed); WRI-009 verifies the lifecycle file against the WRI-011 frozen packet digest before the audit commit; task-file-editing tasks are unsupported under strict isolation. | README corrections table + happy-path policy note; WRI-009 in-scope + AC; WRI-011 in-scope; WRI-003 mapping row |
| F5 | The existing validator rule forbidding Codex workspace-write + network **stays in force**; relaxation is explicitly out of scope. | WRI-003 mapping table + out-of-scope; README §3; happy-path note |
| F6 | New deterministic pre-model `CAPABILITY_UNAVAILABLE` infrastructure class; fallback only to a provider with same-or-stricter effective isolation; supported-host policy/canary failures stay non-fallback security results. | README §3; WRI-002 required outcome + implementation areas; WRI-006 AC; happy-path Claude contract |
| F7 | Keep auth in the **operator's `CODEX_HOME`** with layer isolation (`--ignore-user-config`, untrusted project layer, inventoried remaining layers, orchestrator-owned execpolicy input); the controlled provider home is split out as deferred hardening in [archive/codex-controlled-provider-home.md](../archive/codex-controlled-provider-home.md). | WRI-003 required outcome, in-scope, AC, out-of-scope; README §3; happy-path Codex contract; backlog index row |

Editorial findings F9–F22 were applied as wording/scope fixes in README, happy-path, WRI-002 (settings key names, `sandbox.credentials`, worktree `.git` carve-out, cross-scope merge, F37 composition), WRI-003 (network-injection wording, Windows-mode re-verification), WRI-005 (`.worc/workspace/` classification, `analyze-task-run` skill, dead `runtime/` reference), WRI-006 (smoke recording, `unsupported` mapping), WRI-007 (CLI-verb terminology, tooling updates), WRI-010 (guide read-deny note), WRI-011 (inventory metadata bounds), and WRI-012 (preserved contracts, crash semantics). WRI-004 and WRI-008 needed no changes.
