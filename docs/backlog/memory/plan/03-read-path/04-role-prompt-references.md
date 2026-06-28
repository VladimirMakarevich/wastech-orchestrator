# 03.4 — Role-prompt references (default set, editable)

[phase](index.md) · [design §6,§9](../../design.md) · [acceptance: AC-R1/R4](../../acceptance-criteria.md)

**Goal:** reference `{memory_path}` in the packaged `planning` / `implementation` / `review` / `fixing` role prompts, seeded into `.worc/flows/roles/` at install — editable configuration, not a Core constraint.

## Scope

In: edit the packaged role-prompt sources to reference `{memory_path}`; confirm install seeds them as active editable copies. Out: the allowlist + builder (03.3).

## Approach

- Edit the packaged role prompts under the packaged flows tree (seeded by `_copy_packaged_flows`, `src/wastech_orchestrator/cli.py` ~line 600; source root `_flows_root()` ~line 587) to reference `{memory_path}` — ideally inside a conditional `{?memory_path}...{/memory_path}` block so an empty packet cleanly disappears (AC-R4).
- **Default set** = `planning` / `implementation` / `review` / `fixing` (FR4). Any other node opts in by adding the variable, with no Core change.
- These are **active editable copies** (per the install-seeds-flows precedent): skip-existing on re-run, `--reconfigure` backs up + overwrites, so an operator's edits survive a re-install.

## Files

- `packaged/.../flows/roles/*.md` — the four default role prompts.

## Tests

- The four default prompts render a packet reference when memory is on.
- They render cleanly (no dangling section) when memory is off / empty (AC-R4).
- An operator edit survives a re-install (skip-existing).

## Done when

The default prompts reference `{memory_path}`; install seeds them; AC-R1/R4 hold for the default set.
