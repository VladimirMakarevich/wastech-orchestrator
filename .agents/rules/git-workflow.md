# Git workflow

There are two levels of git here: (A) how the orchestrator **itself** is developed; (B) how the orchestrator works with **target** repositories. Do not conflate them.

## A. Developing the orchestrator itself

### Four long-lived branches

`feat/… → dev → main → release` for code; `main → site` for the documentation.

- **`dev`** — the working branch. Cut every `feat/…`, `fix/…`, `chore/…` branch off `dev` and merge it back into `dev` (squash is fine, and preferred). It carries the code plus `docs/backlog/` (the task queue work is implemented from) and nothing else under `docs/`. Never push to `dev` directly — land changes through a PR, merged only after checks pass.
- **`main`** — integration. It receives `dev` through merge commits. Its tree has the same shape as `dev`'s: no derived documentation, no site machinery. Never commit code directly on `main`.
- **`release`** — published stable versions. `main → release` by merge commit, then `git tag vX.Y.Z` here; the package release publishes from this branch.
- **`site`** — the documentation and the published site. It receives `main` through merge commits and is the **only** branch that carries the derived `docs/` tree, `mkdocs.yml`, `tools/stage_site_docs.py`, and `.github/workflows/site.yml`. Documentation is reconstructed by reverse engineering from the diff arriving from `main`, as its own task: branch `docs/<slug>` off `site`, PR back into `site`. Every push to `site` republishes the site.

### The one hard rule

**`site` is a sink: nothing is ever merged out of it, and `main → site` is always a real merge commit.**

Both halves protect the same thing. `site` and `main` diverge by one recorded seal commit (`git merge -s ours main`, made on `site` when the documentation moved there). The seal is what makes `main`'s missing `docs/` tree read as _added on the `site` side_ rather than _deleted on the `main` side_ — and an addition on one side with no counterpart on the other never conflicts. A squash merge records no merge commit, so the merge base never advances and every later merge re-proposes deleting the whole documentation set; rebase-merge is likewise wrong for a long-lived branch pair. A merge in the other direction copies the documentation onto a branch that must not carry it, and the arrangement has to be rebuilt. The seal is already in history — **do not create a second one.** If a fix ever lands on `site` first, port it to `dev` with `git cherry-pick`.

`main → dev` is ordinary and allowed: `main` carries nothing `dev` must not have. The design record behind the model is [BRANCHING_MODEL.md](../../BRANCHING_MODEL.md).

### Where the branches touch

Every legal transition, and the two illegal ones. The mechanism column is the part that matters: picking the wrong one is how the model breaks.

| From → To | Mechanism | Why this one |
| --- | --- | --- |
| `feat/…` → `dev` | **squash** merge (PR) | Keeps `dev` history one commit per change. |
| `dev` → `main` | **merge commit**, never squash/rebase | Advances the merge base. A squash leaves `dev` quietly behind `main` and every later merge re-proposes the same content. |
| `main` → `release` | merge commit | Ordinary — both branches are code-only. |
| `hotfix/…` → `release` | squash or merge (PR) | Published-version fix; tag afterwards. |
| `release` → `main` | merge commit | How a hotfix gets back into integration. |
| `main` → `dev` | merge (PR) | Allowed. Rarely needed, never harmful. |
| `main` → `site` | **merge commit**, never squash/rebase | Advances the merge base past the seal. A squash re-proposes deleting all of `docs/` on every later merge. |
| `docs/…` → `site` | squash or merge (PR) | The documentation refresh is ordinary work on `site`; it never touches the code branches. |
| `site` → anywhere | **FORBIDDEN** — use `git cherry-pick` | Copies the documentation onto a branch that must not carry it. |
| `feat/…`/`fix/…`/`chore/…` → `main` | **FORBIDDEN** — retarget at `dev` | Bypasses `dev`, so `dev` falls behind `main`. It merges cleanly and raises no conflict, so only `branch-guard` catches it. |

Three more contact surfaces that are not merges:

