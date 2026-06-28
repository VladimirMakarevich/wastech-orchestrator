# 03.3 — `memory_path` prompt variable (node-driven)

[phase](index.md) · [design §6,§9](../../design.md) · [acceptance: AC-R1](../../acceptance-criteria.md)

**Goal:** expose the packet to **any** node whose role prompt references `{memory_path}` — no hardcoded node set — pointing at the per-node packet file, never the memory root.

## Scope

In: add `memory_path` to `ALLOWED_PROMPT_VARS`; build + populate a per-node packet for any node referencing the variable; inject the packet path. Out: which packaged prompts reference it (03.4), empty-state guarantee (03.5).

## Approach

- Add `memory_path` to `ALLOWED_PROMPT_VARS` (`src/wastech_orchestrator/core/prompts.py` ~line 21 — currently a `frozenset` of 13 names). `None → ""` is already handled (`prompts.py` ~line 73), and the conditional `{?memory_path}...{/memory_path}` block form (~lines 63–66) lets a prompt cleanly drop the section when memory is empty.
- **Node-driven** (FR4/D5): for each node about to run, detect whether its (operator-editable) role prompt references `{memory_path}`; if so, build a packet via `PacketBuilder` (03.1/03.2) and set the variable to **that per-node packet path** — never the memory root. Skip nodes that don't reference it. No hardcoded node list, so custom operator nodes opt in with **no Core change**.
- Disabled (Q10) or no relevant memory → the variable renders empty, leaving the prompt unaffected.

## Files

- `src/wastech_orchestrator/core/prompts.py` (allowlist); the packet build/inject site in the node-prompt path (engine driver / orchestrator).

## Tests

- A **custom** node referencing `{memory_path}` gets a packet with no Core change (FR4); a node not referencing it gets none.
- Disabled → empty string; prompt unaffected (Q10 / AC-S4).
- The agent is handed the per-node packet path, never the memory root (AC-R1).

## Done when

`{memory_path}` is allowlisted and fully node-driven; AC-R1 holds.
