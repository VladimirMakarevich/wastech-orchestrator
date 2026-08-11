# Git workflow

There are two levels of git here: (A) how the orchestrator **itself** is developed; (B) how the orchestrator works with **target** repositories. Do not conflate them.

## A. Developing the orchestrator itself

### Three long-lived branches

`feat/… → dev → main → release`.

- **`dev`** — the working branch. Cut every `feat/…`, `fix/…`, `chore/…` branch off `dev` and merge it back into `dev` (squash is fine, and preferred). It carries **no derived documentation**: under `docs/` it holds only `backlog/` (the task queue work is implemented from). Never push to `dev` directly — land changes through a PR, merged only after checks pass.
- **`main`** — integration plus documentation. It receives `dev` through merge commits and is the **only** branch where the derived `docs/` tree is written; that refresh is its own task (branch `docs/…` off `main`, PR back into `main`), driven by the merged `dev` diff. Never commit code directly on `main`.
- **`release`** — published stable versions. `main → release` by merge commit, then `git tag vX.Y.Z` here; the package release and the documentation site publish from this branch.

### Two hard rules

1. **Never merge `main → dev`** (nor `release → dev`). It puts the derived documentation back on `dev` and the whole arrangement has to be rebuilt. If a fix ever lands on `main` or `release` first, port it to `dev` with `git cherry-pick`, never with a merge. A CI guard rejects any PR into `dev` whose source is `main` or `release`.
2. **`dev → main` must be a real merge commit.** A squash merge creates no merge commit, so the merge base never advances and every later merge re-proposes deleting the documentation; rebase-merge is likewise wrong for a long-lived branch pair. On GitHub choose "Create a merge commit" — a ruleset on `main` allows no other method, so the squash and rebase buttons are absent by design.

Both rules exist because the two branches diverge by one recorded seal commit (`git merge -s ours dev` on `main`); anything that resets the merge base or copies docs back into `dev` destroys it. The seal is already in history — `dev` is an ancestor of `main`, so an ordinary `dev → main` merge proposes no deletions. Do not create a second seal. The design record behind the model — the migration that produced the seal and the merge matrix verified against it — is [BRANCHING_MODEL.md](../../BRANCHING_MODEL.md); read it as history, not as an operating manual, and never re-run its migration commands.

Neither rule is self-announcing when broken, which is why both are machine-enforced. Breaking rule 2 is worse than it looks: the squashed content still lands, the merge is clean, and the only symptom is that `dev` quietly falls behind `main` while the merge base stays put — every later `dev → main` merge then re-proposes the same content forever. If it happens anyway, the repair is to land the same change on `dev` through its normal PR and then merge `dev → main` with a merge commit; verify with `git merge-tree --write-tree origin/main origin/dev` first, and expect `git diff origin/main <result-tree>` to be empty (that merge is a content no-op whose only job is to re-advance the merge base).

### Where the branches touch

Every legal transition, and the two illegal ones. The mechanism column is the part that matters: picking the wrong one is how the model breaks.

| From → To | Mechanism | Why this one |
| --- | --- | --- |
| `feat/…` → `dev` | **squash** merge (PR) | Keeps `dev` history one commit per change; the merge base of `dev`/`main` is unaffected. |
| `dev` → `main` | **merge commit**, never squash/rebase | Advances the merge base. A squash leaves it behind and every later merge re-proposes deleting `docs/`. |
| `docs/…` → `main` | squash or merge (PR) | The docs refresh is ordinary work on `main`; it never touches `dev`. |
| `main` → `release` | merge commit | Both branches carry docs, so this is an ordinary merge with no special handling. |
| `hotfix/…` → `release` | squash or merge (PR) | Published-version fix; tag afterwards. |
| `release` → `main` | merge commit | Safe — both have docs. This is how a hotfix gets back into integration. |
| `main`/`release` → `dev` | **FORBIDDEN** — use `git cherry-pick` | A merge restores the derived docs on `dev`. The CI guard rejects it on PRs; a local push would not be caught, so do not do it. |
| `feat/…`/`fix/…`/`chore/…` → `main` | **FORBIDDEN** — retarget at `dev` | Bypasses `dev`, so `dev` falls behind `main`. It merges cleanly and raises no conflict, so only the `main-guard` check catches it. |

Three more contact surfaces that are not merges:

