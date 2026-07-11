# Completed backlog

This folder contains backlog documents for product work that has already shipped or been superseded. The root [backlog README](../../README.md) now lists only open items.

## Completed items

| Document | Status |
| --- | --- |
| [Branch name: epoch prefix + total length cap](branch-name-epoch-and-slug-limit.md) | accepted (implemented 2026-06-26) |
| [Configurable tasks directory](configurable-tasks-dir.md) | implemented |
| [Task discovery: `worc list` + shell completion](cli-task-list-and-completion.md) | implemented |
| [HITL session resume & planning autonomy](hitl-session-resume-and-autonomy.md) | P0 done, P2 documented |
| [HITL-wait observability & prompt cleanup](hitl-wait-observability-and-prompt-cleanup.md) | done |
| [Interactive operator console](cli-upgrade.md) (+ [post-review remediation](cli-upgrade-remediation.md)) | implemented; R1 done, R2/R3 in follow_ups |
| [Implementation roadmap (14-item build order)](implementation-roadmap.md) | historical / closed (step 14 pending merge) |
| [Log management: `worc logs clean` and `logging.*` config](log-management.md) | implemented |
| [Task queue tags for multiple worc instances](multi-instance-task-queues.md) | implemented |
| [Operator confirmation gates in autonomous mode](operator-confirmation-gates.md) | implemented |
| [Orchestrator-driven PR merge](orchestrator-driven-pr-merge.md) | implemented |
| [Custom `tool` nodes (P5)](p5-custom-tool-nodes.md) | implemented 2026-07-08 |
| [Skills selection rework](skills-selection-rework.md) | implemented |
| [Per-node model/reasoning/provider in task front matter](task-node-model-override.md) | implemented |
| [Task priority field](task-priority.md) | accepted (implemented) |
| [Telegram step-trace (live run progress)](telegram-step-trace.md) | implemented |
| [Transient provider-failure recovery](transient-provider-failure-recovery.md) | implemented |
| [Windows / Cross-Platform Support](windows-cross-platform-support.md) | implemented |
| [Improvements intake (9 usage-driven items)](improvements.md) | all 9 implemented (2026-07-02) |
| [Prompt & supervisor authoring contract](prompt-and-supervisor-authoring-contract.md) | implemented (2026-07-02) |
| [Generic node-output prompt variables](node-output-prompt-variables.md) | implemented (2026-07-02) |
| [Sub-task context handoff (intra-task decompose)](subtask-context-handoff.md) | implemented (2026-07-02) |
| [worc shell reliable control surface](worc-shell-reliable-control-surface.md) | implemented (2026-07-06); POSIX verified, Windows real-smoke pending |
| [Autonomous run — open questions & decisions](autonomous-run-open-questions.md) | implementation log (2026-07-02) |
| [Trust levels / danger-approval policy](trust-levels-danger-approval.md) | implemented (config v25, 2026-07-03) |
| [Branch mode (existing / current branch) + per-task publish cap](branch-mode.md) | implemented (config v26, 2026-07-04) |
| [Run-quality & gating hardening (evaluator fail-closed, complete diff, planning HITL, codex usage)](run-quality-gating-hardening.md) | implemented (F19–F22, 2026-07-04) |
| [Codex-primary correctness (resume-argv + supervisor provider/model)](codex-primary-correctness.md) | implemented (2026-07-07, F38/F39) |
| [Supervisor finalize output-schemas OpenAI-strict (F41)](supervisor-output-schema-codex-strict.md) | implemented (2026-07-07); codex re-run verified Проход 18 |
| [P5 findings remediation plan (A1–D1, F42–F50)](p5-findings-remediation-plan.md) | orchestrator items implemented (2026-07-08); target/owner residue → [p5-remediation-skipped-items.md](p5-remediation-skipped-items.md) |
| [P5 remediation — SKIPPED / deferred items (final disposition)](p5-remediation-skipped-items.md) | closed 2026-07-08 — A1 (target `review.md`) + C2 (model/schema-400 error class) shipped, D1 already satisfied; A3 delta-observe stays deferred and E1/F37 stays an owner live-smoke, both carried in [follow_ups.md](../../follow_ups.md) |
| [Multiple named editing lineages in one flow](multiple-editing-lineages.md) | accepted (implemented 2026-07-08) |
| [Flow validation → dedicated `worc validate-flow`](flow-validation-cli-command.md) | implemented (2026-07-11) — preflight is flow-free; `worc validate-flow [NAME] [--all]` is on-demand, operator-scoped, config-aware |
