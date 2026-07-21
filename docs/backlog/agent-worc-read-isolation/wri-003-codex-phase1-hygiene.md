# WRI-003 — Codex Phase-1 hygiene (obscurity) with honest limits

**Status:** open **Phase:** 1 (hygiene) **Source:** [decision record](README.md) **Dependencies:** WRI-001

## Problem

Codex has no per-path read deny, and its OS sandbox confines writes/network, not reads. A real Codex read-deny is a Phase-2 concern (an OS-sandbox profile — WRI-006, aligned with [CODX-003](../codex-provider-improvements/p0-codx-003-enforce-deny-policy.md)). In Phase 1 the only available lever is to not hand the agent any private-home path (guaranteed by WRI-001) and to instruct it not to wander there. This is obscurity, not enforcement, and must not be mistaken for a guarantee (delivery rule: a warning/obscurity may not be marked as completion of an enforcement outcome).

## Required outcome

In Phase 1, Codex runs are handed only exchange paths, plus a concise role-prompt hygiene line telling the agent its inputs are the provided paths and that it must not read the runtime home. Every surface — code comments, docs, status — states plainly that Phase-1 Codex read-isolation is best-effort obscurity, with the real fix tracked in WRI-006.

## In scope

- Add a short hygiene instruction to the packaged role prompts (or a shared preamble the renderer prepends) telling the agent to work from the provided context paths and not to read `.worc/`.
- Documentation that clearly labels Phase-1 Codex read-isolation as obscurity, not enforcement, and links WRI-006 / CODX-003 for the guarantee.

## Acceptance criteria

- [ ] No private-home path is ever injected into a Codex prompt or the context-files footer (follows from WRI-001; assert it in a test).
- [ ] Packaged role prompts (or the shared preamble) carry the hygiene note; the note contains no secrets or absolute private paths.
- [ ] Docs state plainly that Phase-1 Codex read-isolation is obscurity, not enforcement, and reference WRI-006 / CODX-003.
- [ ] No claim of enforcement appears in code comments, docs, or run status for Phase 1 on Codex.

## Verification

- Prompt-audit / footer test: a Codex run's rendered prompt contains no `.worc/` path.
- Docs review for the honest-labeling requirement.

## Out of scope

- Any Codex read enforcement (WRI-006).
- Relocating the private home (WRI-005).

## Likely implementation areas

- src/wastech_orchestrator/packaged/flows/\*\*/\*.md (role prompts)
- src/wastech_orchestrator/core/flow/prompt.py (only if a shared preamble is used)
- docs/operations.md, docs/configuration.md
