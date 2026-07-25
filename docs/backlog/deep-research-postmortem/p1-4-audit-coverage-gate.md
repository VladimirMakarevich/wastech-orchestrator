# P1.4 — split the analysis node and add a coverage gate

Priority: **P1** Status: **accepted** Date: 2026-07-25 Source: [postmortem.md](postmortem.md) DR-7

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

- Claude: [`providers/claude.py:105`](../../../src/wastech_orchestrator/providers/claude.py) maps `read-only` to `("dontAsk", ("Read", "Glob", "Grep"))` — `Bash` is absent entirely, so the grant means adding scoped `Bash(git log:*)`-style entries to that baseline, not adding bare `Bash`. Note that the sandbox branch in `resolve_claude_tools` is keyed on `profile == "workspace-write"`, so a `read-only` node that now carries Bash bypasses the sandbox-need logic — that branch has to be reconsidered as part of this change, not left implicit.
- Codex: `read-only` is a sandbox mode ([`providers/codex.py:320-321`](../../../src/wastech_orchestrator/providers/codex.py)) that already permits command execution while blocking writes, so the verb allowlist is what makes the two providers behave the same rather than accidentally-different.
- Cross-platform: no shell interpolation of any user string — the git invocation is an argument list, as everywhere else.
- Per [security.md](../../../.agents/rules/security.md) the capability needs an operator escape hatch: the grant is a property of the profile/flow, so an operator can withhold it without editing code.

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