- **Shared files** — `AGENTS.md`, `CLAUDE.md`, `README.md`, `.agents/rules/`, `.claude/`, everything under `src/`, and `docs/backlog/` exist on **both** branches. Edit them **only on `dev`**. Editing one on `main` makes the content diverge, and a divergent shared file conflicts on every subsequent merge — the seal only makes _deletions_ conflict-free, never divergent content.
- **Links to `main`-only documents** — from any shared file, link them by absolute URL (`https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/<file>`), never by a relative path: the relative path is dangling on `dev`.
- **New paths under `docs/` on `dev`** — only `backlog/` and `research/` (the latter produced by a `deep_research` run). A CI guard rejects anything else, so the partition cannot erode one file at a time.
- **The Markdown gate** — [wastech-mdlint.config.json](../../wastech-mdlint.config.json) is a shared file and therefore lives on `dev` like the rest of them, but it has to describe two corpora at once. It does so without a branch switch: a glob that selects nothing is an empty set, so `docs/**` means the queue here and the queue plus the guide there, and a rule scoped at `docs/*.md` is simply inert on `dev`. The handful of rules that cannot work that way — the ones asserting the derived documentation **exists** — live in an additive second config, `wastech-mdlint.docs.config.json`, present **only on `main`**. That file's absence from `dev` is what keeps it conflict-free: a merge cannot conflict on a path the incoming branch does not have. `python tools/mdlint.py` runs the shared config plus every overlay it finds, so the same command is correct on either branch, and neither CI nor the hook needs to know which branch it is on. The content to create on `main` is in [docs/backlog/mdlint-main-overlay.md](../../docs/backlog/mdlint-main-overlay.md).

### What is machine-enforced

Everything above that a mistake would break silently is checked. Know which half catches what, because the two halves fail differently.

| Mechanism | Catches | Where |
| --- | --- | --- |
| `dev-guard` — job `dev branch invariants` | a PR into `dev` sourced from `main`/`release`; any new path under `docs/` outside `backlog/`/`research/` | [.github/workflows/dev-guard.yml](../../.github/workflows/dev-guard.yml) |
| `main-guard` — job `main branch invariants` | a PR into `main` sourced from anything but `dev`, `release`, or `docs/…` | [.github/workflows/main-guard.yml](../../.github/workflows/main-guard.yml) |
| Ruleset on `main` | the merge _method_ — only "Create a merge commit" is offered; `main` is pull-request-only | repository settings → Rules |
| `ci` — job `markdown context` | a relative link from a shared document to a file only the documentation branch carries (it dangles on `dev`), a broken anchor, an unreachable document, and any Markdown that outgrew its size budget | [.github/workflows/ci.yml](../../.github/workflows/ci.yml) |

The ruleset is the one that cannot be a workflow: the merge method is chosen after every check has reported, so no pull-request check can observe it. Conversely the guards cannot be settings — GitHub has no notion of a legal source branch.

Two limits worth knowing. A **local push** bypasses both guards (they run on pull requests), which is why `main` is pull-request-only in the ruleset — the ruleset does cover pushes. And the ruleset has **no bypass actors on purpose**: the mistake it prevents is an admin clicking the wrong merge button, so an admin exemption would defeat it. If you genuinely need a direct push to `main`, set that ruleset to `Disabled` in repository settings, do the push, and switch it back — a deliberate two-step act, not a silent default.

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

**Integrate `dev` into `main`.** The merge method is the one thing you cannot get wrong.

```bash
git checkout dev && git pull
gh pr create --base main --head dev --title "Integrate dev" --body "<what dev brings in>"
gh pr merge --merge                        # --merge = merge commit. NEVER --squash / --rebase
```

`main` is pull-request-only, so there is no local variant — but do run the safety check the local recipe used to carry, before you merge rather than after. It costs nothing and needs no worktree:

```bash
git fetch origin && git merge-tree --write-tree --messages origin/main origin/dev
```

The first line of output is the resulting tree; anything after it is a conflict. `git diff --stat origin/main <that-tree>` shows exactly what the merge will do to `main` — no doc deletions may appear, and if it is empty the merge is a pure merge-base advance.

**Refresh the derived docs on `main`** (its own task, after an integration merge).

```bash
git checkout main && git pull
git log --merges --oneline -5                  # find the integration merge commit
git show --stat <merge-sha>                    # what dev brought in
git diff <merge-sha>^1 <merge-sha>             # the diff to reverse-engineer the docs from
git checkout -b docs/<slug>
# … regenerate the affected docs (see the /sync-docs skill, main scope) …
git push -u origin docs/<slug> && gh pr create --base main
```

**Cut a release.** Push the branch **before** the tag — `release.yml` refuses a tag that `origin/release` does not contain.

```bash
git checkout release && git pull
git merge --no-ff --no-edit main
git push origin release                    # first: the branch
git tag vX.Y.Z                             # aN / bN / rcN suffix ⇒ GitHub pre-release
git push origin vX.Y.Z                     # then: the tag
```

