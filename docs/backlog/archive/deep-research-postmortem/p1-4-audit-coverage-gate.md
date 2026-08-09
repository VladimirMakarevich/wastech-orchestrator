# P1.4 — split the analysis node and add a coverage gate

Priority: **P1** Status: **implemented** (2026-07-26) Date: 2026-07-25 Source: [postmortem.md](postmortem.md) DR-7

## Implemented

Changes 1 and 2. Change 3 (the read-only git grant) is **not** implemented — see below.

Taken **before** [P1.5](p1-5-research-role-prompts.md) rather than in the README's order: P1.5's item 8 writes the class-sweep instruction into "`repository_analysis`, or its successors", so doing P1.5 first would have landed it in one prompt that this item then splits into three, and it would have had to be re-propagated.

**The split is packaged, not target-first, and the taxonomy is repo-agnostic.** This document's "Scope / risk" asked for target-first with promotion after one clean run, but `.worc/` does not exist in this repository — `packaged/` is the only editable copy and it is what `worc install` delivers — so the generic taxonomy the document wanted for a packaged version landed directly: `analysis_core` (central logic, the rules/invariants the project claims to uphold, configuration and data model, internal wiring) → `analysis_surfaces` (command-line entry points and their argument/exit contracts, APIs and servers, packaging and executable resolution, adapters and integrations, generated or declared schemas, discovery) → `analysis_docs_tests` (the plan of record — roadmap, requirements, decisions, glossary, guide — and what the tests actually exercise). Each remit is stated as mandatory and narrow, with the reason spelled out in the prompt: budget spent outside it is depth the surface that needs it never gets. `analysis_surfaces` is where the two release-blocking misses of `p9-09` lived, and its watch-list names both failure shapes (an entry point that resolves to a silent no-op; boundary input that reaches the filesystem unvalidated).

**A hidden dependency this document does not list: the gate could not read anything.** An evaluator's prompt variables were the core allowlist alone — `{<node_id>_path}` resolved only for `agent`/`tool` nodes ([P2.8](p2-8-node-output-handoff.md) piece 2) — and at the gate's position `report.md` does not exist yet, `checks_path` is unset and there is no shell. A `coverage_gate` there could therefore judge the repository but never the audit of it, which makes the whole item decorative. So P2.8 piece 2 landed here as the enabler: `build_node_output_paths` moved to `core/flow/context_paths.py` and both the agent and the evaluator runner read it, the evaluator renders with the flow-derived allowlist, and the prompt-variable lint follows (an evaluator referencing an upstream node is no longer flagged). It is the change P2.8 already specifies — "mirror `agent.py` into `evaluator.py`" — so nothing is redone there; only piece 1 (publish the produced file rather than the sign-off) and piece 3 (the footer slot) remain. The hardcoded `{repo}/docs/research/{task_id}/report.md` in `verifier.md`/`critic.md` is deliberately **left alone**: `{synthesis_path}` resolves to the node's chat sign-off, not the report, so swapping it in before piece 1 would point both evaluators at a 4 KB summary instead of the deliverable.

**The rework edge re-enters at `analysis_core`, not at the last analysis node.** The document says "a rework edge back to the analysis node", which with three nodes has no single answer, and re-entering at `analysis_docs_tests` would satisfy the acceptance criterion literally while being unable to close a gap in either of the other two remits — the gate's guarantee would be false. Re-entering at the head of the chain lets whichever pass owns the gap close it. The cost of a second full sweep is bounded by the findings rather than by the corpus: each analysis prompt carries a `{?review_path}` re-entry section that hands it the gate's findings and tells it to close the named gaps in its own remit first and not re-derive what the previous round covered, so a pass with nothing named for it is a cheap turn. `budget: 2` on the edge matches `max_rework_per_stage: 2`, so the non-blocking self-cap always fires before the edge budget does.

**The gate measures rather than reads.** `coverage.md` opens the three reports through the new channel, re-derives each remit's file set from `{repo}` itself with `Glob`, and compares. Its two checks are "was it really read" and "does it show a traced property"; scope is what the task declares plus what the reports themselves claim, so a narrowly scoped task is not punished (this document's own risk note). It is told explicitly not to re-litigate the analysis — a severity it would rate differently belongs to `critical_review`, missing coverage is its only subject. To make that checkable, each analysis prompt must close with a `## Coverage` section (enumerated / opened / deliberately skipped with the reason / the traced property per subsystem), and `synthesis.md` now derives the deliverable's coverage claim from those sections: a subsystem enumerated and skipped is reported as unexamined, never as clean.

