# WRI-010 — Isolate and freeze the in-repo control plane

**Status:** implemented **Milestone:** 0 (security prerequisite) **Source:** [decision record](README.md) **Dependencies:** WRI-004, WRI-012

Shipped as `core/flow/control_bundle.py` (freeze / load+verify / live-digest, reusing the WRI-001 no-follow inspector, the containment belt, and `sha256_file` — no new identity code), a freeze/bind step in `Orchestrator._engine_run` (fresh/restart freezes and persists `tasks.control_bundle_digest`; continue loads+verifies and reuses the frozen bytes), a per-node live-mutation verify in `_engine_post_node` routed to `manual_action_required` (Core-detected, never fallback/park), and the `InternalDenyPolicy.frozen_control_bundle` deny target. See [follow_ups.md](../follow_ups.md) for the shipped entry and deferrals (provider-deny projection is a WRI-002/003 cluster-exit; tool dependency-graph freezing and the ephemeral merge flow are out of scope).

## Problem

WRI-005 intentionally keeps the operator control plane under `<repo>/.worc`: `config.yaml`, flow YAML, role/supervisor prompts, and executable tools. That directory remains under the provider working directory. A workspace-write provider can therefore change control inputs even after private runtime state moves elsewhere.

This is an execution boundary, not merely configuration drift. Agent/evaluator and supervisor runners read role files from the live flow directory when each later call is built, while tool nodes resolve and launch operator executables from live `.worc/tools/`. A provider can rewrite a later prompt or tool, and the orchestrator can then read/execute it outside the provider sandbox with the orchestrator's child-process authority. It can also poison `config.yaml` or another flow for the next task. Gitignore does not prevent any of these writes.

## Required outcome

Under strict isolation, providers cannot read or mutate `control_home`. At task start the orchestrator freezes the exact effective control inputs needed by that task into a private, immutable task bundle; all later flow prompt and tool consumers use that bundle rather than reopening live `.worc`. The live control tree is fingerprinted across every provider attempt so a bypass becomes a security violation before another orchestrator-controlled consumer or task can run.

The operator may deliberately modify the live control plane between tasks or between runs of the same task. A **fresh** task adopts such changes after normal validation. **Amended (VF-3, 2026-07-24 — see [runtime-validation-findings.md](runtime-validation-findings.md)):** a deliberate operator **`rerun --continue`** now also adopts them — it re-freezes from the live control plane and records a new digest, so a between-run flow/role/tool fix takes effect from the resume point onward. Only **automatic crash-recovery** (`resume()` without an operator `--continue`) and any in-run **agent** mutation keep the original frozen digest and route drift to `manual_action_required`, because a crash can follow an agent's control-file mutation.

## In scope

- Add `control_home` and the task's frozen control bundle to the provider-internal denied-root policy. WRI-002/003 project the policy into provider-specific enforcement; the Core remains provider-neutral. **Shim handed over from WRI-004:** the typed `InternalDenyPolicy` (`runtime_layout.py`, assembled in `composition.build_internal_deny_policy`) already carries `control_home`, `private_home`, the resolved env-file, and provider auth/config homes; it has **no** frozen-bundle field yet. WRI-010 adds the frozen control bundle to that policy (extend `InternalDenyPolicy` + the composition assembly) rather than inventing a parallel deny set.
- Snapshot the selected flow definition, every referenced agent/evaluator/supervisor role file, resolved tool bundle/executable identity, and the effective configuration/version metadata needed to reproduce the run. Store it under `private_home`, never the exchange.
- Bind flow runners, supervisor prompt rendering, and tool-node resolution to the frozen bundle. Do not retain a live `flow_dir`/`tools_dir` fallback after the first untrusted provider attempt.
- Hash the relevant live control inputs into parent-held state before each provider attempt and verify them only after WRI-012 proves the provider containment empty. A change is a non-fallback security violation; do not execute a changed tool/prompt, silently refresh the snapshot, or destroy operator-owned evidence.
- Before launching a next task, verify that any previous violation is resolved and load/validate a new snapshot. An operator edit made while a task is parked is adopted by a deliberate `rerun --continue` (re-freeze) — **amended by VF-3**; only automatic crash-recovery treats it as a conflict requiring fresh/restart.
- Use regular-file, containment, link/reparse, case-fold, and executable-identity checks. A snapshot must not follow an external symlink/junction or preserve a hard-link back to mutable live control data.
- Keep install/upgrade/operator editing on live `control_home`; the frozen bundle is an execution/audit object, not a new authoring location.
- Redact audit metadata and never include config/env contents in a provider request or exchange packet.

