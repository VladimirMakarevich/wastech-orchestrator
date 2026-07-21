# WRI-010 — Isolate and freeze the in-repo control plane

**Status:** open **Milestone:** 0 (security prerequisite) **Source:** [decision record](README.md) **Dependencies:** WRI-004, WRI-012

## Problem

WRI-005 intentionally keeps the operator control plane under `<repo>/.worc`: `config.yaml`, flow YAML, role/supervisor prompts, and executable tools. That directory remains under the provider working directory. A workspace-write provider can therefore change control inputs even after private runtime state moves elsewhere.

This is an execution boundary, not merely configuration drift. Agent/evaluator and supervisor runners read role files from the live flow directory when each later call is built, while tool nodes resolve and launch operator executables from live `.worc/tools/`. A provider can rewrite a later prompt or tool, and the orchestrator can then read/execute it outside the provider sandbox with the orchestrator's child-process authority. It can also poison `config.yaml` or another flow for the next task. Gitignore does not prevent any of these writes.

## Required outcome

Under strict isolation, providers cannot read or mutate `control_home`. At task start the orchestrator freezes the exact effective control inputs needed by that task into a private, immutable task bundle; all later flow prompt and tool consumers use that bundle rather than reopening live `.worc`. The live control tree is fingerprinted across every provider attempt so a bypass becomes a security violation before another orchestrator-controlled consumer or task can run.

The operator may deliberately modify the live control plane between tasks. Such changes are adopted only by a fresh task after normal validation; a continue/resume keeps the original frozen bundle and digest.

## In scope

- Add `control_home` and the task's frozen control bundle to the provider-internal denied-root policy. WRI-002/003 project the policy into provider-specific enforcement; the Core remains provider-neutral.
- Snapshot the selected flow definition, every referenced agent/evaluator/supervisor role file, resolved tool bundle/executable identity, and the effective configuration/version metadata needed to reproduce the run. Store it under `private_home`, never the exchange.
- Bind flow runners, supervisor prompt rendering, and tool-node resolution to the frozen bundle. Do not retain a live `flow_dir`/`tools_dir` fallback after the first untrusted provider attempt.
- Hash the relevant live control inputs into parent-held state before each provider attempt and verify them only after WRI-012 proves the provider containment empty. A change is a non-fallback security violation; do not execute a changed tool/prompt, silently refresh the snapshot, or destroy operator-owned evidence.
- Before launching a next task, verify that any previous violation is resolved and load/validate a new snapshot. Define operator edits made while a task is parked as a conflict requiring fresh/restart rather than merging them into continue.
- Use regular-file, containment, link/reparse, case-fold, and executable-identity checks. A snapshot must not follow an external symlink/junction or preserve a hard-link back to mutable live control data.
- Keep install/upgrade/operator editing on live `control_home`; the frozen bundle is an execution/audit object, not a new authoring location.
- Redact audit metadata and never include config/env contents in a provider request or exchange packet.

## Acceptance criteria

- [ ] No provider request can read or write live `.worc` control files under strict isolation; private/runtime and control roots remain denied after WRI-005 separates them.
- [ ] Editing a later role/supervisor prompt during an agent attempt cannot change the prompt used by a subsequent call in the same task.
- [ ] Replacing a referenced `.worc/tools` executable or dependency during an agent attempt cannot make a later orchestrator tool node execute provider-selected bytes.
- [ ] Config/flow/tool mutation is detected before downstream execution and before the next task; it is a security/manual-action result, never infrastructure fallback.
- [ ] Continue/resume uses the original verified control-bundle digest. Fresh/restart may adopt a newly validated operator version and records the new digest.
- [ ] The snapshot refuses symlink/junction/reparse escapes, hard-linked aliases, special files, case collisions, and Windows alternate data streams.
- [ ] Legitimate install/upgrade/operator edits between tasks remain supported and do not require relocating the control plane.

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