The tag push runs `release.yml` (full quality gate → sdist + wheel → GitHub release, version derived by `hatch-vcs`) and the Pages deploy in `site.yml`.

**Hotfix a published version.**

```bash
git checkout release && git pull
git checkout -b hotfix/<slug>
# … fix + the gate … then PR into release and tag:
gh pr create --base release && gh pr merge --squash --delete-branch
git checkout release && git pull && git tag vX.Y.Z+1 && git push origin vX.Y.Z+1
# then propagate release → main, which is pull-request-only:
gh pr create --base main --head release --title "Propagate hotfix vX.Y.Z+1" --body "<the fix>"
gh pr merge --merge                        # merge commit, as for dev → main
```

**Port a fix from `main`/`release` into `dev`** — cherry-pick, never a merge.

```bash
git checkout dev && git pull
git cherry-pick <sha>
```

A **code-only** commit cherry-picks cleanly. A commit that also touched a `main`-only document conflicts on that path as `DU` (deleted by us / modified by them) — the doc half does not belong on `dev`, so drop it and continue:

```bash
git rm <docs/path>          # resolves the DU by keeping the deletion
git cherry-pick --continue
```

**If `main` or `release` was merged into `dev` anyway.** The derived docs are back on `dev` and no ordinary revert repairs the merge base. Stop, tell the maintainer: the fix is to re-cut `dev` from `main`, redo the removal commit, and record a fresh seal — i.e. redo the migration. This is why rule 1 is absolute.

### Everyday hygiene

- Atomic commits with an imperative subject (`Add provider health preflight`).
- Before committing, run the gate: `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, `pytest` (CI also runs `interrogate` / `vulture` / `deptry`). Install the local mirror once with `pre-commit install && pre-commit install --hook-type pre-push`.
- Keep docs in sync **in the same change** as the code, scoped to the branch you are on: on `dev` that is `.agents/rules/`, `README.md`, `docs/backlog/`, and the shipped operator-facing docs under `src/wastech_orchestrator/packaged/`; the derived `docs/` refresh happens on `main`. Do not add anything under `docs/` on `dev` outside `backlog/` (and `docs/research/`, which a `deep_research` run produces) — a CI guard rejects it.
- Documents that live on `main` only are linked by absolute URL (`https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/<file>`) from any file shared with `dev`, so the link resolves from either branch. Never edit a shared file (`AGENTS.md`, `.agents/rules/`, `.claude/skills/`) on `main`: those edits flow through `dev`, otherwise the two branches diverge in content and conflict on every merge.
- Do not commit: `config.yaml`, `.venv/`, `workspace/`, `logs/`, `*.db`, secrets, or the transient task folders (see `.gitignore`).
- Gitignored `.md` files (e.g. under `.archive/`, or a target repo's `.worc/`) are not project documentation — do not treat them as current, cite them, or link to them. Check `git ls-files` / `git check-ignore -v` first.

## B. How the orchestrator manages a target repository

This is a product invariant (see [architecture.md](architecture.md)).

- **Only the orchestrator (Git Manager) commits, pushes, and opens PRs** — never the agent provider.
- Default task branch: `repo.branch_prefix/<task-id>-<slug>` (`worc/…` by default); a validated task `branch_name` may override it.
- Branch setup: `git fetch` → checkout `base_branch` → `pull` → create the task branch.
- A direct push to `base_branch` is forbidden; the result always goes through a PR, whose body is the task summary.
- Publishing happens only from the `ready_to_publish` status, when checks succeed and there are no blocking findings.
- Idempotent: a re-run never creates a second commit / push / PR.
- Stage only the agent's intended code paths; `tasks/`/`logs/`/`workspace/`/`.worc/`/`.worc-io/` are always excluded from code commits, and never `git add .` / `git add -A` for a code commit. Target-repo hooks/filters must not run inside an orchestrator git command, and provider tampering with git control state stops the run in `manual_action_required` — with one deliberate exception: a `read-only` node holding the git-evidence grant never parks the task (operator decision, 2026-07-26), so its drift is reported as a warning plus a ⚠️ trace naming the aspect and the run continues. The fingerprint is never dropped, only its consequence changes; do not extend that exception to any other node class.
- The git footprint mode is configurable (in-repo audit-commit by default, local-exclude, or external/zero-footprint); orchestration and task artifacts never enter a code commit.
- A decomposed task makes one local commit per subtask on a single branch, but still one PR per parent task.
- After a task reaches a terminal status, the Git Manager checks out and refreshes `base_branch` before the next task can start; any ambiguous branch state stops in `manual_action_required` with no automatic actions.
- Auto mode (`orchestrator.auto_mode.enabled`) is opt-in and only starts the next task after a clean return to `base_branch`.
