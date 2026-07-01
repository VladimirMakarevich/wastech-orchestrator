# Architecture as Code (C4 via LikeC4)

"Architecture as code": the system model lives in [`workspace.likec4`](./workspace.likec4), from which consistent C4 views are generated with interactive navigation, zoom, and clickable links to block documents.

The model is derived from executable code — it is the same factual model as in [`../functional/`](../functional/index.md). This is a navigable top-level map; detailed step-by-step flows, errors, and `file:line` bindings remain in `docs/functional/`.

## C4 in a Nutshell

[C4](https://c4model.com) describes a system at four levels of detail:

1. **Context** — the system and its environment (people, external systems).
2. **Container** — separately deployable units (applications, databases, storage).
3. **Component** — major building blocks inside a container.
4. **Code** — classes/functions (usually not drawn).

How this maps onto the orchestrator:

| Level | Here | View in the model |
| --- | --- | --- |
| Context | operator, human-in-the-loop, `codex`/`claude`, `git`/`gh`, Telegram | `landscape` |
| Container | single process + `state.db` / `.worc/` artifact storage | `containers` |
| Component | **functional blocks B01–B32** | `components` |
| Code | — | (skipped; details in `docs/functional/`) |

L2 is intentionally thin here: the orchestrator is a **single process**, so "containers" means the process itself plus the storage it owns; child CLIs are external systems launched as subprocesses.

## Requirements

- **Node.js 18+** (LikeC4 is a web-native tool). Check with: `node -v`.
- Nothing else to install: run via `npx`. The VS Code extension is convenient for editing.

## Quick Start (interactive view)

From the repository root:

```bash
npx likec4@latest dev docs/likec4
```

A local server opens (usually <http://localhost:5173>): live diagrams, hot-reload, clicking an element drills down a level, clicking a link opens the block document. Stop with `Ctrl+C`.

## Editing in VS Code (recommended)

1. Install the **LikeC4** extension (Marketplace, publisher `likec4`).
2. Open [`workspace.likec4`](./workspace.likec4).
3. Command palette → **LikeC4: Open Preview**.

The extension provides a live preview, **model validation**, and autocomplete — syntax errors are visible directly in the editor (this is the best way to verify edits).

## Static Site and Image Export

```bash
# self-contained site (share with the team); consider adding site/ to .gitignore
npx likec4@latest build docs/likec4 -o docs/likec4/site

# PNG for each view (for embedding in md/PR); may download Chromium on first run
npx likec4@latest export png docs/likec4 -o docs/likec4/img
```

## Views in the Model

| View | Type | What it shows |
| --- | --- | --- |
| `landscape` | view | Context (C4 L1): system + people + external systems |
| `containers` | view of `orchestrator` | Containers (C4 L2): process + `state.db` / `.worc/` storage |
| `components` | view of `proc` | Components (C4 L3) = functional blocks |
| `crosscutting` | view | Cross-cutting concerns: security, editing, observability |
| `happyPath` | **dynamic view** | Step-by-step run of a single task: `run` → … → PR |

A `dynamic view` is analogous to a sequence diagram: it shows the order of interactions over time.

## Traceability to Code

Each component carries a `link` to its block document in `docs/functional/blocks/` — from the diagram you can click through to a description with `file:line` bindings. This is the bridge between the abstract C4 model and the code-verified documentation.

## Applied Best Practices and How to Extend

Already in the model: typed element kinds (`actor`/`system`/`externalSystem`/`container`/`component`/`store`) with styles (shape, color), semantic tags (`spine`/`entrypoint`/`crosscutting`/`external`/`datastore`), descriptions and `technology`, multiple targeted views, a dynamic run, and `link` to block documents.

To extend further:

- **Icons** (looks "production-ready"): in an element's `style { … }` block add `icon tech:python`, `icon tech:sqlite`, etc. — see the icon catalog on the LikeC4 website. (Not added by default to avoid coupling to specific icon names without local verification.)
- **Split into files**: `spec.likec4` / `model.likec4` / `views.likec4` (LikeC4 merges all `*.likec4` files in the directory) — convenient as the model grows.
- **Typed relationships**: declare relationship kinds (`relationship async`, `relationship spawns`) with their own line style.
- **All 32 blocks**: a representative subset (~18, now including the B28 flow engine and B31 supervisor) is currently included; the rest are listed as comments in `workspace.likec4` and can be added following the same pattern.
- **CI**: run `likec4 build`/`export` in the pipeline to catch drift and publish the site.

## Keeping in Sync with Code (important)

The model is **manual** and has **no** `file:line` bindings (unlike `docs/functional/`). Therefore:

- when block boundaries, relationships, external systems, or storage change, update the model **in the same change** — this is enforced by the skill [`sync-docs`](../../.claude/skills/sync-docs/SKILL.md);
- keep exactly one model owner;
- the source of truth for details is the code and `docs/functional/`; this is the top-level map.

> Note: on the first `likec4 dev` run, watch for any warnings about `link` and `dynamic view` (their syntax was not verified locally at the time the file was created). Any edits are immediately visible in the preview; the easiest way to fix a bad line is to follow the LikeC4 validator's suggestion.
