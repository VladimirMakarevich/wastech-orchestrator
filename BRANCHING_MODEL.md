# Three-branch model (`dev` / `main` / `release`) — setup spec

Status: **implemented** (Phases 1–4 landed 2026-07-25/26) Date: 2026-07-25 Owner: Vladimir Makarevich

This was the implementation spec; the model is now **in effect**. It is kept as the design record — the rationale, the verified merge matrix, and the reasoning behind the seal. **For day-to-day work follow [.agents/rules/git-workflow.md](.agents/rules/git-workflow.md) §A instead** (branch model, the two hard rules, the touchpoint table, and the command recipes for merging `dev → main`, cutting a release, and porting a hotfix); this file is not the operating manual and its migration commands must not be re-run — in particular **do not create a second seal**.

What actually landed:

| Phase | Result |
| --- | --- |
| 1 — neutralise the shared files | PR #40 (`1022cc5`) + PR #41 (`d7c9608`), the latter fixing 11 links in `docs/backlog/` that this spec's link audit missed |
| 2 — create `dev`, remove the derived docs | `cdd6a32` — 20 tracked files removed; under `docs/`, `dev` keeps only `backlog/` |
| 3 — the seal | `8587cbe` = `git merge -s ours dev` on `main`; tree byte-identical, `git diff HEAD~1 HEAD` empty |
| 4 — create `release` | cut from `main` at `8587cbe`, full history inherited |
| 5 — round trip on the live branches | not yet run; the full merge matrix was verified on clones of the sealed history |

Two corrections to the text below, found while implementing:

- The link audit ("only three files need it") was incomplete — `docs/backlog/` stays on `dev` and linked out to the derived docs 11 times across 7 files. Fixed in PR #41. The file counts in the [concrete partition](#concrete-partition) and [acceptance criteria](#acceptance-criteria) are also stale: 20 tracked files were removed (16 markdown + 3 site assets + the LikeC4 model), leaving 30 markdown files under `docs/backlog/` out of 46 on `main` — not 18/35/49.
- "hotfix on `main` → `git cherry-pick` into `dev` → clean" holds only for a **code-only** commit. One that also touches a `main`-only document conflicts on that path as `DU`; resolve with `git rm <path>` then `git cherry-pick --continue`.

## Goal

Split the repository into three long-lived branches so that day-to-day development never touches, reads, or is distracted by descriptive documentation, while the documentation itself stays complete and accurate on the published branches.

- **`dev`** — the working branch. Every feature branch is cut from it and merged back into it. It carries **no derived documentation**: no architecture overview, no cookbook, no how-it-works. An agent working on `dev` physically cannot read a stale architecture document, and cannot "helpfully" rewrite one.
- **`main`** — integration + documentation. It receives `dev` through merge commits and is the only branch where `docs/` is written. Documentation is **reconstructed by reverse engineering** from the changes arriving from `dev`, as a separate task with its own lifecycle.
- **`release`** — published stable versions. Version tags (`v*`) are cut here; the package release and the documentation site publish from it.

Flow: `feat/… → dev → main → release`.

## Why the naive approach silently destroys the documentation

Git merges three-way. Branch `dev` off `main`, delete `docs/` on `dev`, then merge `dev → main`:

- merge base (the branch point) — documents present
- `dev` side — deleted
- `main` side — untouched

Git reads that as a deliberate deletion and applies it. Verified on a scratch repository:

```
### merge dev -> main (naive)
exit=0
docs after merge: ls: docs: No such file or directory   ← documentation removed from main
```

Note `exit=0`: **there is no conflict and no warning.** The documentation is simply gone from `main`, and only a diff review would catch it. And once `main` starts editing a document that `dev` deleted, every subsequent merge raises a `modify/delete` conflict on that file — forever, on each merge.

So "just delete the docs on `dev`" is not a viable starting point. The divergence has to be recorded in history once, deliberately.

## The mechanism: a one-time seal commit

`main` must record a merge commit in which `dev`'s deletion commit becomes an **ancestor of `main`**, while `main`'s own tree stays byte-for-byte unchanged. From then on the merge base of the two branches is already a docs-less tree, so on every later merge the documentation reads as _added on the `main` side_ — and an addition on one side with no counterpart on the other never conflicts.

`git merge -s ours` does exactly this: it records both parents and keeps the current branch's tree verbatim. Verified:

