# Skills selection rework: operator-pinned + supervisor-proposed

Status: **implemented** (2026-06-27) Date: 2026-06-26 Owner: Vladimir Makarevich

Rework who selects repo skills and where they come from, while keeping the existing **Model A** delivery (a skill is a read-only `SKILL.md` reference path surfaced in the prompt footer — never loaded as a provider Skill tool, never executed). Selection moves off the `planning` node to two layers: operator-pinned skills per flow node (static, deterministic) and an optional once-per-task supervisor proposal (the supervisor proposes a `node → skills` map, the Core decides). The whole-repo skill inventory is discovered automatically (`git ls-files` for `**/SKILL.md` in the clone), so a monorepo with scattered skills is covered without configuration. This design record reflects the resolved decisions from the 2026-06-26 design conversation; a few implementation-level details remain open.

## The problem

Today `planning` is the single, mandatory, LLM-driven, single-root, whole-task selector of skills. The `planning` agent proposes skill names in its structured output, the Core deterministically filters them against a one-level inventory scan of one root (`skills.scan_root`, default `<repo>/.claude/skills`), and that one set is surfaced identically to every downstream node. Three concrete pains follow: (1) skill selection dies when `planning` is disabled from the flow — nobody else selects; (2) one `scan_root` one level deep cannot see a monorepo where skills are scattered (`mobile/`, `backend/`, `web/` each carry their own); (3) there is no way to guarantee "the testing node always gets the testing skill" — placement is at the LLM's discretion on every run, with no operator control and no per-node granularity.

## Constraints

These bound the solution and were the reason the current advisory model exists; the rework must not break them.

- **Provider parity / core knows no CLI syntax** ([architecture.md](../../../../.agents/rules/architecture.md)): both providers must behave identically. A skill is surfaced as a provider-neutral path, not as a Claude-only or Codex-only native Skill tool. (Both Codex and Claude do now support native `SKILL.md` skills on the open Agent Skills spec — Codex discovers `.agents/skills`, Claude `.claude/skills` — but delegating to native auto-selection is explicitly rejected below.)
- **Untrusted repo content** ([security.md](../../../../.agents/rules/security.md) §8): skill bodies are repo-controlled = untrusted. They are surfaced by path only and never executed; task/repo content reaches providers only as paths, never as argv/env.
- **Determinism and auditability**: selection follows "the agent proposes, the Core decides" (cf. decomposition and `resolve_planning_skills`). A skill name can never introduce a path the Core did not independently discover.
- **The task does not patch the graph** ([memory: per-task stage-skip exception]): per-node skill pins live in the operator-owned flow YAML, not in task files.
- **Supervisor is advisory by construction** ([supervisor.py](../../../../src/wastech_orchestrator/core/supervisor.py)): it observes completed steps read-only, never reworks/reopens/routes, and is best-effort. The new upfront proposal must keep the Core as the decider so the supervisor proposes but never routes.
- **Greenfield, no migration** ([memory: greenfield-mvp-no-migration]): the old `skills.scan_root` / `skills.exclude` config is replaced outright, not migrated.

## Alternatives considered

| Option | Why rejected |
| --- | --- |
| Do nothing (keep `planning`-only selection) | Fails all three pains: dies when `planning` is disabled, blind to monorepos, no operator guarantee of placement. |
| Model B — executable skills (`scripts/`) / hooks | Executes untrusted repo content; hooks are a non-starter under headless runs + per-hook hash trust + the §10 ban on trust-bypass flags (`--dangerously-bypass-hook-trust`). Hooks are out of scope entirely; this is a separate, larger project. |
| Model C — delegate to providers' native skill auto-selection | Loses determinism, auditability, and Core-side selection control; breaks provider parity (different discovery paths/sets) and makes per-node/per-flow scoping awkward (native auto-selection is per-session global). |
| Dynamic selector owned by `planning` (status quo, relocated) | `planning` is optional; selection must survive it being disabled. |
| Per-node dynamic selection (Variant 3 — supervisor runs before every node) | Adaptive to the running diff, but +1 LLM call per node, a new pre-node engine seam, and turns the supervisor from observer into a control-path input provider (the inversion removed on 2026-06-19). Over-engineering for the common case. |
| Diff-scoped `skills.sets` (the `checks.command_sets` pattern for skills) | Timing mismatch: the working-tree diff is empty at `implementation` (the first code-writing node), so diff-scoping can only help post-implementation nodes and cannot scope the node that needs skills most. It largely duplicates the relevance the dynamic proposal already derives from the task spec. Dropped as YAGNI; revisit only if the inventory grows large enough that feeding it to the dynamic proposal is too much context. |

## Decision

Stay in Model A and select skills through automatic discovery + two attachment layers, with the Core as the deterministic decider.

**Discovery (automatic, whole-repo).** The inventory is collected by enumerating tracked `**/SKILL.md` via `git ls-files` in the clone. This is ignore-aware and bounded for free (untracked `node_modules`/build/vendor never appear), needs no "where to look" configuration, and finds skills under any convention anywhere in the tree (`.claude/skills`, `.agents/skills`, or any directory). Frontmatter (`name`/`description`) is read bounded and denied-aware as today.

**Identity (global, scope-independent).** A skill is addressed by its frontmatter `name` when that name is globally unique; on a collision (e.g. `backend/.../testing` and `mobile/.../testing`), it is addressed by its repo-relative path. Identity never depends on scope — a name resolves to exactly one skill, full stop. An ambiguous bare name in an operator reference is reported per the strict/warn flag below (strict: error; warn: skip with a warning).