- **Shared files** — everything except the derived documentation and the site machinery exists on **all four** branches. Edit them **only on `dev`**. Editing one on `main` or `site` makes the content diverge, and a divergent shared file conflicts on every subsequent merge — the seal only makes _deletions_ conflict-free, never divergent content.
- **No links out to documents that are not in the checkout** — an instruction file (`AGENTS.md`, `.agents/rules/`, `.claude/skills/`) must never send an agent off to read a document it does not have. Do not paper over a `site`-only document with an absolute URL either: name the file in plain text if you have to refer to it at all, and put anything an agent actually needs into the instruction itself.
- **Paths that must not come back to `dev`/`main`** — under `docs/`, only `backlog/` and `research/` (the latter produced by a `deep_research` run); and none of `mkdocs.yml`, `tools/stage_site_docs.py`, `.github/workflows/site.yml`, which belong to `site` alone. `branch-guard` rejects both, so the partition cannot erode one file at a time.
- **The Markdown gate** — [wastech-mdlint.config.json](../../wastech-mdlint.config.json) is a shared file and therefore lives on `dev` like the rest of them, but it has to describe two corpora at once. It does so without a branch switch: a glob that selects nothing is an empty set, so `docs/**` means the queue here and the queue plus the guide there, and a rule scoped at `docs/*.md` is simply inert here. The handful of rules that cannot work that way — the ones asserting the derived documentation **exists** — live in an additive second config, `wastech-mdlint.docs.config.json`, present **only on `site`**. That file's absence everywhere else is what keeps it conflict-free: a merge cannot conflict on a path the incoming branch does not have. `python tools/mdlint.py` runs the shared config plus every overlay it finds, so the same command is correct on either branch shape and nothing has to know which branch it is on. The gate is a local pre-commit hook, not a CI job — the linter is not published yet — so it is only as strong as the hook being installed.

### What is machine-enforced

One workflow, and nothing else.

| Mechanism | Catches | Where |
| --- | --- | --- |
| `branch-guard` — job `branch invariants` | a PR sourced from `site`; a PR into `main` not sourced from `dev`/`release`; a PR into `site` not sourced from `main`/`docs/…`; derived docs or site machinery added on a PR into `dev`/`main` | [.github/workflows/branch-guard.yml](../../.github/workflows/branch-guard.yml) |

**Everything else rests on discipline, and you should know exactly how much.** This repository has no branch protection and no active ruleset: `main` and `site` accept a direct push, and the GitHub UI offers squash and rebase on every pull request. The merge _method_ is the one thing no workflow can ever check — it is chosen after every check has already reported — so "`dev → main` and `main → site` are merge commits" is enforced by the person clicking the button. `branch-guard` runs on pull requests only, so a local push bypasses it too.

To make it real, in repository settings → Rules: a ruleset on `main` and on `site` requiring a pull request and allowing **only** "Create a merge commit". Give it no bypass actors — the mistake it prevents is an admin clicking the wrong merge button.

### Command recipes

Copy-pasteable. `gh pr merge` flags are spelled out because the merge _method_ is load-bearing.

**Everyday change (the 95% case).**

```bash
git checkout dev && git pull
git checkout -b feat/<slug>
# … work …
ruff check . && ruff format --check . && mypy src && lint-imports && pytest
git push -u origin feat/<slug>
gh pr create --base dev --title "<subject>" --body "<what and why>"
gh pr merge --squash --delete-branch      # squash is correct here
```

**Integrate `dev` into `main`.** Dry-run first — it costs nothing and needs no worktree:

```bash
git fetch origin && git merge-tree --write-tree --messages origin/main origin/dev
gh pr create --base main --head dev --title "Integrate dev" --body "<what dev brings in>"
gh pr merge --merge                        # --merge = merge commit. NEVER --squash / --rebase
```

The first line of `merge-tree` output is the resulting tree; anything after it is a conflict. `git diff --stat origin/main <that-tree>` shows exactly what the merge will do.

**Publish the documentation (`main → site`).** The push is what republishes the site.

```bash
git fetch origin && git merge-tree --write-tree --messages origin/site origin/main
git diff --stat origin/site <that-tree>    # MUST show no deletions under docs/ and no loss of mkdocs.yml
git checkout site && git pull
git merge --no-ff --no-edit main
git push origin site
```

**Refresh the derived docs** (its own task on `site`, after a publish merge).

```bash
git checkout site && git pull
git log --merges --oneline -5                  # find the merge that came from main
git diff <merge-sha>^1 <merge-sha>             # the diff to reverse-engineer the docs from
git checkout -b docs/<slug>
# … regenerate the affected docs (see the /sync-docs skill, site scope) …
git push -u origin docs/<slug> && gh pr create --base site
```

**Cut a release.** Push the branch **before** the tag — `release.yml` refuses a tag that `origin/release` does not contain.

```bash
git checkout release && git pull
git merge --no-ff --no-edit main
git push origin release                    # first: the branch
git tag vX.Y.Z                             # aN / bN / rcN suffix ⇒ GitHub pre-release
git push origin vX.Y.Z                     # then: the tag
```

**Hotfix a published version.**

```bash
git checkout release && git pull
git checkout -b hotfix/<slug>
# … fix + the gate … then PR into release and tag:
gh pr create --base release && gh pr merge --squash --delete-branch
git checkout release && git pull && git tag vX.Y.Z+1 && git push origin vX.Y.Z+1
gh pr create --base main --head release --title "Propagate hotfix vX.Y.Z+1" --body "<the fix>"
gh pr merge --merge                        # merge commit, as for dev → main
```

