# B13 — Skill Inventory and Selection

> Reconstructed from code (`core/skills.py`) and tests (`tests/core/test_skills.py`). The code is the only source of truth; this document was rebuilt from the implementation, not from prose or comments. Significant claims carry a `file:line` reference.

**Status:** documented · **Source modules:** `src/wastech_orchestrator/core/skills.py`

## Responsibility

Discover the procedural-knowledge **skills** a target repo ships as `SKILL.md` files — anywhere in the tree (a monorepo may scatter them under `mobile/`, `backend/`, `.claude/skills/`, `.agents/skills/`, or any directory) — and resolve operator-pinned / supervisor-proposed skill **tokens** against that inventory, "the proposer proposes, the Core decides". This module is pure logic: a bounded, read-only, frontmatter-only **inventory scan** over a caller-supplied list of tracked files ([skills.py:121](../../../src/wastech_orchestrator/core/skills.py#L121)), collision-aware **identity/resolution** ([skills.py:64](../../../src/wastech_orchestrator/core/skills.py#L64)), and a deterministic **token resolver** ([skills.py:171](../../../src/wastech_orchestrator/core/skills.py#L171)). It never builds CLI argv, reads env, weakens the sandbox, or executes anything; accepted skills are only ever surfaced downstream **by path** as advisory read-only references ([skills.py:1-21](../../../src/wastech_orchestrator/core/skills.py#L1)).

The wiring (discover the inventory after branch prep, resolve the two layers at task start, persist the per-node map for resume, thread it into each node) lives in the orchestrator and is owned by [B06](B06-orchestrator-pipeline.md); this block is the data + decision functions B06 calls.

## Selection model (skills-selection-rework)

Skills reach a node through **two attachment layers** the Core merges deterministically — there is no `planning`-driven selection:

- **Static — operator pins.** A `skills:` list on a flow `AgentNode` (in the flow YAML) pins skills to that node. Pins are deterministic and always included; the flow loader validates only their **structure** (a list of non-empty bounded strings), since skills live in the clone and are absent at flow-load time ([B29 flow definition and validation](B29-flow-definition-and-validation.md)).
- **Dynamic — supervisor proposal.** Once per task, the constant supervisor layer proposes a `node → skills` map (on by default; `skills.dynamic`); the Core accepts it deterministically. Skipped when the inventory is empty. See [B31 supervisor](B31-supervisor.md) / [B06](B06-orchestrator-pipeline.md).

**Effective set per node** = `Core_filter( pins(node) ∪ dynamic_accepted(node) )`, de-duplicated against the inventory ([orchestrator.py:1559](../../../src/wastech_orchestrator/core/orchestrator.py#L1559)). Both layers route through the same resolver, so a name/path can never introduce a file the scan did not independently discover.

## Public surface

- `SkillRef` ([skills.py:37](../../../src/wastech_orchestrator/core/skills.py#L37)) — frozen `(name, description, path)` for one repo skill; `path` is the **repo-relative POSIX** path to its `SKILL.md` (the collision key, the operator path-pin token, and the persisted identity). The absolute path surfaced to a provider is derived by joining the clone root at the wiring seam (B06).
- `SkillResolveResult` ([skills.py:51](../../../src/wastech_orchestrator/core/skills.py#L51)) — one token's outcome: a `ref` plus a `status` of `resolved` / `ambiguous` / `unknown`.
- `SkillInventory` ([skills.py:59](../../../src/wastech_orchestrator/core/skills.py#L59)) — the scanned `skills` tuple; `.resolve(token)` resolves one token ([skills.py:64](../../../src/wastech_orchestrator/core/skills.py#L64)).
- `SkillSelection` ([skills.py:87](../../../src/wastech_orchestrator/core/skills.py#L87)) — the Core's accepted `refs` plus the `unknown` / `ambiguous` tokens; `.unresolved` is their sorted union (for a pin report).
- `SkillInventoryScanner` ([skills.py:105](../../../src/wastech_orchestrator/core/skills.py#L105)) — constructed with `repo_dir` + a `list_tracked` callable; `.collect()` builds the inventory.
- `resolve_skills(tokens, inventory)` ([skills.py:171](../../../src/wastech_orchestrator/core/skills.py#L171)) — the deterministic resolver shared by both layers.

## Behavior

### Discovery (`collect`)

The scanner is handed a `list_tracked` callable (the orchestrator passes `GitManager.list_tracked_skill_files`, which runs `git ls-files` in the clone — ignore-aware and whole-repo, so untracked `node_modules`/build/vendor trees never appear) and the clone `repo_dir` ([orchestrator.py:360](../../../src/wastech_orchestrator/core/orchestrator.py#L360)). `collect` keeps only `SKILL.md` basenames (a defensive filter), reads each one's YAML **front matter** bounded + denied-aware, and records a `SkillRef` with the repo-relative POSIX `path` ([skills.py:121-155](../../../src/wastech_orchestrator/core/skills.py#L121)). A file with no front matter, non-dict YAML, a YAML error, or a missing/blank `name` is skipped defensively; `description` is optional. The single read primitive `_read_text` is **bounded and denied-aware** ([skills.py:157](../../../src/wastech_orchestrator/core/skills.py#L157)): it matches the `denied_read_paths` globs against the repo-relative and absolute path, refuses non-files and files larger than `_MAX_FILE_BYTES` (262 144), and swallows `OSError` to `None`.

### Identity and resolution (`resolve` / `resolve_skills`)

A skill is addressed by its frontmatter `name` when that name is **globally unique**; on a collision (e.g. `backend/.../testing` and `mobile/.../testing`) it is addressed by its repo-relative `path`. `SkillInventory.resolve(token)` is exact-match and provenance-closed ([skills.py:64-84](../../../src/wastech_orchestrator/core/skills.py#L64)): a token equal to exactly one path resolves to it; else a token equal to exactly one `name` resolves to it; a bare name shared by more than one skill is `ambiguous`; anything else is `unknown`. `resolve_skills(tokens, inventory)` ([skills.py:171](../../../src/wastech_orchestrator/core/skills.py#L171)) walks the tokens, accepts the resolved ones (de-duplicated by path, ordered by `(name, path)`), and categorizes the rest into `unknown` / `ambiguous`. Both layers use it; pins additionally apply strict/warn, dynamic proposals just keep `.refs`.

### Strict vs warn (operator pins only)

Resolution runs once at task start, after the clone + inventory scan, before any node runs ([orchestrator.py:1559](../../../src/wastech_orchestrator/core/orchestrator.py#L1559)). Each agent node's pins are resolved; if any are `unresolved` (typo, removed skill, ambiguous bare name, missing path) the global `skills.strict` flag decides ([orchestrator.py:1591](../../../src/wastech_orchestrator/core/orchestrator.py#L1591)): `false` (default, fail-open) logs a warning per node and skips the unresolved pins; `true` stops the task in `manual_action_required` with a report. A **dynamic** proposal naming a missing/ambiguous skill — or a node id not in the flow — is always silently filtered, never an error.

### How acceptance reaches downstream nodes (read-only, provider-neutral)

The accepted per-node `refs` are surfaced purely as **paths**. B06 builds a `node id → absolute POSIX paths` map (repo-relative identity joined to the clone) and threads it onto `NodeInputs.skill_paths_by_node`; the agent node runner reads its own entry via `NodeInputs.skills_for(node.id)` and turns it into the prompt variable `skills_path` and `AgentRunRequest.skill_reference_paths` ([B30 flow node runners](B30-flow-node-runners.md)). The providers' shared context footer lists each verbatim as "read-only reference; advisory, do not execute" and is omitted entirely when a node has no skills. Nothing routes through the Claude-only Skill tool, so both providers behave identically. The map is persisted to `skill_map.json` and restored on resume without re-proposing (B06 / [B10 recovery](B10-recovery-and-resume.md)).

## Invariants & guarantees

- Read-only and bounded: only `_read_text` touches disk; it caps file size at 262 KB and honours `denied_read_paths` on every read ([skills.py:157-169](../../../src/wastech_orchestrator/core/skills.py#L157)).
- No execution, no argv, no env, no secrets: skills are surfaced only as advisory read-only paths; skill bodies are treated as untrusted repo-controlled content ([skills.py:1-21](../../../src/wastech_orchestrator/core/skills.py#L1)).
- Provenance-closed: an accepted token always resolves to a discovered `SkillRef`, so neither an operator pin nor the supervisor can smuggle in an undiscovered path ([skills.py:64](../../../src/wastech_orchestrator/core/skills.py#L64)).
- Deterministic and order-stable: accepted refs are de-duplicated by path and sorted by `(name, path)`; the dropped lists are sorted ([skills.py:189-200](../../../src/wastech_orchestrator/core/skills.py#L189)).
- All dataclasses are frozen ([skills.py:37](../../../src/wastech_orchestrator/core/skills.py#L37), [skills.py:59](../../../src/wastech_orchestrator/core/skills.py#L59), [skills.py:87](../../../src/wastech_orchestrator/core/skills.py#L87)).

## Dependencies

- **Uses:** B25 (security policy — the `denied_read_paths` globs applied on every read), B05 (configuration — `skills.dynamic` / `skills.strict`), B22 (git manager — `list_tracked_skill_files` whole-repo discovery).
- **Used by:** B06 (orchestrator pipeline — discovers the inventory, resolves the two layers at task start, persists `skill_map.json`, threads the per-node map), B31 (supervisor — proposes the `node → skills` map the Core resolves), B30 (flow node runners — read `NodeInputs.skills_for(node.id)` and render `skills_path` / `skill_reference_paths`), B29 (flow definition and validation — parses + structurally validates per-node `skills:` pins), B15 (prompt templates — allowlists the `skills_path` variable).

## Tests

- `tests/core/test_skills.py` — whole-repo discovery via a fake `list_tracked` (frontmatter-only, malformed/frontmatterless skips, non-`SKILL.md` basename filter, denied-path reads, empty inventory); collision identity (ambiguous bare name vs resolve-by-path); `resolve_skills` (resolved/unknown/ambiguous, de-dup-by-path-and-sort, empty proposal, the `unresolved` union).
- `tests/core/test_orchestrator.py` — operator pin reaches the node; the supervisor proposal reaches the node and is filtered for unknown node/skill; `skill_map.json` persistence; `skills.strict` unresolved pin → `manual_action_required` before any node runs.
- `tests/core/test_supervisor.py` — `propose_skill_map` parses assignments + records one advisory row, skips when the inventory is empty, and is best-effort on failure.
- `tests/core/test_recovery.py` — a resume restores `skill_map.json` without re-proposing.
