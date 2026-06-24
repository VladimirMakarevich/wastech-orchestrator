# Flow: `security_audit`

> Reconstructed from code (`src/wastech_orchestrator/packaged/flows/security_audit.yaml` and the node runners). The code is the only source of truth. Significant claims carry a `file:line` reference.

An advisory security-audit flow ([security_audit.yaml](../../../src/wastech_orchestrator/packaged/flows/security_audit.yaml), `task_type: security_audit`). Flow-wide ceilings: `permission_ceiling: workspace-write`, `output_policy: private_control_workspace_report` (the report lives under the gitignored `.worc/security-reports/<task-id>/` and must never enter git), `publishing: none` — the publish node touches git **not at all** and fails closed if the report would be git-trackable ([B30](../blocks/B30-flow-node-runners.md)). `network_policy: advisories` grants the network needed to fetch vulnerability advisories.

## The graph

```mermaid
flowchart LR
    scope --> repository_analysis --> dependency_scan
    dependency_scan -->|pass| threat_analysis --> finding_verification
    finding_verification -->|accept| report --> private_storage
    finding_verification -->|rework · budget 2| threat_analysis
```

| Node | Kind | Profile / session | Notes |
| --- | --- | --- | --- |
| `scope` | agent | read-only · fresh_disposable · network off | HITL question |
| `repository_analysis` | agent | read-only · fresh_disposable · network off |  |
| `dependency_scan` | checks | `dependency_scan` | runs the core-owned argv scanners as **evidence**; always `pass` ([B32](../blocks/B32-flow-checkers.md)) |
| `threat_analysis` | agent | read-only · fresh_disposable · network on |  |
| `finding_verification` | evaluator | read-only · fresh_disposable · network on · **non-blocking**, `max_rework_per_stage: 2` | self-caps then accepts |
| `report` | agent | workspace-write · fresh_disposable · network off | writes `report.md` under the private report dir |
| `private_storage` | publish | `private_control_workspace_report` | registers the report as an artifact; no git |

(Verified against [security_audit.yaml:14-58](../../../src/wastech_orchestrator/packaged/flows/security_audit.yaml#L14).)

## Loops and budgets

One feedback edge: `finding_verification → threat_analysis` (`rework`, inline budget 2); `finding_verification` is non-blocking and self-caps at `max_rework_per_stage: 2` then accepts. The `dependency_scan` checker never gates (it always emits `pass`); whether its findings matter is expressed by the flow's edges, which here proceed unconditionally to `threat_analysis`.

As with `deep_research`, the flow declares `budgets.global_fix_iterations: 8` ([security_audit.yaml:60-61](../../../src/wastech_orchestrator/packaged/flows/security_audit.yaml#L60)) — the reserved key the engine's global cap reads — so cumulative rework stops at `min(8, agents.max_total_fix_iterations)`.

The supervisor layer writes the task summary; the audit deliverable is the private report under `.worc/`. See [flows/index.md](index.md), [B31](../blocks/B31-supervisor.md).