Following the [P0.1](p0-1-evaluator-gate-severity.md) decision, `coverage.md` does **not** restate `medium`: it states the mechanism (the flow decides which severities gate, file everything at its true severity, a sub-threshold finding is carried to the operator) exactly as `critic.md` and `verifier.md` do, so the prompt cannot go stale when the YAML changes.

Two things fixed in passing because the new prompts would otherwise inherit them:

- **The git-evidence clause is now capability-conditional** rather than an unconditional demand. The three passes say history is prime evidence _where it is reachable with the tools you were actually granted_, that `git log`/`git show` need a shell, and that with no shell the honest move is to say so and drop the claim — not to grep a changelog and present that as history. This is what the run actually did (DR-7 sub-defect 1). It is also the truthful wording across providers: a Codex `read-only` node can already run `git log` (commands are permitted; the sandbox makes the filesystem read-only and disables network), while a Claude `read-only` node has no `Bash` tool at all, so the same flow has genuinely different reach depending on which provider runs it.
- **"Return the typed structured result required by the output schema"** is false for these nodes: with no `hitl` block and no flow-level `decomposition`, an agent node's output contract is `none`, so no schema is set and the report _is_ the final message. The new prompts say that instead, which also matters mechanically — `<node_id>.out.md` is what the gate and the downstream nodes receive. The same line is repo-wide boilerplate on other `contract: none` nodes (`security_audit/repository_analysis.md`, `deep_research/synthesis.md`, `architecture_design.md`); those are left untouched as out of scope.

### Not implemented: change 3, the read-only git grant

The instruction and the tool set now **agree** (the acceptance criterion), but by making the prompt honest rather than by granting the capability. The grant was not attempted because it is a new dimension in the security envelope, not a flag:

- Claude resolves `read-only` to `("dontAsk", ("Read", "Glob", "Grep"))` and passes **one** joined tool string to both `--tools` (the hard existence gate, which takes bare tool names) and `--allowedTools` (which takes patterns). A verb allowlist needs `Bash` in the first and `Bash(git log:*)`-style entries in the second, i.e. `ClaudeToolPlan` must split into two sets — and `needs_sandbox` / the `LINUX_MISSING_DEPS` refusal, both keyed on `profile == "workspace-write"`, must be re-keyed on "this plan keeps `Bash`", or a read-only node with a shell would run it unsandboxed on exactly the hosts where the adapter currently refuses to.
- Codex has no counterpart to express the allowlist: `build_codex_permission_profile` emits `extends` / `filesystem` / `network` only. What makes the two providers agree there is not a verb list but the sandbox — under `read-only` the workspace is `read` and `network.enabled` is false, so every mutating verb already fails. That is a defensible design, but it is a different design from this document's "allowlist of read-only git verbs … and nothing else".
- However the grant is declared — a node field, a profile variant, a config key — the flow schema, the validator, the ceiling check and the preflight all have to learn it, since a capability that is not declared somewhere is not reachable without editing Python.

**Correction to this document's own implementation note.** The third bullet of "Grant read-only git to the analysis nodes" says the capability "needs an operator escape hatch" _per_ [security.md](../../../../.agents/rules/security.md). That attribution is wrong, and it is worth fixing here because it inverts the pressure for whoever picks the grant up. `security.md` is four lines: one `## MANDATORY` paragraph saying security mechanisms must **not** unnecessarily limit functionality or degrade UX, that restrictions come only from significant risks and only as the least restrictive solution that gives the necessary protection, and "do not silently trade functionality away for isolation." It mandates no escape hatch for anything. The only "escape hatch" wording in `.agents/rules/` is [architecture.md](../../../../.agents/rules/architecture.md) lines 12 and 36, where it means the opposite — "flexibility means arbitrary _safe_ flows, not an escape hatch" from the validated envelope. The escape-hatch pattern is real but it is codebase convention (`security.strict_isolation`, `security.disable_read_isolation`, Codex `sandbox: danger-full-access`), not a rule clause.