```
### SEAL: git merge -s ours dev
seal exit=0
main docs: architecture.md cookbook.md
(git diff HEAD~1 HEAD is empty — the seal did not touch main's tree)
```

The seal is a one-time operation, it must be done **locally with the real `git` CLI, and pushed** — a GitHub pull request cannot perform an `-s ours` merge.

## Verified merge matrix

Every scenario below was executed on scratch repositories after the seal. All completed with `exit=0`, no conflicts, correct trees:

| Scenario | Result |
| --- | --- |
| `dev` changes code → merge into `main` | clean; `docs/` intact on `main` |
| `main` rewrites `worc_architecture.md` and adds a new doc, `dev` changes code → merge | clean; `main`'s doc edits preserved |
| `dev` writes `docs/research/<id>/` (a `deep_research` deliverable) → merge | clean; lands alongside the existing docs |
| hotfix on `main` → `git cherry-pick` into `dev` → merge back | clean; documentation did not leak into `dev` |
| feature branch cut from `dev`, PR opened against `main` by mistake | clean; does **not** delete docs (the merge base is already docs-less) |

## Two hard rules

### 1. Never merge `main → dev`

This is the single action that breaks the model. Verified:

```
### naive back-merge main -> dev
dev now has docs: architecture.md cookbook.md   ← documentation leaked into dev
```

Once that merge is recorded, `dev` has the documentation back and the whole arrangement has to be rebuilt. The same applies to `release → dev`.

Consequence for practice: **never commit code directly on `main`.** All code enters through `dev`. `main` receives only documentation commits plus merges from `dev`. If a hotfix ever does land on `main` or `release` first, port it to `dev` with `git cherry-pick` (verified clean), never with a merge.

### 2. `dev → main` must be a real merge commit

A squash merge produces no merge commit, so the merge base never advances and **every subsequent merge re-proposes deleting the documentation**. Rebase-merge is likewise wrong for a long-lived branch pair. On GitHub, `dev → main` must use "Create a merge commit".

Squash is fine — and preferable — for `feat/… → dev`.

## What must NOT be removed from `dev`

"No `.md` files at all" is not implementable here. Three hard blockers, all verified against the current tree:

