# WRI-001 — Split private artifacts from the curated exchange

**Status:** implemented **Milestone:** 0 (foundation) **Source:** [decision record](README.md), [happy path](happy-path.md) **Dependencies:** WRI-004, WRI-008

> **Re-verified 2026-07-22** after its dependencies WRI-004 (typed layout) and WRI-008 (portable identities) landed — this task shipped ahead of both and handed them interim shims. Both consolidations are confirmed absorbed: the exchange root now flows from `layout.exchange_root` (no inline `EXCHANGE_HOME`), `git_manager` builds its ignore footprint from the shared `runtime_layout` constants, and both roots share one `assert_contained_path` belt plus the `security/identifiers` segment grammar. One test-coverage gap was closed during the re-review: the `security_audit` report-slot structured-output capture had no regression guard and the flow integration test's fake agent still modelled the old agent-written `.worc/security-reports/` contract (`tests/core/test_flow_postprocess.py` + `tests/core/test_flow_security_audit.py`).

## Problem

Agent-facing paths and private audit files currently share `<private_home>/logs/<task-id>`. Some are task-level files, some are per-run files, and provider attempts add a `sub-<NN>` level that the generic node-output run directory does not use. A blanket private-home deny would break legitimate downstream context.

The current live checks path also has a functional gap: `CheckOutcome.first_failure_log` is calculated, but `ChecksNodeRunner` does not assign `NodeInputs.checks_path` before routing to `fixing`; the path appears only after recovery rehydrates it from the state store.

## Required outcome

Create `<repo>/.worc-io/<task-id>/` as the only provider-readable orchestration surface. Dedicated exchange helpers never insert a `logs/` segment. Every exchange file is published through a redaction and path-safety boundary; private writers continue to use the private artifact helpers.

## Exchange allowlist

Only these provider inputs may be published:

- The validated task packet exposed as `{task_path}`; the lifecycle source file is never the provider path.
- `plan.md` for the `output_artifact: plan` slot.
- `current.diff`.
- The first failing command-profile checks log exposed as `{checks_path}`.
- Evaluator `findings.json` exposed as `{review_path}`.
- Generic agent `<node-id>.out.md` and tool `stdout.txt` exposed as `{<node-id>_path}`.
- Subtask specs and predecessor handoff briefs.
- Per-node memory retrieval packets, never the memory store.
- Bounded tracked skill-package snapshots selected for the node; live `SKILL.md` paths are not handed to later attempts.
- A sanitized answer-only HITL packet exposed as `AgentRunRequest.human_input_path`, never the durable interaction record or transport handle.

The task/skill source files remain in the repository but are not live provider inputs; WRI-011 owns their immutable snapshots and repository-instruction manifest. `task.enriched.md`, `output_artifact: summary`, supervisor summaries, checker JSON (`citation.json`, `dependency_scan.json`), tool stderr, rendered prompts, prompt audit, provider attempts, validation/failure reports, durable HITL state, state DB, flows, tools, memory store, and security reports stay private/provider-denied.

## In scope

- Add explicit `exchange_task_dir` and `exchange_node_run_dir` builders for `.worc-io/<task-id>/...`; do not reuse `task_artifact_dir` in a way that produces `.worc-io/logs/...`.
- Add an exchange publisher that validates containment, refuses symlinks/junctions/reparse-point escapes, writes atomically, preserves LF where byte stability matters, redacts content, and returns a POSIX display/prompt path.
- Require every published object to be a regular single-link file created by the orchestrator's atomic replace; refuse hard links, special files, case-fold/normalization-colliding relative names, and unexpected files. On native Windows, enumerate and reject/remove named NTFS alternate data streams rather than assuming the default stream is the whole file.
- Route every allowlisted writer and resolver to the exchange, including live and recovery paths, generic fan-in, decomposition, predecessor handoff, memory packets, and the provider footer.
- Provide safe publication/layout primitives for WRI-011 task and skill snapshots; WRI-011 owns source discovery, instruction precedence, and package-closure semantics.
- Audit all three current `AgentRunRequest` producers: graph agent, evaluator, and the constant supervisor. Supervisor prompt/result/attempt artifacts stay private; its provider call still passes the same current-exchange preflight and provider isolation.
- Keep the full durable HITL JSON private and create a separate redacted packet containing only the answer/approval needed by the rerun.
- Preserve authoritative private check logs and copy the redacted first quality-failure log into the exchange; set `NodeInputs.checks_path` before returning the checks `fail` outcome.
- Preserve private audit evidence for tool/agent/evaluator outputs either in existing provider/state artifacts or an explicit private copy; the split must not silently reduce the current audit record.
- Migrate the packaged `security_audit` report node off its agent-written `.worc/security-reports/` contract: the node returns the report as structured output and the orchestrator writes `report.md` privately through the standard slot/postprocess capture. `output_policy` report directories stay private, the role prompt loses its write instruction, and no agent-writable path remains in either root.
- Gitignore `.worc-io/` in both the tracked install-managed ignore block and clone-local `.git/info/exclude`, with a dedicated `git check-ignore` probe target.
- Classify `.worc-io/` as a runtime artifact everywhere Git paths are filtered, but do not treat ignore/filtering as sufficient commit protection; WRI-009 owns index mutation detection and full staged-set validation.
- Define fresh/restart/continue behavior: fresh and restart start with a clean exchange after archiving the prior attempt; continue uses the active/restored exchange and never resolves stale files from another attempt. In CLI terms: fresh/restart is `rerun` (including its restart-in-place branch for pre-checkpoint tasks on operator-owned branches), continue is `rerun --continue`; the daemon `restart` command is unrelated.
- Add a pre-launch invariant that the active exchange root contains at most the current task directory. Terminal sealing/restoration is implemented by WRI-007.
- Update all operator and shipped documentation.