Read correctly, `security.md` argues **for** the grant rather than against it: an audit node whose prompt treats history as prime evidence, on a host where the same flow under Codex can already run `git log` while under Claude it has no shell at all, is close to what that paragraph warns about. So the reason for deferring here is only the mechanical one — the two provider changes above are a security-envelope redesign, and bundling them with a graph split and four new prompts would put that behind a review of prompt wording. It is an ADR of its own, not a lower priority than it looks, and nothing in it is blocked by this item.

### Target-only remainder

A target repository already carrying `.worc/flows/deep_research.yaml` keeps running the single `repository_analysis` node until that tree is refreshed from the new packaged copy (the same refresh the campaign README already flags for `gate_severity`). Refreshing it must bring the four new role files (`analysis_core.md`, `analysis_surfaces.md`, `analysis_docs_tests.md`, `coverage.md`) and drop `repository_analysis.md`, or the flow fails to load on a missing `role_file`.

Doc impact for the `main` refresh: the node-output channel now spans evaluators (`configuration.md` / the flow-authoring page's prompt-variable table), and `deep_research`'s graph gained three nodes and a gate (`worc_architecture.md`, `cookbook.md`).

## Problem

A single linear `repository_analysis` node was asked to walk eight named subsystems across 149 source files and 161 docs. It self-triaged: it went deep on the rules/primitives core, labelled the rest "no findings", and stopped at turn **34 of 400** with 232 k of context used and 112 of 120 timeout minutes remaining. Nothing in the flow measured that, and the two worst defects in the target repo sat in files it never opened while the report graded their subsystems "walked".

Coverage was not budget-limited; it was choice-limited. That is a flow-shape problem, not a model problem.

## Evidence

| Surface                    | Opened / total     |
| -------------------------- | ------------------ |
| production `.ts` source    | 49 / 86 (57%)      |
| tests                      | 4 / 60 (**6.7%**)  |
| `docs/guide/`              | 2 / 51 (**3.9%**)  |
| all tracked in-scope files | 61 / 331 (**18%**) |

Never opened despite being named verbatim in the task Description: `packages/cli/src/index.ts`, `packages/cli/src/init-command.ts` (836 lines — the whole `init` write path), `packages/cli/schema.json`, 9 of 12 MCP-server files, all 4 `decisions/` docs, all 11 per-phase `index.md` files the role prompt says carry the exit criteria, and 56 of 60 test files. Every one of them appeared in the agent's own `Glob` output.

Two release-blocking defects lived in that unread surface — the CLI `bin` being a silent no-op through the npm symlink, and `SEC-003` reading arbitrary files outside the analyzed root — while the report concluded "no HIGH / release-blocking defect was found".

Downstream did not compensate: `architecture_design` + `synthesis` cost **$3.31 (37% of producer spend) for zero new findings**, with 13/13 and 23/28 of their repository reads being files already read in full upstream.

The marginal economics favour more coverage. `repository_analysis`'s $5.25 bought ~111 k tokens of unique evidence (95% of input was cache re-read), so reading the remaining 37 production source files (~57 k tokens) would have cost roughly **+$3–4** for 100% production-source coverage instead of 57%.

## Constraint

The graph is strictly sequential. `run_state.current_node` is a single value, and two unconditional out-edges from one node raise `EngineInternalError` at runtime (and are not caught by the validator). There is no join, barrier, or `foreach`, and `docs/how-it-works.md` states research/audit flows do not support decomposition. **No fan-out design is expressible** — the fix must be serial.

Checkers are a closed set (`command_profile | citation | dependency_scan`), so a coverage gate must be an `evaluator`, not a `checks` node.

## Change

### 1. Split `repository_analysis` into three sequential agent nodes

`analysis_core` (engine, primitives, rules, config, graph) → `analysis_surfaces` (CLI + init, MCP server, generated schema, discovery) → `analysis_docs_tests` (requirements, decisions, glossary, guide, the test suite), each `fresh_disposable` / `read-only`, each with its own role file under `.worc/flows/deep_research/`, each with a **narrow mandatory remit** rather than a menu.

Same total work split across three fresh context windows; cost roughly flat because each node reads less. The remit is what forces even depth — a node that may only report on the CLI cannot spend its budget re-reading `table.ts`.

### 2. Add a `coverage_gate` evaluator

Placed after the last analysis node, before `external_research`:

```yaml
- id: coverage_gate
  kind: evaluator
  role: verifier
  role_file: deep_research/coverage.md
  session_scope: fresh_disposable
  permission_profile: read-only
  network_access: false
  blocking: false
  gate_severity: medium
  max_rework_per_stage: 2
```

with edges `analysis_docs_tests → coverage_gate`, `coverage_gate → external_research (accept)`, `coverage_gate → analysis_docs_tests (rework, budget 2)`.

`coverage.md` asserts one thing only: **every declared subsystem must show a traced property** — an invariant checked, a determinism or correctness claim verified — not a bare "no findings" label. A subsystem that cannot show one is reported as a `medium` finding, which then gates (given [P0.1](p0-1-evaluator-gate-severity.md)).

### 3. Grant read-only git to the analysis nodes (decided)

The role prompt says _"the git history is always present and authoritative … **Discrepancies here are prime findings**"_, while the node's tools are `Read, Glob, Grep`. The agent tried, substituted a Markdown grep, and never examined a commit.

**Decision: grant the capability, do not delete the clause.** History inspection is core audit evidence. The grant is an allowlist of read-only git verbs — `log`, `show`, `diff`, `blame`, `status`, `rev-list`, `rev-parse`, `ls-files`, `shortlog`, `describe`, `cat-file`, `for-each-ref` — and nothing else.

Everything that mutates a repository or publishes stays forbidden, unchanged: `commit`, `push`, `tag`, `merge`, `rebase`, `reset`, `checkout`/`switch`, `restore`, `clean`, `stash`, `apply`/`am`/`cherry-pick`, `remote`, `config`, `gc`, `filter-branch`, and every `gh` write subcommand. This is the existing hard invariant — only the orchestrator commits / pushes / opens PRs — and this change must not create a second path to it. `--dangerously*` / sandbox-disabling flags remain rejected by the config validator.

Implementation notes (both providers must agree):

- Claude: [`providers/claude.py:105`](../../../../src/wastech_orchestrator/providers/claude.py) maps `read-only` to `("dontAsk", ("Read", "Glob", "Grep"))` — `Bash` is absent entirely, so the grant means adding scoped `Bash(git log:*)`-style entries to that baseline, not adding bare `Bash`. Note that the sandbox branch in `resolve_claude_tools` is keyed on `profile == "workspace-write"`, so a `read-only` node that now carries Bash bypasses the sandbox-need logic — that branch has to be reconsidered as part of this change, not left implicit.
- Codex: `read-only` is a sandbox mode ([`providers/codex.py:320-321`](../../../../src/wastech_orchestrator/providers/codex.py)) that already permits command execution while blocking writes, so the verb allowlist is what makes the two providers behave the same rather than accidentally-different.
- Cross-platform: no shell interpolation of any user string — the git invocation is an argument list, as everywhere else.
- Per [security.md](../../../../.agents/rules/security.md) the capability needs an operator escape hatch: the grant is a property of the profile/flow, so an operator can withhold it without editing code.

## Acceptance

- A run whose analysis leaves a declared subsystem with no traced property reworks at least once before reaching `synthesis`.
- The deliverable's coverage section can be reconciled against the read log: a subsystem labelled "walked" has per-file reads on record.
- The three analysis nodes together open strictly more distinct files than the single node did on `p9-09`, at comparable total cost.
- The git-evidence instruction and the granted tool set agree.

## Test

Integration on a fixture repo with a deliberately unread subsystem: `coverage_gate` returns `rework`; on the second pass with the subsystem covered, it returns `accept`. Flow-validator test that the new node/edge set is well-formed (single out-edge per outcome, every node reaches a terminal).

## Scope / risk

Target-first. Promote to `packaged/flows/deep_research.yaml` only after one clean run — the node split hard-codes a subsystem taxonomy, and a packaged version needs a repo-agnostic one (e.g. "core / adapters / docs+tests" rather than mdlint's package names).

Risk: an over-strict `coverage.md` turns every run into two extra rounds. Bound it with `max_rework_per_stage: 2` and word the rubric around _declared_ subsystems, so a flow that declares less is not punished.

## Depends on

[P0.1](p0-1-evaluator-gate-severity.md) — a coverage gate whose findings cannot gate is decorative. Ship P0.1 first.