**Port a fix out of `site` into `dev`** — cherry-pick, never a merge.

```bash
git checkout dev && git pull
git cherry-pick <sha>
```

A **code-only** commit cherry-picks cleanly. A commit that also touched a `site`-only document conflicts on that path as `DU` (deleted by us / modified by them) — the doc half does not belong on `dev`, so drop it and continue:

```bash
git rm <docs/path>          # resolves the DU by keeping the deletion
git cherry-pick --continue
```

### Everyday hygiene

- Atomic commits with an imperative subject (`Add provider health preflight`).
- **No agent attribution anywhere in a commit or PR.** A commit message ends with its own body — never a `Co-Authored-By: Claude …` / `Co-authored-by: Codex …` trailer, never a `🤖 Generated with …` line, and no other tool-authorship footer; the same holds for PR titles and bodies. This overrides any default an agent harness injects (Claude Code adds such a trailer unless told otherwise — here it is told otherwise). The author of a commit is the repository's configured git identity, which already records who ran the work; a second synthetic author only pollutes `git log`, `git shortlog`, and the contributor graph. If a trailer slips in, strip it before pushing (`git commit --amend` on an unpushed commit; otherwise say so in the PR rather than force-pushing a shared branch).
- Before committing, run the gate: `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, `pytest` (CI also runs `interrogate` / `vulture` / `deptry`). Install the local mirror once with `pre-commit install && pre-commit install --hook-type pre-push`.
- Keep docs in sync **in the same change** as the code, scoped to the branch you are on: on the code branches that is `.agents/rules/`, `README.md`, `docs/backlog/`, and the shipped operator-facing docs under `src/wastech_orchestrator/packaged/`; the derived `docs/` refresh happens on `site`. Do not add anything under `docs/` here outside `backlog/` (and `docs/research/`, which a `deep_research` run produces) — `branch-guard` rejects it.
- Never link a `site`-only document from a shared file — not relatively, not by absolute URL. Never edit a shared file (`AGENTS.md`, `.agents/rules/`, `.claude/skills/`) on `main` or `site`: those edits flow through `dev`, otherwise the branches diverge in content and conflict on every merge.
- Do not commit: `config.yaml`, `.venv/`, `workspace/`, `logs/`, `*.db`, secrets, or the transient task folders (see `.gitignore`).
- Gitignored `.md` files (e.g. under `.archive/`, or a target repo's `.worc/`) are not project documentation — do not treat them as current, cite them, or link to them. Check `git ls-files` / `git check-ignore -v` first.

## B. How the orchestrator manages a target repository

This is a product invariant (see [architecture.md](architecture.md)).

- **Only the orchestrator (Git Manager) commits, pushes, and opens PRs** — never the agent provider. That is a de-jure mandate: no node is given one and no mechanism expects the agent to publish. Mechanical impossibility holds only where a sandbox exists, and only for the local half (`.git` and `.worc` are immutable); the remote half is **reported** by detection on our `origin` and is not held there or anywhere else (see [architecture.md](architecture.md)).
- Default task branch: `repo.branch_prefix/<task-id>-<slug>` (`worc/…` by default); a validated task `branch_name` may override it.
- Branch setup: `git fetch` → checkout `base_branch` → `pull` → create the task branch.
- A direct push to `base_branch` is forbidden; the result always goes through a PR, whose body is the task summary.
- Publishing happens only from the `ready_to_publish` status, when checks succeed and there are no blocking findings.
- Idempotent: a re-run never creates a second commit / push / PR.
- Stage only the agent's intended code paths; `tasks/`/`logs/`/`workspace/`/`.worc/`/`.worc-io/` are always excluded from code commits, and never `git add .` / `git add -A` for a code commit. Target-repo hooks/filters must not run inside an orchestrator git command. A change in git control state around an attempt is **reported and never parked**, on every node class: the drift is a warning plus a ⚠️ trace naming the aspect, and the run continues. The reason is that the fingerprint sees only "the state moved", never whose hand moved it, and in practice what it catches is the operator committing in their own repository — while parking throws away a finished node's work. The fingerprint itself is never dropped, only its consequence: it is the one signal by which an operator learns a clone has to be discarded. What holds a commit made inside a run is the dangerous-diff gate, which measures from the orchestrator's own last commit and so still asks about it.
- The git footprint mode is configurable (in-repo audit-commit by default, local-exclude, or external/zero-footprint); orchestration and task artifacts never enter a code commit.
- A decomposed task makes one local commit per subtask on a single branch, but still one PR per parent task.
- After a task reaches a terminal status, the Git Manager checks out and refreshes `base_branch` before the next task can start; any ambiguous branch state stops in `manual_action_required` with no automatic actions.
- Auto mode (`orchestrator.auto_mode.enabled`) is opt-in and only starts the next task after a clean return to `base_branch`.
