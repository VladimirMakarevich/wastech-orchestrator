# WRI-011 — Freeze task, skill, and repository instruction inputs

**Status:** open **Milestone:** 0 (security prerequisite) **Source:** [decision record](README.md) **Dependencies:** WRI-001, WRI-010

## Problem

The previous plan kept the task file and selected `SKILL.md` paths in the writable repository and allowed provider-native project instruction discovery. A workspace-write node can modify any of them. A later evaluator, supervisor, resumed session, or fallback provider can then receive different instructions from those validated at task start.

Codex makes this especially important: marking project `.codex/config.toml` untrusted does not disable `AGENTS.md`; Codex discovers `AGENTS.md` separately on every new run. The current WRI-003 wording explicitly preserved that live discovery. Claude has the analogous `CLAUDE.md`/project customization surface unless safe mode disables it. Permission profiles can make a known file read-only during one attempt, but they do not stop an earlier legitimate repository edit from becoming a later node's new instruction source.

## Required outcome

Before the first untrusted provider attempt, freeze every agent instruction/context input whose identity should remain stable for the task:

- the validated task specification;
- the bounded tracked skill inventory metadata/packages from which selected node skills will be chosen;
- the applicable repository instruction set needed by the provider contract;
- the WRI-010 active flow/control instruction bundle.

Provider requests use the frozen task/skill paths, and provider-native live project instruction discovery is disabled. Each adapter injects the frozen repository instruction set through an orchestrator-owned supported high-priority instruction surface. Fresh, resume, evaluator, supervisor, and provider fallback all use the same manifest digest. Ordinary repository source remains live and editable.

## In scope

- Publish a redacted, immutable task packet under the current exchange and point `AgentRunRequest.task_path` to it. Deny provider writes to the original task/lifecycle location so status/rerun bookkeeping cannot be corrupted.
- Before the supervisor skill-map proposal, snapshot the task packet and a bounded, validated inventory of every candidate skill's metadata/package identity. The proposal receives exchange paths and small allowlisted metadata, never inline task title/description bodies or live skill paths. After Core accepts the proposal, expose only the selected packages to each node.
- Snapshot each selected tracked skill into a bounded exchange package. Copy only tracked regular files inside its declared/package directory, enforce count/byte caps, reject links/reparse points/hard links/special files/ADS/case collisions, and preserve relative layout. A root-level or cross-directory skill whose resource closure cannot be represented safely fails strict resolution with an actionable packaging error rather than reading live mutable guidance.
- Define the repository instruction inventory explicitly. Capture applicable root/nested `AGENTS.md`/`AGENTS.override.md`, Claude instruction files, and bounded tracked instruction resources required by repository policy. Record exact paths/digests and reject ambiguous escapes or provider-specific discovery that cannot be reproduced.
- Disable Codex live project-doc discovery (for example, controlled `project_doc_max_bytes`/fallback settings on the supported CLI) and inject the frozen instruction content through an adapter-owned developer/instruction layer. Project config trust and AGENTS discovery are separate controls and must be tested separately.
- Keep Claude project/user/local customization discovery disabled and inject the same frozen repository policy through its supported adapter-owned system/developer instruction surface. Do not re-enable `.claude` settings, hooks, agents, or skills to regain instructions.
- Preserve instruction precedence deliberately: built-in provider safety/system policy, orchestrator security contract, frozen repository instructions, flow role, and task/context paths. Provider fallback must not silently interpret the same files at a weaker precedence.
- Bind every provider session/lineage record to the instruction-manifest digest. Resume is allowed only when the digest matches; fresh/restart or any accepted new instruction bundle starts a new provider session instead of mixing old conversation context with new files.
- Store the canonical instruction bundle privately for audit and expose only the paths/packages intentionally needed by the model. Redaction must not silently change a task/skill semantic requirement; if a required input contains a detected secret that cannot be safely projected, stop before launch.
- Remove inline task title/description content from every constant-supervisor prompt path, including skill proposal and finalize. Small identifiers such as validated `task_id` may remain metadata; the task body/title is read from the frozen exchange packet through `AgentRunRequest`.
- On continue, verify and reuse the original manifest. Fresh/restart may create a new snapshot after revalidation. A provider edit to the live task/skill/instruction source may be an ordinary proposed repository diff, but it cannot alter the running task's frozen inputs.

## Acceptance criteria

- [ ] A workspace-write node that edits its source task file cannot change any later agent/evaluator/supervisor/fallback task instructions or lifecycle identity.
- [ ] The earliest supervisor skill proposal already runs behind the adapter isolation boundary and receives the frozen task/inventory without inline task title/description or live skill paths.
- [ ] A node that edits a selected `SKILL.md`, resource, `AGENTS.md`, `CLAUDE.md`, or referenced repository rule cannot change the instruction bundle used later in the same task.
- [ ] Codex project `.codex` trust and live `AGENTS.md` discovery are independently disabled/proven; the frozen instruction digest is identical on fresh/resume and fallback.
- [ ] Claude safe/config-isolation mode does not drop required repository rules; they arrive only through the controlled frozen layer.
- [ ] Task/skill exchange paths are read-only under WRI-002/003 and contain no untracked files, links, special files, case collisions, ADS, or content beyond bounded allowlists.
- [ ] A required secret match or unrepresentable skill/instruction resource closure fails before model execution rather than being truncated, read live, or silently redacted into different instructions.
- [ ] Tasks that legitimately edit repository guidance/skills can still propose those source changes; only the running instruction snapshot is immutable.
- [ ] A session whose recorded instruction digest differs is never resumed; fresh/restart uses a new session and continue requires the original verified digest.

## Verification

- Multi-node and cross-provider fake-CLI tests that mutate task, skill resources, AGENTS/CLAUDE files, and referenced rules between calls, then assert byte-identical later instructions.
- Current Codex CLI tests for project-doc discovery disablement plus controlled developer instructions, separately from `.codex` trust.
- Current Claude CLI tests for safe/settings isolation plus controlled instruction injection.
- Skill package tests for tracked/untracked files, root-level packages, caps, symlink/junction/hard-link/reparse/ADS/case behavior, spaces, Unicode, and Windows paths.
- Fresh/restart/continue and provider-fallback manifest tests.

## Out of scope

- Freezing the entire source workspace; code and ordinary documentation remain the object being edited/reviewed.
- Treating arbitrary repository content as trusted instructions.
- Allowing provider-native user/project skills or instruction discovery in strict autonomous runs.

## Likely implementation areas

- src/wastech_orchestrator/core/skills.py and orchestrator.py
- src/wastech_orchestrator/core/flow/wiring.py and provider request construction
- src/wastech_orchestrator/providers/claude.py and codex.py
- src/wastech_orchestrator/runtime_layout.py and exchange publisher
- tests/core/, tests/providers/, tests/security/
- docs/operations.md, docs/configuration.md, and packaged guide
