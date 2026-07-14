# Conventions and Maintenance Rules for Functional Documentation

IMPORTANT: This is not the source of truth. See the code instead.

This document describes how to maintain and keep current the documentation set in `docs/functional/` (index, block registry, cross-cutting flows, and block files). This is a **meta-document**: it does not describe system behavior — it defines the rules for working with the documentation itself.

## Documentation Language

- This applies to `index.md`, `block-registry.md`, `system-flows.md`, all files in `blocks/` and `flows/`, and this file.
- Technical identifiers within the text remain as they appear in code: file names, function names, class names, status values, enumerations, flags, and paths are not translated.

## Source of Truth

- Document **only** what is confirmed by executable code and tests.
- The following are **not** used as sources (even as supplementary references): README and existing documentation, architectural descriptions and diagrams, comments/docstrings, task/issue/PR titles, statements about how the system "should" work.
- Purpose must not be inferred from a file/directory/class/function/variable name. Confirm through actual calls, dependencies, inputs, conditions, state changes, side effects, and return values.
- A test is evidence of behavior only based on its setup, invocation, and the actual assertion — not based on its name.
- The 2026-06-21 reconstruction rebuilt every block, flow, and top-level document from the code alone (the prior prose was not trusted).

## When to Update Documentation

Update `docs/functional/` **in the same change** as the code whenever the following changes:

- an entry point, route, CLI subcommand, background task, or event handler;
- a contract between blocks (signatures, enumerations, schemas, statuses, error codes);
- the flow graph — a node, edge, outcome, loop/budget, ceiling (`permission_ceiling` / `output_policy` / `publishing` / `network_policy`), or a packaged flow YAML;
- an external integration, storage, data schema, or artifact format;
- a check, constraint, branch, side effect, or error handler inside a block;
- a new independent responsibility appears (new block) or an existing one disappears.

Purely cosmetic code changes that do not alter boundaries/behavior/contracts do not require documentation updates.

## Update Order

1. Identify the affected block (via `block-registry.md`). If the responsibility is new — create a new block (see below) rather than expanding someone else's.
2. Re-read the actual code and tests for the affected area; do not rely on the previous documentation text.
3. Update the block file `blocks/<id>-<short-name>.md` using the template, verifying each statement against the code.
4. Update mutual references in related blocks (the "Dependencies: Uses / Used by" section).
5. If entry points, storage, integrations, or relationships have changed — update `index.md`.
6. If a flow graph changed — update the affected `flows/<flow>.md` (and `flows/index.md` if the model changed), without duplicating block details.
7. If a cross-cutting scenario changed — update `system-flows.md`.
8. Update `block-registry.md`: purpose, entry points, dependencies, status.
9. Run the quality checks (see checklist) and verify that all links resolve.

## What Counts as a Separate Block

Extract behavior into a separate block if it combines several of the following traits: independent task; separate entry point; its own rules/constraints; separate state/dataset; separate external system; used by multiple parts; separate lifecycle; independent result/side effect; describable independently of implementation details; clear responsibility boundary.

- Do not create a block for every file/class/function/directory.
- Do not merge different responsibilities just to reduce the number of files, and do not split a single behavior just because it is spread across different modules.
- A module that is not an independent block is marked in the registry as `excluded` with a brief reason and assigned to the block of which it is a part.

## Block File Template

Each file `blocks/<id>-<short-name>.md` opens with a reconstructed-from-code blockquote and a `**Status:** … · **Source modules:** …` line, then these sections: **Responsibility**; **Public surface** (key symbols with `file:line`); **Behavior** (subsections, with a mermaid diagram where it clarifies); **Invariants & guarantees**; **Dependencies** (Uses / Used by); **Tests**.

- One file — one logical block.
- Block identifiers (`B01`…`B32`) and short file names are fixed; a new block receives the next available `Bxx` and does not reuse a freed one.

## Flow Documents (`flows/`)

The pipeline is data, not a fixed stage sequence: each `task_type` resolves to a flow — a YAML graph of typed nodes driven by the flow engine ([B28](blocks/B28-flow-engine.md)). The `flows/` directory documents this from the graph's perspective:

- `flows/index.md` — the flow model (node kinds, edges/outcomes, loops/budgets, decomposition regions, the supervisor layer above all flows).
- one document per packaged flow — `flows/implementation.md`, `flows/deep-research.md`, `flows/security-audit.md` — each describing that flow's node-by-node graph.

