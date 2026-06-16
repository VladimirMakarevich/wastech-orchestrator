# Implementation flow

This is a more detailed layer on top of [B06 Pipeline](../../blocks/B06-orchestrator-pipeline.md): a frame-by-frame walkthrough of how a single task moves through the pipeline — one document per stage (S01–S08). B**-blocks answer "what exists in the system"; this layer answers "what happens at each execution step": who runs the stage, whether it is optional, how stages are connected, and how the **ping-pong\*\* mechanism works.

Stage documents describe the **flow** and reference the B\*\* blocks that implement the mechanics (without duplication). The same rules apply as for B\*\* blocks (see [CONVENTIONS.md](../../CONVENTIONS.md)): only what is confirmed by code, references in `file:line` format, English language.

## Pipeline stages (overview)

```mermaid
flowchart TB
    start(["task passed gate §19, branch ready"]) --> s1["S01 refinement<br/>(opt.: refined / complete)"]
    s1 --> s2["S02 planning<br/>(opt.; + decomposition, skills)"]
    s2 --> unit{{"for each unit of work (subtask)"}}
    unit --> s3["S03 implementation<br/>(+ dangerous-diff guardrail)"]
    s3 --> s4{"S04 testing (opt.)"}
    s4 -->|checks passed| s5{"S05 review (opt.)"}
    s4 -->|quality failure| s6["S06 fixing<br/>(opt.; limits B09)"]
    s4 -.->|launch failure| reres["re-resolve checks (B23)"]
    s5 -->|no blockers| nextunit{"more subtasks?"}
    s5 -->|blocking findings| s6
    s6 -->|back to testing/review| s4
    nextunit -->|yes| s3
    nextunit -->|no| s7["S07 summary (opt.)"]
    s7 --> s8["S08 publishing<br/>(commit / push / PR)"]
    s6 -.->|limit exhausted| manual["manual_action_required + report (B08)"]
```

## Who runs each stage and what is optional

| Stage | Who runs it | Optional? | Document |
| --- | --- | --- | --- |
| refinement | agent (B17→B18) | yes — skipped when `refined: true` or `COMPLETE` (not via `skip_stages`) | [S01](./S01-refinement.md) |
| planning | agent | yes — `SKIPPABLE` (stub plan is used, decomposition is disabled) | [S02](./S02-planning.md) |
| implementation | agent | **no** — core of the work, cannot be skipped | [S03](./S03-implementation.md) |
| testing | Check Runner (B24), **not an agent** | yes — `SKIPPABLE` | [S04](./S04-testing.md) |
| review | agent | yes — `SKIPPABLE` (requires `agents.allow_review_skip`) | [S05](./S05-review.md) |
| fixing | agent | yes — entered only on failure; `SKIPPABLE` | [S06](./S06-fixing.md) |
| summary | agent (or stub / minimal) | yes — `SKIPPABLE`; best-effort | [S07](./S07-summary.md) |
| publishing | Git Manager (B22), **not an agent** | **no** — exit stage, cannot be skipped | [S08](./S08-publishing.md) |

Classification confirmed by `ROUTABLE_STAGES`/`SKIPPABLE_STAGES` ([schema.py:39-63](../../../../src/wastech_orchestrator/config/schema.py#L39)).

## Ping-pong (testing/review → fixing)

On a **quality** check failure ([S04](./S04-testing.md)) or **blocking** review findings ([S05](./S05-review.md)), the unit enters [S06 fixing](./S06-fixing.md): the agent edits the code and returns to testing (or directly to review if testing is skipped — `_after_edit_target`). Passing resets the counters (B09: `on_check_pass` resets the test cycle, `on_review_pass` resets both). Two limits (`max_fix_cycles` per-loop and `max_total_fix_iterations` global) prevent infinite loops; on exhaustion — `manual_action_required` + failure report ([B08](../../blocks/B08-ledger-and-failure-reports.md)). A check launch failure is **not** a ping-pong event: it is an infrastructure issue → single re-resolve attempt ([S04](./S04-testing.md)/[B23](../../blocks/B23-check-discovery.md)).

With decomposition, each subtask is a separate unit `implementation → testing → review → fixing` with its own local commit ([B11](../../blocks/B11-task-decomposition.md)); the global `fix_iterations` counter accumulates across all subtasks so that decomposition cannot bypass the hard stop ([B09](../../blocks/B09-fix-loop-control.md)).

## Flow documents

- [S01 — refinement stage](./S01-refinement.md)
- [S02 — planning stage](./S02-planning.md)
- [S03 — implementation stage](./S03-implementation.md)
- [S04 — testing stage](./S04-testing.md)
- [S05 — review stage](./S05-review.md)
- [S06 — fixing stage](./S06-fixing.md)
- [S07 — summary stage](./S07-summary.md)
- [S08 — publishing stage](./S08-publishing.md)

## Connections

- Block-level detail and status transitions — [B06 Pipeline](../../blocks/B06-orchestrator-pipeline.md); state machine — [B07](../../blocks/B07-state-machine-and-store.md).
- Cross-cutting scenarios (multiple flows) — [system-flows.md](../../system-flows.md); block map — [index.md](../../index.md).
- C4: dynamic view `implementationFlow` in [docs/likec4/](../../../likec4/README.md).

## Code confirmation

- [orchestrator.py:1033-1047](../../../../src/wastech_orchestrator/core/orchestrator.py#L1033) — `_run_units_and_finish`: loop over units → summary → publish.
- [orchestrator.py:1196-1296](../../../../src/wastech_orchestrator/core/orchestrator.py#L1196) — `_run_unit`: `implementing → testing → reviewing → fixing` loop (ping-pong) and transition to summary.
- [schema.py:39-63](../../../../src/wastech_orchestrator/config/schema.py#L39) — `ROUTABLE_STAGES` / `SKIPPABLE_STAGES`.
- Tests: [tests/core/test_orchestrator.py](../../../../tests/core/test_orchestrator.py), [tests/core/test_cli_pipeline.py](../../../../tests/core/test_cli_pipeline.py).
