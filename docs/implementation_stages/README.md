# Implementation stages

This directory breaks the build of **wastech-orchestrator** into six sequential phases.
The canonical spec is [orchestrator_final_plan.md](../orchestrator_final_plan.md); these files
expand its §15 roadmap into concrete, ordered work blocks with a Definition of Done (DoD) per
phase. In any discrepancy, the spec wins — these documents never override an invariant.

> **Rule (spec §15):** the phases run **strictly in sequence**. You may not start phase N+1 until
> every DoD item of phase N is documented as complete. Within a phase, the logical blocks are also
> ordered — later blocks build on earlier ones.

## Phases

| # | File | Builds | Spec |
|---|------|--------|------|
| 1 | [01_contracts_and_config.md](01_contracts_and_config.md) | Provider contracts, canonical enums, config schema + validator, task data model, `init`/templates | §4.3, §7.1, §11, §20, §21.4 |
| 2 | [02_provider_layer.md](02_provider_layer.md) | Safe process runner, env allowlist, redaction, artifact writer, error normalization, **CodexProvider** | §4.4, §7.1, §10, §12 |
| 3 | [03_claude_code_adapter.md](03_claude_code_adapter.md) | **ClaudeCodeProvider**, shared fake-CLI integration harness | §4.4, §10, §12 |
| 4 | [04_routing_and_fallback.md](04_routing_and_fallback.md) | Agent Router, allowlist enforcement, infra-only fallback, `stage_attempts`, partial-change snapshots | §4.2, §5, §7.2–§7.4, §8.1 |
| 5 | [05_pipeline_and_recovery.md](05_pipeline_and_recovery.md) | State Store, state machine, validation gate, Task Parser, Check Runner, Git Manager, the Core pipeline, decomposition, loop control, terminal cleanup, auto mode, recovery, ledger, `run`/`watch` | §5, §5.1, §5.2, §6, §8, §8.3, §9, §10, §13, §19, §21 |
| 6 | [06_security_and_observability.md](06_security_and_observability.md) | Security hardening + adversarial tests, audit completeness, `failure_report.json`/`stuck.md`, operations docs | §8.1, §10, §12, §16, §19.5 |

```text
P1 contracts+config
   └─> P2 provider infra + Codex
          └─> P3 Claude adapter
                 └─> P4 router + fallback
                        └─> P5 pipeline + recovery   (the largest phase)
                               └─> P6 security hardening + observability + ops docs
```

## Suggested module layout

Each phase fills in its slice of this `src/wastech_orchestrator/` tree (one component = one
module/subpackage, per [coding-style.md](../rules/coding-style.md)). It is a target, not a
contract — adjust names as the code demands, but keep the layer boundaries from
[architecture.md](../rules/architecture.md) intact.

```text
src/wastech_orchestrator/
  cli.py                 # init (done) ─ run, watch          [P1 done / P5]
  config/                # schema, loader, validation         [P1]
  task/                  # model, parser, validation_gate      [P1 model / P5 parser+gate]
  providers/
    base.py              # AgentProvider contract (done)        [P1]
    process.py           # safe subprocess + env allowlist       [P2]
    redaction.py         # secret redaction                      [P2]
    artifacts.py         # request/stdout/stderr/events/result    [P2]
    errors.py            # exit/stderr -> ErrorClass               [P2]
    codex.py             # CodexProvider                            [P2]
    claude.py            # ClaudeCodeProvider                        [P3]
  routing/router.py      # Agent Router + fallback                    [P4]
  core/
    state_machine.py     # statuses + transitions                      [P5]
    orchestrator.py      # the deterministic Core pipeline              [P5]
    loop_control.py      # stage_attempts / fix_cycles / fix_iterations  [P5]
    decomposition.py     # accept/reject + sequential execution           [P5]
    recovery.py          # restart reconciliation                          [P5]
  git_manager.py         # branch / scoped staging / commit / push / PR     [P5]
  check_runner.py        # run configured checks                             [P5]
  state_store.py         # SQLite (§9)                                        [P5]
  artifact_store.py      # artifact dir layout + checksum registration         [P5]
  ledger.py              # completed.jsonl (append-only)                        [P5]
  summary.py             # summary stage + deterministic fallback (§5.2)         [P5]
  security/              # env, injection scan, denylists                        [P2/P5/P6]
  templates/             # packaged data for init (done)                          [P1]
```

## Conventions for every phase

- **Tests ship with the code.** Each phase lists the unit/integration/e2e tests it must add
  (see [testing.md](../rules/testing.md)). A green `ruff check .`, `mypy src`, `pytest`
  (the `/run-checks` skill) is a precondition for closing the phase.
- **Invariants are non-negotiable.** Core never knows CLI syntax; only the orchestrator
  commits/pushes/PRs; fallback is infra-only; the security policy can't be weakened by a task or
  `extra_args`; no secrets in logs/SQLite/artifacts; CLIs launched as an argv list, never a shell
  string. See [architecture.md](../rules/architecture.md) and [security.md](../rules/security.md).
- **Canonical names only.** Providers `codex`/`claude`; the eight stages and the state-machine
  statuses from the spec; branch prefix `agent/<task-id>-<slug>`. Define enums, don't scatter
  string literals.
- **DoD is a checklist.** A phase is closed only when every box is ticked and documented.

## Current starting point (already in the repo)

- `providers/base.py` — the `AgentProvider` Protocol, `Stage`/`RunStatus`/`ErrorClass` enums,
  `AgentRunRequest`/`AgentRunResult`/`ProviderHealth`/`NormalizedError`, `FALLBACK_ELIGIBLE`.
- `cli.py` — the `init` command (idempotent scaffolding, `--git-mode`, `--force`, `--dry-run`,
  `--quiet`); `run`/`watch` are stubs.
- `templates/` — packaged `config.example.yaml`, `task.md`, `AGENTS.md`, `CLAUDE.md`, and the
  per-stage prompt templates, shipped as package data.

Phase 1 treats these as its baseline: it confirms their DoD and adds the config + task-model layers
around them.
