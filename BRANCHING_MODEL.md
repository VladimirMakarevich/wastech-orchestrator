# Branch model (`dev` / `main` / `release` / `site`) — design record

Status: **in effect** (2026-09-01) Owner: Vladimir Makarevich

This is the design record: why the model has the shape it has, and which properties must keep holding. **For day-to-day work follow [.agents/rules/git-workflow.md](.agents/rules/git-workflow.md) §A instead** — the branch table, the hard rule, and the command recipes live there. Nothing here is a command to re-run; in particular **do not create a second seal**.

## The problem it solves

Descriptive documentation is expensive to keep near the code. It is reconstructed from the code rather than written alongside it, it is large, and an agent that can see a stale architecture document will read it as truth or "helpfully" rewrite it. So it is kept off the branches where development happens — but keeping it off them is not free, and the cost is what this document records.

## The four branches

`feat/… → dev → main → release` carries the code; `main → site` carries it into the documentation.

| Branch | Holds | Receives |
| --- | --- | --- |
| `dev` | sources of truth and live working state: the code, the rules, and `docs/backlog/` (the work queue) | `feat/…` (squash) |
| `main` | the same tree as `dev` — integration only | `dev` (merge commit), `release` (merge commit) |
| `release` | the same tree as `dev`; version tags are cut here | `main` (merge commit), `hotfix/…` |
| `site` | everything `main` has, **plus** the derived `docs/` tree and the site machinery (`mkdocs.yml`, `tools/stage_site_docs.py`, the `site` workflow, the mdlint docs overlay) | `main` (merge commit), `docs/…` (PR) |

The split rule that produced this partition, and that decides any future file:

> **The code branches hold sources of truth and live working state. `site` holds derived description.**

Everything an agent needs in order to _do_ work stays on `dev`. Everything that only _describes_ the result — and can therefore be reconstructed from the code — lives on `site` alone. `docs/backlog/` is the one apparent exception and is not one: it is the queue that work is implemented from, an input rather than a description. Under `docs/`, the code branches keep `backlog/` and `research/` (the latter produced by a `deep_research` run) and nothing else — one mechanically checkable rule, which is what `branch-guard` enforces.

Three things can never leave the code branches, whatever the split rule says: `README.md` (declared by `pyproject.toml`, so `pip install -e .` fails without it), the Markdown under `src/wastech_orchestrator/packaged/` (runtime assets — flows, role prompts, the shipped operator guide — not documentation), and `AGENTS.md` / `CLAUDE.md` / `.agents/rules/` / `.claude/skills/` (inputs that govern how agents work).

## Why the seal exists

Git merges three-way, and it reads a one-sided deletion as a decision. Branch `site` off `main`, delete `docs/` on `main`, then merge `main → site`:

- merge base — documents present
- `main` side — deleted
- `site` side — untouched

Git applies the deletion, with `exit=0`, no conflict and no warning. The documentation is simply gone from the branch that owns it, and only a diff review would catch it. Worse, once `site` starts editing a document that `main` deleted, every later merge raises a `modify/delete` conflict on that path, forever.

So the divergence has to be recorded in history once, deliberately. `git merge -s ours main`, run on `site`, records both parents while keeping `site`'s tree byte-for-byte unchanged. From then on the merge base of the two branches is already a docs-less tree, so on every later merge the documentation reads as _added on the `site` side_ — and an addition on one side with no counterpart on the other never conflicts.

The seal is a one-time operation. It must be done **locally with the real `git` CLI and pushed**: a GitHub pull request cannot perform an `-s ours` merge. It is already in history.

## The properties that must keep holding

| Property | What breaks it |
| --- | --- |
| A `dev → main` merge changes only code | nothing — the two trees have the same shape, which is the point of moving the docs off `main` |
| A `main → site` merge never proposes deleting `docs/` | squashing that merge: the merge base never advances, so every later merge re-proposes the whole deletion |
| A documentation edit on `site` survives the next merge from `main` | editing a **shared** file on `site`. The seal makes _deletions_ conflict-free, never divergent content — shared files are edited on `dev` only |
| Nothing on `site` reaches the code branches | merging `site` into anything. Port with `git cherry-pick`; a commit that also touched a `site`-only document conflicts as `DU`, resolved by dropping the doc half |
| The partition does not erode | adding a derived doc or the site machinery back onto `dev`/`main`, one file at a time — `branch-guard` rejects both |

## What is deliberately not enforced

`branch-guard` covers every rule above that a pull request can express. It cannot cover the merge _method_: GitHub picks that after all checks have reported. And it runs on pull requests, so a direct push bypasses it — which matters here, because this repository has **no branch protection and no active ruleset at all**. `main` and `site` accept a push, and the UI offers squash and rebase on every PR.

That is a deliberate current state, not an oversight, and §A says so plainly rather than claiming an enforcement that does not exist. The ruleset that would close it (pull-request-only on `main` and `site`, merge commit as the only method, no bypass actors) is a repository setting, described in §A.

## History

The first version of this model, in effect from 2026-07-25, had three branches and put the documentation on `main`. That made `main` both the integration branch and the documentation branch, so the seal sat on `dev → main` — the busiest edge in the repository — and every rule about it had to be obeyed on every integration. Moving the documentation to `site` on 2026-09-01 left `dev`, `main`, and `release` shape-identical, so `dev → main` became an ordinary merge and the seal moved to `main → site`, which is touched only when documentation is published. The mechanism is unchanged; only the edge it sits on moved.