Flow docs **reference** the `B**` blocks that implement the mechanics without duplicating them. (The former per-step `S01`–`S08` stage documents are retired: the system no longer has a fixed eight-stage pipeline.) Same requirements: English, code-confirmed content only, `file:line` references, diagrams where appropriate. The related C4 layer is the dynamic view in `docs/likec4/` (see its README; note it is maintained separately and may lag).

## Registry Statuses

- `discovered` — identified, not yet investigated; `in-progress` — being analyzed; `documented` — investigated and documented; `needs-review` — behavior cannot be established unambiguously; `excluded` — reviewed, but not an independent block.
- A block entry is complete only in the `documented` or `needs-review` status. `needs-review` is required when behavior could not be established unambiguously (rather than left as-is).

## Links and Evidence

- Use relative Markdown links; make them mutual and accompany them with a brief explanation of the relationship.
- Back each significant statement with a reference to the code/test location in `file:line` format (e.g., `[orchestrator.py:342](../../src/wastech_orchestrator/core/orchestrator.py#L342)`).
- Paths are relative to the file's location: from `blocks/<...>.md` and `flows/<...>.md` to code — `../../../src/...`; from `index.md`/`system-flows.md`/`block-registry.md` — `../../src/...`.
- Do not reference a non-existent block file without first registering it in the registry.

## Style

- Write concisely and precisely; do not paraphrase code line by line. Include technical details only when they affect boundaries, execution order, result, constraint, error, state change, or relationship.
- Distinguish between a block's own work, initiation, delegation, use of another block's result, and another block's side effect.
- Do not present unreachable code as working behavior; mark it as unreachable/unconfirmed.
- Speculative wording is prohibited: "probably", "most likely", "apparently", and similar. State unestablished facts as such, specifying what is unknown and which areas were checked.
- Markdown: prose **without hard wrapping** — one paragraph = one line (wrap only softly in the editor; do not insert manual line breaks in prose). Surround headings and lists with blank lines; do not use bold text instead of a heading; increment heading levels one at a time.
- Markdown formatting is standardized by **Prettier** (`proseWrap: never`, see `.prettierrc.json` in the root): `npx prettier@3 --write "**/*.md"`. It removes hard wrapping, aligns tables and blank lines around headings; it does not touch code blocks, mermaid, or links. Exceptions are in `.prettierignore`.

## Diagrams

- Format diagrams as a code block with the `mermaid` language tag — it renders on GitHub and in VS Code and remains as text visible in diffs.
- The same source-of-truth rule applies to diagrams as to text: only what is confirmed by code. Do not add edges, states, or relationships "for completeness" if they are not in the code.
- Add a diagram where it clarifies order, state, or relationships (state machine, call sequence, dependency map, flow graph) rather than trivially duplicating a list.
- Keep the diagram up to date in the same change as the code (just like the text).
- Labels should use plain English; technical identifiers, statuses, and names remain as they appear in code.

## Adding and Excluding Blocks

- **New block:** add an entry in `block-registry.md` (status `discovered`), reserve the file `blocks/<id>-<short-name>.md`, update the map in `index.md`, and add mutual references only after confirming actual interaction.
- **Exclusion:** if a registered element turns out not to be an independent block — move it to `excluded` with a brief reason and transfer its description to the absorbing block.

## Pre-completion Checklist

- The description is based solely on code and tests; all confirmed entry points have been found.
- Boundaries and what the block does not do are defined; the main and alternative scenarios are confirmed.
- Checks, constraints, errors, and side effects are described; dependencies and mutual references are correct.
- No code paraphrasing, speculation, or filler; every significant statement is verifiable.
- Registry, index, flows, and (if necessary) cross-cutting scenarios are consistent; all statuses are final.
- All relative links (to blocks, flows, and to code/tests) resolve.

Quick check for links to block/flow files and to code/tests (run from `docs/functional/`):

```bash
# broken links to local .md files from index/registry/system-flows
for f in index.md system-flows.md block-registry.md; do
  grep -oE '\]\([^)]+\.md[^)]*\)' "$f" | sed -E 's/^\]\(//; s/\)$//; s/#.*$//' \
    | sort -u | while read -r t; do [ -f "$t" ] || echo "$f -> $t"; done
done

# broken links to code/tests from block files (../../../<path>)
for f in blocks/*.md flows/*.md; do
  grep -oE '\]\(\.\./\.\./\.\./[^)]+\)' "$f" | sed -E 's/^\]\(//; s/\)$//; s/#.*$//' \
    | sort -u | while read -r t; do d=$(dirname "$f"); [ -e "$d/$t" ] || echo "$f -> $t"; done
done
```