1. **`README.md`** — [pyproject.toml:10](pyproject.toml#L10) declares `readme = "README.md"`. Without it `pip install -e ".[dev]"` fails outright, so the entire dev environment and CI break. It stays.
2. The markdown under `src/wastech_orchestrator/packaged/` — **69 tracked files.** These are not documentation, they are **runtime assets**: flow graphs, role prompts, and the operator `guide/` shipped inside the wheel and read by the engine. Removing them breaks the product.
3. **`.agents/rules/` + `.claude/skills/` + `AGENTS.md` + `CLAUDE.md` — 17 files.** These govern how agents work; they are inputs, not output. They stay (as you specified).

## The split rule

Rather than enumerating files by hand each time, use the criterion that produced the partition above:

> **`dev` holds sources of truth and live working state. `main` holds derived description.**

Everything an agent needs in order to _do_ work stays on `dev`. Everything that _describes_ the result — and can therefore be reconstructed from the code — lives on `main` only.

### Concrete partition

On `dev`, `docs/` contains **only `backlog/`**. Removed from `dev` — 14 paths, 18 tracked files (14 markdown + 3 site assets + the LikeC4 model), leaving 35 markdown files under `docs/`:

```
docs/analysis/            docs/glossary.md        docs/task-authoring.md
docs/assets/              docs/how-it-works.md    docs/telegram.md
docs/likec4/              docs/how-to.md          docs/worc_architecture.md
docs/configuration.md     docs/index.md
docs/cookbook.md          docs/operations.md
docs/flow-authoring.md
```

`docs/assets/` (the CSS/JS for the MkDocs theme) goes with them — it exists only to build the site, which only ever builds on `main` / `release`.

**`docs/backlog/` stays on `dev` — decided** (owner, 2026-07-25: "это dev-задачи"). This is the one place where "no documents on `dev`" is deliberately relaxed, because the directory is not derived description — it is the dev work queue:

- The ADRs, `issues/`, and post-mortems under it are **task inputs** — the specs that `dev` work is implemented from.

Removing it would leave `dev` work without the specs it is implemented from.

The whole directory stays, `archive/` (six retired ADRs) included. Those six are the one part that is arguably neither a live task nor derived description, but splitting a directory across the two branches would buy nothing and would cost a special case in both the Phase 2 removal filter and the CI guard. Keep the rule simple and mechanically checkable: **under `docs/`, `dev` keeps `backlog/` and nothing else.**

`docs/research/` is not in either list: it does not exist on `dev` until a `deep_research` run creates it, and it merges into `main` cleanly (verified above).

## Prerequisite: only deletions may diverge, never content

The seal makes _deletions_ free of conflict. It does **not** make _divergent content_ free of conflict. If a file exists on both branches and both branches edit it, that is an ordinary two-sided edit and it will conflict on every merge.

Since `main` only ever edits `docs/` (absent from `dev`), the invariant holds automatically — **provided the shared files are identical at the moment `dev` is created.** So the shared files that currently point at now-`main`-only documents must be fixed **on `main`, before `dev` is cut**. If instead they are fixed on `dev` after the fact, the seal will freeze `main` on the old text and the two versions diverge permanently.

Only three files need it (verified by link audit):

| File | Dangling on `dev` | Fix |
| --- | --- | --- |
| [AGENTS.md](AGENTS.md) | `docs/worc_architecture.md` | absolute URL to `blob/main/`; reword the "read it first" instruction to say the doc lives on `main` |
| [.agents/rules/architecture.md](.agents/rules/architecture.md) | `../../docs/worc_architecture.md` | absolute URL to `blob/main/` |
| [.claude/skills/sync-docs/SKILL.md](.claude/skills/sync-docs/SKILL.md) | `docs/configuration.md`, `cookbook.md`, `glossary.md`, `operations.md`, `worc_architecture.md` | split the skill by branch — see below |

Everything else already resolves on both branches: the remaining links point at `docs/backlog/…`, which stays.

Use `https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/<file>` for the absolute form, so the link works from either branch and from the rendered site.

### `/sync-docs` becomes branch-aware

The skill currently targets `docs/configuration.md`, `docs/cookbook.md`, `docs/glossary.md`, `docs/operations.md`, and `docs/worc_architecture.md` — none of which exist on `dev`. Its scope must split:

- **on `dev`** — sync only what is present: `.agents/rules/`, `README.md`, and the shipped `src/wastech_orchestrator/packaged/` guide and `config.example.yaml`. Additionally, record a short doc-impact note (see below).
- **on `main`** — the full `docs/` refresh, driven by the merged `dev` diff.

Detect the branch by the presence of the derived docs tree, not by branch name — that keeps it correct in worktrees and in detached HEAD.

### Optional but recommended: a doc-impact note

The reverse-engineering task on `main` reads the merged `dev` diff as its input, which is sufficient. It gets considerably cheaper if `dev` work leaves a one-line breadcrumb per change — "touched X, likely affects `configuration.md` / `worc_architecture.md`". The PR description on `dev` is the natural place; no new mechanism is needed.

## Migration

Phase order matters: Phase 1 must land on `main` before `dev` is cut, and the seal must precede creating `release`.

### Phase 1 — neutralize the shared files (ordinary PR into `main`)

Land, on `main`, through a normal pull request, before creating any branch:

1. The three link/scope fixes from the table above.
2. The `AGENTS.md` working-style rule — it currently says to update the affected docs "in the same change (use `/sync-docs`)". Reword for the two-branch reality: on `dev`, sync the rules/`README`/packaged guide; the `docs/` refresh is a separate task on `main`.
3. [.agents/rules/git-workflow.md](.agents/rules/git-workflow.md) §A — replace "Branch off `main`" with the three-branch model, and state both hard rules explicitly (no `main → dev` merge; `dev → main` is always a merge commit). §B (how the orchestrator drives _target_ repositories) is unrelated and must not change.
4. The [.claude/hooks/docs_sync_gate.py](.claude/hooks/docs_sync_gate.py) change below.
5. The CI changes below.

### Phase 2 — create `dev` and remove the derived docs

```bash
git checkout main && git pull
git config rerere.enabled true          # safety net: remembers conflict resolutions

git checkout -b dev main
# keep only docs/backlog/ under docs/
git ls-files docs/ | grep -v '^docs/backlog/' | xargs git rm -q --
git commit -m "chore(dev): drop derived docs (reconstructed on main)"
git push -u origin dev
```

PowerShell equivalent for the removal step:

```powershell
git ls-files docs/ | Where-Object { $_ -notlike 'docs/backlog/*' } | ForEach-Object { git rm -q -- $_ }
```

Verify before committing: `git ls-files docs/` must list only `docs/backlog/…`, and `pip install -e ".[dev]" && pytest -q` must still pass on `dev` (it does — `tests/test_stage_site_docs.py` uses synthetic paths and never reads a real document).

### Phase 3 — the seal (local only, cannot be done through a PR)

```bash
git checkout main
git merge -s ours --no-edit dev -m "chore: seal the docs-less dev baseline (docs stay on main)"
git diff --stat HEAD~1 HEAD        # MUST be empty — the seal must not change main's tree
git push origin main
```

If that `git diff --stat` prints anything, stop and investigate: the seal is supposed to be a pure history record.

### Phase 4 — create `release`

```bash
git checkout -b release main
git push -u origin release
```

Cut it **after** the seal so it inherits the full history.

### Phase 5 — verify the model holds

Before trusting it, run one round trip: make a trivial code commit on `dev`, merge `dev → main` with a merge commit, and confirm `docs/` is untouched on `main` and the diff contains only the code change.

## Release process

1. `dev` accumulates feature work (`feat/… → dev`, squash merges).
2. `dev → main` — merge commit. `main` now has the code but stale documentation.
3. Documentation reverse-engineering runs as its own task: branch `docs/…` off `main`, regenerate the affected documents from the merged diff, PR into `main`.
4. `main → release` — merge commit. Both branches carry documentation, so this is an ordinary merge with no special handling.
5. Tag on `release`: `git tag vX.Y.Z && git push origin vX.Y.Z`. `release.yml` runs the full quality gate, builds sdist + wheel, and creates the GitHub (pre)release; `hatch-vcs` derives the version from the tag.

**Hotfix on a published version:** branch `hotfix/…` off `release` → PR into `release` → tag. Then propagate: merge `release → main` (safe — both have docs), and `git cherry-pick` the code commits into `dev`. Never merge `release → dev`.

## CI/CD changes

### `ci.yml` — cover the new long-lived branches

Currently `push` only covers `main`, so a push to `dev` (including the merge commits that carry all real work) runs no CI outside of pull requests.

```yaml
on:
  pull_request:
  push:
    branches: ["main", "dev", "release"]
  workflow_dispatch:
```

The three-OS test matrix and the static gates need no other change — nothing in them reads `docs/`.

### `site.yml` — must never run against a docs-less tree

This is the one workflow that hard-breaks on `dev`. It triggers on **every** `pull_request` and runs `tools/stage_site_docs.py`, which copies a hardcoded list of `docs/*.md`; `mkdocs.yml` additionally sets `strict: true`. A `feat/… → dev` pull request would therefore fail on missing files.

```yaml
on:
  pull_request:
    branches: ["main", "release"]
  push:
    branches: ["main", "release"]
    tags: ["v*"]
  workflow_dispatch:
    inputs:
      deploy:
        description: "Deploy to GitHub Pages"
        type: boolean
        default: false
```

**Pre-existing bug this exposes.** The deploy job is gated on `if: startsWith(github.ref, 'refs/tags/v')`, but the current trigger is `push: branches: ["main"]` — and when a `push` trigger specifies `branches`, tag pushes do not fire it at all. So today the tag-based Pages deploy is unreachable and only `workflow_dispatch` can publish. Adding `tags: ["v*"]` above fixes it, and the three-branch model is what makes it matter: releases will be tagged on `release`.

### `release.yml` — assert the tag is on `release`

Triggering stays as-is (`push: tags: ["v*"]`). Add a guard so a tag accidentally cut from `dev` or a feature branch cannot ship. `fetch-depth: 0` is already set, so the ref data is available:

```yaml
- name: Assert the tag is on the release branch
  run: |
    TAG="${{ inputs.tag || github.ref_name }}"
    git branch -r --contains "$TAG" | grep -qE '^\s*origin/release$' \
      || { echo "::error::$TAG does not point at a commit on origin/release"; exit 1; }
```

### New: guard the two invariants in CI

Both hard rules deserve a machine check rather than trust. A small job on pull requests targeting `dev`:

- **fail if the PR source is `main` or `release`** (`github.head_ref`) — catches the back-merge that breaks the model;
- **fail if the PR adds any path under `docs/` other than `docs/backlog/` or `docs/research/`** — keeps `dev` clean permanently, so the partition does not erode one file at a time.

### `.claude/hooks/docs_sync_gate.py` — branch-aware scope

The gate blocks a turn's end when `src/` changed but nothing under `docs/` or `.agents/` did. On `dev` there is no derived `docs/` tree, so instead of disabling the gate (which loses real enforcement — the rules, the `README`, and the shipped packaged guide all still need syncing on `dev`), narrow its notion of "documentation" per branch:

```python
#: Present only on main/release — the derived docs live there (see BRANCHING_MODEL.md).
_DERIVED_DOCS_MARKER = ROOT / "docs" / "worc_architecture.md"


def _doc_prefixes() -> tuple[str, ...]:
    """Path prefixes that count as a docs change on the current branch."""
    if _DERIVED_DOCS_MARKER.exists():
        return ("docs/", ".agents/")
    return (".agents/", "docs/backlog/", "src/wastech_orchestrator/packaged/")
```

and have `_should_block` use `_doc_prefixes()` in place of the literal `("docs/", ".agents/")`. `_DOC_FILES = ("README.md",)` stays as is. Detecting by marker file rather than by `git rev-parse --abbrev-ref HEAD` keeps it correct inside worktrees and in detached HEAD. `_should_block` is pure and already unit-testable — add cases for both branch shapes.

## GitHub repository settings

- **Default branch: keep `main`.** It is the branch whose `README` and docs GitHub renders. Accidental `feat/… → main` pull requests are not dangerous (verified: the merge base is already docs-less, so no deletion is proposed) — they merely bypass `dev`, which the CI guard above will catch by target branch.
- **Branch protection on `main`:** require the `ci` checks; allow merge commits and **disable squash merge** for this branch, so rule 2 cannot be violated through the UI.
- **Branch protection on `dev`:** require the `ci` checks and the new guard job. Squash merge allowed (preferred for feature branches).
- **Branch protection on `release`:** require the `ci` checks; restrict who can push and tag.

## Acceptance criteria

- `git ls-files docs/` on `dev` lists only `docs/backlog/…`; `README.md`, `AGENTS.md`, `CLAUDE.md`, `.agents/rules/`, `.claude/skills/`, and `src/wastech_orchestrator/packaged/**` are all present.
- `pip install -e ".[dev]"`, `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, and `pytest` all pass on `dev`.
- `main` still has all 49 markdown files under `docs/`, and the seal commit's `git diff HEAD~1 HEAD` is empty.
- A code-only commit on `dev` merged into `main` produces a diff containing only that code change — no documentation deletions.
- A documentation edit on `main` survives the next `dev → main` merge unchanged.
- No markdown link in `AGENTS.md`, `.agents/rules/`, or `.claude/skills/` is dangling on `dev`.
- `site.yml` does not run on pull requests targeting `dev`; a tag push on `release` triggers both `release.yml` and the Pages deploy.
- The Stop docs-sync gate is satisfiable on `dev` without inventing a file, and still fires when `src/` changes alone.
- [.agents/rules/git-workflow.md](.agents/rules/git-workflow.md) §A documents the three branches and both hard rules.

## Risks to watch

| Risk | Mitigation |
| --- | --- |
| Someone merges `main → dev` | CI guard job on PRs into `dev`; documented in `git-workflow.md`; port hotfixes via `cherry-pick` |
| `dev → main` squash-merged, resetting the merge base | disable squash merge on `main` in branch protection |
| Shared file (`AGENTS.md`, a rule, a skill) edited on both branches | never edit them on `main`; all such edits flow through `dev`. Phase 1 exists precisely to make them identical up front |
| Derived docs creep back onto `dev` one file at a time | CI guard rejects new `docs/` paths outside `backlog/` and `research/` |
| Documentation on `main` drifts behind the code | the reverse-engineering task is a required step before `main → release`, not an optional one |
| `rerere` silently reapplying a stale resolution | it is a safety net only; the seal is what makes merges clean. Review merge diffs on `main` |

## Decisions on record

| Decision | Date | Rationale |
| --- | --- | --- |
| `docs/backlog/` (all of it, `archive/` included) stays on `dev` | 2026-07-25 | It is the dev work queue — the task inputs `dev` work is implemented from — not derived description |
| Only `docs/backlog/` survives under `docs/` on `dev`; everything else goes | 2026-07-25 | One mechanically checkable rule, enforceable by the CI guard, with no directory split across branches |
| Default branch stays `main` | 2026-07-25 | It is the branch GitHub renders; misaddressed `feat/… → main` PRs are harmless (verified) and CI-catchable |

No open questions remain — the spec is ready to implement.