## Acceptance criteria

- [~] No provider request can read or write live `.worc` control files under strict isolation; private/runtime and control roots remain denied after WRI-005 separates them. **(Cluster-exit: WRI-010 names `control_home` + the frozen bundle in `InternalDenyPolicy`; the provider-side projection lands in WRI-002/003.)**
- [x] Editing a later role/supervisor prompt during an agent attempt cannot change the prompt used by a subsequent call in the same task. (Consumers bound to the frozen bundle; the live edit is also detected post-node and routed to manual — `test_live_control_plane_edit_during_run_is_manual_not_fallback`.)
- [x] Replacing a referenced `.worc/tools` executable or dependency during an agent attempt cannot make a later orchestrator tool node execute provider-selected bytes. (Tool nodes launch the frozen executable via a per-task `ToolRegistry(bundle.tools_dir)`; a live swap is detected — `test_live_digest_diverges_when_tool_executable_replaced`. Only the entry executable is copied; a tool's sibling dependencies are covered by detection + the future provider deny, not by copying — see follow-ups.)
- [x] Config/flow/tool mutation is detected before downstream execution and before the next task; it is a security/manual-action result, never infrastructure fallback. (`_engine_post_node` re-hash → `NodeManualRequired`; a `manual_action_required` terminal blocks the next task under the single-slot invariant.)
- [x] **Automatic** crash-recovery uses the original verified control-bundle digest and refuses a parked live edit; fresh/restart and a deliberate operator `rerun --continue` adopt a newly validated operator version and record the new digest (**amended by VF-3**). (`_prepare_control_bundle`: auto-recovery resume = `load_control_bundle` against the persisted digest + parked-conflict refuse; fresh/restart and operator `continue_task` re-freeze and overwrite — `test_autorecovery_after_parked_live_edit_stays_conflict_manual`, `test_continue_task_after_parked_live_edit_adopts_flow`, `test_parked_task_resumes_when_provider_recovers`.)
- [x] The snapshot refuses symlink/junction/reparse escapes, hard-linked aliases, special files, case collisions, and Windows alternate data streams. (`_inspect_source` + case-fold key check, via the injected inspector — `test_freeze_refuses_non_single_link_regular_source`.)
- [x] Legitimate install/upgrade/operator edits between tasks remain supported and do not require relocating the control plane. (The freeze copies from live `control_home` and never writes it; a fresh task re-freezes and adopts the operator's current version.)

## Verification

- End-to-end fake-provider tests that mutate active/later role files, supervisor prompts, tool executables/dependencies, flow YAML, and config during a provider attempt.
- Tests proving later nodes use the frozen bytes and the live mutation blocks the run before execution.
- Fresh/restart/continue and parked-task conflict tests.
- Native Windows/macOS/Linux filesystem identity, executable, lock, link/reparse, case, and named-stream tests.
- Provider-policy integration in WRI-002/003 proving both `control_home` and the private frozen bundle are denied.

## Out of scope

- Moving the editable operator control plane out of the repository.
- Allowing an autonomous task to edit its own active orchestrator configuration.
- General signing or distribution of operator tools.
- Provider read access to `.worc/guide/`: the control-plane deny also covers the packaged guide, so an orchestrator-run task that needs guide content must receive it as ordinary frozen task inputs; interactive (operator-session) authoring keeps using the live guide and is unaffected.

## Likely implementation areas

- src/wastech_orchestrator/runtime_layout.py
- src/wastech_orchestrator/core/flow/registry.py, prompt.py, tools_registry.py, and node wiring
- src/wastech_orchestrator/core/supervisor.py and orchestrator.py
- src/wastech_orchestrator/providers/claude.py and codex.py
- tests/core/, tests/providers/, tests/security/
- docs/operations.md, docs/flow-authoring.md, and packaged guide