## Redaction requirements

Redaction is not deferred because this directory is the sanctioned readable surface. Tests must seed secrets into each source shape and prove they do not reach the exchange:

- Structured `plan` output and evaluator findings.
- Check stdout and stderr.
- Generic agent/tool outputs.
- Diff output.
- HITL answer packet.
- Memory packet and subtask/handoff content.

If a source cannot be made safe, it stays private and the task must define a different sanitized projection; "the agent could already read it" is not an acceptance argument.

## Acceptance criteria

- [x] The on-disk layout is exactly `<repo>/.worc-io/<task-id>/...`, never `.worc-io/logs/...`.
- [x] Every non-`None` provider orchestration input path is contained under the current task exchange; `repo_path` remains workspace metadata, but no live task/skill/control/private path appears in a request, rendered prompt footer, or tool-node stdin path object.
- [x] `human_input_path` points to an answer-only exchange packet and the private Telegram/durable handle is not provider-readable.
- [x] A live checks failure sets `{checks_path}` before the fixing node runs; restart produces the same path semantics.
- [x] `enriched_spec`, publish/supervisor summaries, checker JSON, and all private audit/attempt files remain private.
- [x] The `security_audit` report is produced through orchestrator-captured structured output; no packaged flow or role prompt instructs an agent to write outside the repository workspace.
- [x] Every exchange file passes seeded-secret redaction tests and is written atomically.
- [x] Latest-run fan-in selects the newest run containing the requested file and never crosses task/attempt boundaries.
- [x] The exchange is ignored by Git, cannot be staged by code or audit commits, and has its own ignore probe.
- [ ] WRI-009 integration proves a force-added exchange file cannot survive to any orchestrator commit (cluster exit criterion — verified when WRI-009 lands, not a gate for closing this task).
- [x] A pre-existing symlink/junction/reparse point in any exchange path fails closed before a provider launch.
- [x] Hard-linked/special files, case-fold collisions, unexpected paths, and NTFS alternate data streams fail closed before launch; a clean exchange manifest covers file type, link identity/count, relative name, size, and content digest.
- [x] The implementation is node-id/topic agnostic and covers every packaged flow plus a custom-flow fixture.
- [x] Agent, evaluator, supervisor observe/finalize, fresh, and resumed provider calls all pass the same prelaunch exchange/private-path invariant.

## Verification

- Artifact-layout and exchange-publication unit tests, including repeated runs and decomposed subtasks.
- Full fake-CLI pipeline test asserting every request path and both trees.
- Live fail→fix test proving `{checks_path}` exists without a restart, plus recovery parity.
- HITL first-run/restart tests proving only the sanitized answer packet crosses the boundary.
- Seeded-secret matrix for every exchange writer.
- Git staging/ignore tests and path-escape tests for POSIX symlinks/hard links and Windows junction/reparse/hard-link/alternate-stream seams.
- Lifecycle tests for fresh, restart-in-place, and continue; terminal states are completed in WRI-007.

## Out of scope

- Provider enforcement (WRI-002/003).
- Terminal sealing/restoration (WRI-007).
- Relocating private runtime state (WRI-005).
- Freezing task/skill/repository instructions (WRI-011).
- Inlining artifact bodies into prompts.

## Likely implementation areas

- src/wastech_orchestrator/providers/artifacts.py
- src/wastech_orchestrator/core/flow/postprocess.py
- src/wastech_orchestrator/core/flow/nodes/agent.py, evaluator.py, checks.py, and tool.py
- src/wastech_orchestrator/core/flow/context_paths.py and provider request/footer code
- src/wastech_orchestrator/core/hitl.py
- src/wastech_orchestrator/core/decomposition.py and core/orchestrator.py recovery/handoff paths
- src/wastech_orchestrator/check_runner.py and git_manager.py
- src/wastech_orchestrator/composition.py and cli.py
- src/wastech_orchestrator/core/flow/output_policy.py and packaged/flows/security_audit/ (report-node migration)
- tests/, docs/, .agents/rules/, and src/wastech_orchestrator/packaged/
