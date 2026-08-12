# Architecture as Code (C4 via LikeC4)

"Architecture as code": the system model lives in [`workspace.likec4`](./workspace.likec4) — a single source file — from which consistent C4 views are generated with interactive navigation, zoom, and drill-down from an element into the view that details it. (No element carries a `link` to an external document today; the model has no `link` statements.)

The model is derived from executable code. This is a navigable top-level map of the system's components and relationships.

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
| Container | single process + the stores it reads and writes: `state.db`, the `.worc/` private home, the `.worc-io/` agent exchange, the repository working tree, and the committed `tasks/` queue | `containers` |
| Component | **functional blocks B01–B32**, plus the post-taxonomy components below | `components` |
| Code | — | (skipped; see the source under `src/`) |

`B01`–`B32` is a **closed** taxonomy — it exists only in this model. Components added after it was closed carry descriptive names instead of invented numbers: `Summary Report` (the deterministic PR body), `Exchange Publisher` (the redaction + path-safety boundary onto `.worc-io/`), `Frozen Bundles` (the per-task immutable control and instruction snapshots under `runs/`), and `Memory`. Follow that convention rather than extending the numbering. One number is **retired** rather than pending: `B04` (config discovery) describes a config block removed in schema v15 and only tolerated as a legacy key today.

L2 is intentionally thin here: the orchestrator is a **single process**, so "containers" means the process itself plus the storage it owns; child CLIs are external systems launched as subprocesses.

## Requirements

- **Node.js 22.22.3+** — LikeC4 is a web-native tool, and its current release (`likec4@1.59.2`) declares `engines.node >= 22.22.3`. Check with: `node -v`.
- Nothing else to install: the repository has no `package.json`, so LikeC4 is not a dependency here — run it via `npx`. The VS Code extension is convenient for editing.

## Quick Start (interactive view)

From the repository root:

```bash
npx likec4@latest dev docs/likec4      # `dev` is an alias of `start`
```

A local server opens at <http://localhost:5173> (the URL is printed in the terminal): live diagrams, hot-reload, and clicking an element drills down a level. Stop with `Ctrl+C`.

## Validating the Model

The model is hand-written, so the cheapest guard is the CLI validator — it checks syntax, semantics, and layout drift, and needs no browser:

```bash
npx likec4@latest validate docs/likec4     # → "✓ Valid (1 files)"
npx likec4@latest format docs/likec4       # normalize the source formatting
```

This is the check to run after every edit (and the one to wire into CI, see below); the VS Code extension reports the same diagnostics inline while you type.

## Editing in VS Code (recommended)

1. Install the **LikeC4** extension (Marketplace, publisher `likec4`).
2. Open [`workspace.likec4`](./workspace.likec4).
3. Command palette → **LikeC4: Open Preview**.

The extension provides a live preview, **model validation**, and autocomplete — syntax errors are visible directly in the editor (this is the best way to verify edits).

## Static Site and Image Export

```bash
# static site (share with the team); `site/` is already gitignored (.gitignore)
npx likec4@latest build docs/likec4 -o docs/likec4/site

# one self-contained HTML file instead of a directory
npx likec4@latest build docs/likec4 --output-single-file -o docs/likec4/site

# PNG for each view (for embedding in md/PR); may download Chromium on first run
npx likec4@latest export png docs/likec4 -o docs/likec4/img
```

Other export formats: `jpg`, `json` (the whole model as data), and `drawio`. Note that `img/` is **not** gitignored — either commit the images deliberately or export outside the repository.

## Views in the Model

| View | Type | What it shows |
| --- | --- | --- |
| `landscape` | view | Context (C4 L1): system + people + external systems |
| `containers` | view of `orchestrator` | Containers (C4 L2): process + the five stores (`state.db`, `.worc/`, `.worc-io/`, the working tree, `tasks/`) |
| `components` | view of `proc` | Components (C4 L3) = functional blocks |
| `crosscutting` | view | Cross-cutting concerns: security, redaction, the dangerous-diff gate, isolation boundaries, observability |
| `isolation` | view | What the agent may read, what it may **write** (and where writes are denied), and what is frozen per task |
| `happyPath` | **dynamic view** | Step-by-step run of a single task: `run` → … → PR |
| `failurePath` | **dynamic view** | The same run going wrong: provider fallback, quality fix loops, the resumable park, and the three terminals |
| `implementationFlow` | **dynamic view** | The default flow's node graph with its bounded fix loop |

A `dynamic view` is analogous to a sequence diagram: it shows the order of interactions over time.

## Applied Best Practices and How to Extend

Already in the model: typed element kinds (`actor`/`system`/`externalSystem`/`container`/`component`/`store`) with styles (shape, color), semantic tags (`spine`/`entrypoint`/`crosscutting`/`external`/`datastore`), descriptions and `technology`, multiple targeted views, and a dynamic run.

To extend further:

- **Icons** (looks "production-ready"): in an element's `style { … }` block add `icon tech:python`, `icon tech:sqlite`, etc. — see the icon catalog on the LikeC4 website. (Not added by default to avoid coupling to specific icon names without local verification.)
- **Split into files**: `spec.likec4` / `model.likec4` / `views.likec4` (LikeC4 merges all `*.likec4` files in the directory) — convenient as the model grows.
- **Typed relationships**: declare relationship kinds (`relationship async`, `relationship spawns`) with their own line style.
- **All 32 blocks**: the model currently holds **29 components** — 25 of the numbered blocks (including the B28 flow engine, the B31 supervisor, and the B32 checkers/tools) plus the four descriptive ones (`Summary Report`, `Exchange Publisher`, `Frozen Bundles`, `Memory`). The remaining six (`B10` recovery, `B13` skills, `B15` prompts, `B20` artifact layout, `B29` flow definition/validation, `B30` flow node runners) are listed as comments in `workspace.likec4` and can be added following the same pattern; `B04` is retired, not pending.
- **CI**: run `likec4 validate` (fails on a broken model) and `likec4 build`/`export` in the pipeline to catch drift and publish the site. Nothing runs LikeC4 in CI today — the model is checked by hand.
- **MCP**: `npx likec4@latest mcp docs/likec4` serves the model to an MCP client, the same way [.mcp.json](../../.mcp.json) already registers the Markdown linter. Not registered here yet.

## Keeping in Sync with Code (important)

The model is **manual** and has **no** `file:line` bindings. Therefore:

- **this directory lives on `main` only**, like the rest of the derived documentation, so it is never edited on `dev` — code changes reach it through the reverse-engineering docs task that runs on `main` after an integration merge (see [git-workflow.md](../../.agents/rules/git-workflow.md) §A);
- the [`sync-docs`](../../.claude/skills/sync-docs/SKILL.md) skill **excludes** `docs/likec4/` for that reason: it is regenerated on that cadence rather than patched alongside a code change, so do not expect that skill to keep the model current;
- when block boundaries, relationships, external systems, or storage change, the regeneration pass must revisit the model in the same change set as the other `docs/` documents;
- keep exactly one model owner;
- the source of truth for details is the code; this is the top-level map.

> The model validates cleanly with the current LikeC4 release (`likec4 validate docs/likec4` → `✓ Valid`), including both `dynamic view`s. Any edits are immediately visible in the preview; the easiest way to fix a bad line is to follow the validator's suggestion.