**Static layer — operator pins (primary).** A `skills:` field on a flow node (in the flow YAML) pins skills to that node. Pins are deterministic, always included, and never removed by the dynamic layer. Nodes are **open**: the dynamic layer may add more skills to a node that already has pins. (Per-node pins live in the flow, not the task — the task does not patch the graph.)

**Dynamic layer — supervisor proposal (optional, on by default).** One upfront proposal per task, owned by the supervisor: it sees the flow graph + task spec + the skill inventory and proposes a `node → skills` map. The Core accepts it deterministically (exactly like `resolve_planning_skills` — proposer proposes, Core decides), so the supervisor proposes but never routes (its advisory contract holds). It runs independently of `planning`, surviving `planning` being disabled. Skipped entirely when the inventory is empty (a repo with no skills pays nothing). Cost when skills exist: +1 LLM call per task; the map is fixed for the run (blind to what later steps reveal — the accepted trade for not paying per-node).

**Effective set per node** = `Core_filter( pins(node) ∪ dynamic_accepted(node) )`, de-duplicated against the inventory. `planning` is removed from skill selection entirely (its `skills` structured-output branch and `_engine_apply_skills` retire).

**Strict vs warn (operator pins only).** A dynamic proposal naming a missing skill is not an error — the Core just filters it (as `dropped_unknown` today). Only an operator pin that does not resolve (typo, removed skill, ambiguous bare name, missing path) is subject to the global `skills.strict` flag. Because skills live in the clone (absent at config preflight), the existence check runs at task start, after the clone and inventory scan, as one upfront resolution pass over the active flow's pins — before any node runs. `strict: false` (default): log a warning, skip the unresolved skill, continue (fail-open). `strict: true`: stop the task in `manual_action_required` with a report (a fixable config/repo error, not `failed`).

**Config.** The `skills:` block shrinks to two keys: `dynamic: true` (the supervisor proposal; `true` + skip-when-empty) and `strict: false`. `scan_root` is removed (discovery is automatic) and `exclude` is dropped (operators pin explicitly; a dynamic proposal of a gate-duplicating skill is low-harm and filtered by the role prompt). Schema version bump, replaced outright (greenfield, no migration).

The cost of the rejected alternatives: Model C/native would have given us providers' auto-selection and progressive disclosure for free; Variant 3 would have given per-node adaptivity; diff-scoped sets would have given deterministic post-implementation relevance — we trade all three away to keep determinism, auditability, provider parity, never-executed safety, operator control over placement, and a small config surface.

**supervisor.** The supervisor becomes more than just an advisor, but something more. This is a gradual evolution for future capabilities and new functionality. For example, repository memory management.

## Open questions

These are implementation-level and do not change the shape above.

1. **Huge-monorepo inventory context.** The dynamic proposal is handed the whole inventory (names + descriptions). If an inventory grows to hundreds of skills this becomes large context; the deferred diff-scoped `skills.sets` (or a `roots` limiter) is the pre-filter to revisit then.
2. **Path-pin resolution details.** Whether a path pin references the skill directory or its `SKILL.md`, and the `..`/absolute/symlink validation applied to it (mirror the existing denied-path / traversal rules).
3. **Supervisor proposal mechanics.** The role-prompt wording, the `node → skills` output schema, and the persistence file (a `selected_skills.json` equivalent) so a resume past the proposal restores the accepted map — while keeping the turn read-only and propose-only.

## Implementation notes

- **Selection module:** [core/skills.py](../../../../src/wastech_orchestrator/core/skills.py) — `SkillInventoryScanner` moves from one-root one-level scanning to `git ls-files`-based whole-repo discovery + collision detection + name/path resolution; `resolve_planning_skills` generalizes from "planning proposes" to a proposer-agnostic "propose, Core decides".
- **Config:** [config/schema.py](../../../../src/wastech_orchestrator/config/schema.py) `SkillsConfig` + [config/loader.py](../../../../src/wastech_orchestrator/config/loader.py) `_build_skills` — drop `scan_root`/`exclude`, add `dynamic`/`strict` (schema version bump).
- **Flow schema:** [core/flow/schema.py](../../../../src/wastech_orchestrator/core/flow/schema.py) `AgentNode` gains `skills: tuple[str, ...]`; flow loader + [core/flow/validator.py](../../../../src/wastech_orchestrator/core/flow/validator.py) parse it and validate pin **structure** at preflight (existence is deferred to the task-start resolution pass).
- **Node wiring:** [core/flow/nodes/agent.py](../../../../src/wastech_orchestrator/core/flow/nodes/agent.py) `skill_reference_paths` / `{skills_path}` is filled from the per-node resolved set (pins ∪ accepted dynamic) instead of the single global `inputs.skill_paths`; [core/flow/nodes/base.py](../../../../src/wastech_orchestrator/core/flow/nodes/base.py) `skill_paths` becomes per-node.
- **Dynamic proposal:** [core/supervisor.py](../../../../src/wastech_orchestrator/core/supervisor.py) gains a once-per-task upfront `propose_skill_map` turn (read-only, propose-only — Core decides), persisted for resume; the orchestrator runs the upfront pin-resolution pass (strict/warn) and threads the accepted map into per-node inputs.
- **Retire** the `planning` skill branch: [core/hitl.py](../../../../src/wastech_orchestrator/core/hitl.py) `_validate_skills` + the `skills` structured field, and `_engine_apply_skills` in [core/orchestrator.py](../../../../src/wastech_orchestrator/core/orchestrator.py).
- **Related backlog:** README row "Agent instruction stubs in target repo" — the skill-_reference_ half this reworks; the stub-_authoring_ half stays deferred.
