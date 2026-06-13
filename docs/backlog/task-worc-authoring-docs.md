---
id: worc-authoring-docs
title: "Ship agent-facing task-authoring docs (docs/worc) and copy them on install"
refined: false
decompose: false
contacts:
  - "@Vladimir Makarevich"
---

## Description

Make it possible for an **AI agent** to reliably author high-quality task files for this orchestrator
from a single, self-contained, local source — not by reading the whole `docs/` tree and the spec.

Today the knowledge needed to write a good task is spread across
[docs/task-authoring.md](../task-authoring.md), [docs/configuration.md](../configuration.md),
[docs/operations.md](../operations.md), and
[docs/implementation_stages/00_orchestrator_final_plan.md](../implementation_stages/00_orchestrator_final_plan.md).
That is too much, and too implementation-oriented, for an agent to consume reliably when its only job
is "write a correct task manifest for this orchestrator." We want a compact, rule-first, copy-paste
oriented guide that an agent can be pointed at to produce valid, well-scoped tasks every time — and we
want that guide to land next to the operator's `config.yaml` at install time, so it travels with each
orchestrator deployment.

There are two deliverables:

1. **A new `docs/worc/` directory** (the authored source of truth) holding agent-facing
   documentation. It must cover, concisely and rule-first:
   - **What the orchestrator is and the task contract** — the front-matter fields, the required
     fields, the body sections (`## Description` / `## Acceptance criteria` / `## Constraints`), and
     the validation gate's hard rules (so an agent never emits a task that is rejected).
   - **When to use what** — a short decision guide: `run` vs `watch`; when to set `decompose`; when to
     skip stages (`stages.<stage>.enabled: false` for `planning`/`testing`/`review`/`fixing`/`summary`,
     and the `review` gate via `agents.allow_review_skip`); per-stage `model`/`reasoning` overrides;
     `auto_merge` (and its danger); the `refined` flag; footprint modes; Telegram HITL/contacts.
   - **Best practices for writing tasks** — testable vs vague acceptance criteria, scoping a task to
     one coherent change, stating constraints (do-not-touch areas, no new deps), and the project's own
     working rules an authored task should respect (minimal focused changes; tests updated with
     behavior; docs/CHANGELOG/follow-ups kept in sync; canonical stage/provider names; the security
     invariants in [CLAUDE.md](../../CLAUDE.md)).
   - **Ready-to-adapt examples** — at least one minimal task and one richer task an agent can copy and
     fill in, consistent with the packaged `templates/task.md` shape.

2. **Install-time copy**: `docs/worc/` is copied to the orchestrator's install location, **next to
   `config.yaml`**, so the guide is available locally per deployment (and to any agent run there):
   - `init` writes it next to the generated `config.yaml` (e.g. a `worc/` folder beside it);
   - `install` writes it into the control workspace next to that workspace's `config.yaml`;
   - the files must be available from an **installed wheel** (shipped as package data), not only from a
     source checkout.

3. **Keep the installed copy current on upgrade.** Because the `worc/` docs ship with the package, an
   upgraded orchestrator carries newer docs than an already-installed copy. Provide a way to refresh
   the installed `worc/` (next to `config.yaml`) to the current packaged version:
   - a **dedicated command** to update the docs (e.g. `upgrade-docs`), **and/or** fold the refresh
     into the existing `upgrade-config` flow so one post-upgrade step brings both config and docs
     current (mirror what we did for config in [docs/operations.md](../operations.md#upgrading-the-orchestrator));
   - unlike `config.yaml`, the `worc/` docs are **generated content with no operator edits to
     preserve**, so the refresh is a straight overwrite-with-the-packaged-version (idempotent — a
     re-run is a no-op when already current; `--dry-run` previews what would change);
   - ideally the same step runs (or is recommended) right after `pipx upgrade`, the same way config is
     refreshed; if a single umbrella `upgrade` command (config + docs) is deferred, record it in
     follow-ups.

The goal: an operator (or an automation) can hand an AI agent the local `worc/` folder and say "write
a task for this orchestrator," and get back a manifest that passes the validation gate and follows the
project's conventions.

## Acceptance criteria

- [ ] `docs/worc/` exists and contains at least: an entry-point `README.md` (index + "you are writing
      a task for wastech-orchestrator" framing), a task-authoring best-practices doc, a "when to use
      what" decision guide, and at least two example task files (minimal + richer).
- [ ] The content is accurate against the current code/docs: every front-matter field, skippable
      stage, and rule it states matches [docs/task-authoring.md](../task-authoring.md) and
      [docs/configuration.md](../configuration.md); the example tasks pass the validation gate
      (verified by a test that loads them through `ValidationGate`).
- [ ] `wastech-orchestrator init <dir>` copies the `worc/` docs next to the generated `config.yaml`;
      the copy is idempotent (a re-run skips existing files like other `init` output), `--dry-run`
      lists them without writing, and `--force` re-copies them.
- [ ] `wastech-orchestrator install <repo>` copies the `worc/` docs into the control workspace next to
      its `config.yaml`; `install --reconfigure` refreshes them.
- [ ] The `worc/` docs are resolvable from an installed wheel via `importlib.resources` (a test
      mirrors the existing packaged-template test pattern), not just from the source tree.
- [ ] A command refreshes the installed `worc/` docs in place to the current packaged version
      (overwriting a stale installed copy), is idempotent (already-current → no-op), and supports
      `--dry-run`. Whether it is a new `upgrade-docs` command or part of `upgrade-config`, the choice
      is documented and the command is discoverable from `--help`.
- [ ] The docs refresh never modifies `config.yaml` or any operator-edited file, targets the same
      location as the install-time copy (respecting the footprint rules), and exits non-zero with a
      clear hint when no install location can be resolved (consistent with `upgrade-config`).
- [ ] Tests cover: the `init` and `install` copy behavior, packaged-data availability, the docs
      refresh command (stale → updated, already-current → no-op, `--dry-run` writes nothing), and that
      the shipped example tasks validate; the suite stays green (`ruff`, `ruff format --check`,
      `mypy`, `pytest`).
- [ ] [docs/operations.md](../operations.md) and the README command notes document the new install
      output; `CHANGELOG.md` `[Unreleased]` and [docs/backlog/follow_ups.md](follow_ups.md) are updated
      in the same change (the Stop docs-sync gate enforces this).

## Constraints

- **Reuse the existing install machinery** — `_templates_root` / `_iter_template_files` /
  `cmd_init` / `cmd_install` / `_install_atomic_write` and the `install` registry binding in
  `src/wastech_orchestrator/cli.py`. Do not invent a second copy/scaffold mechanism.
- **Reuse the `upgrade-config` pattern for the refresh** — `config/upgrade.py` +
  `cli.cmd_upgrade_config` + `resolve_config_path` already implement "find the install location next
  to `config.yaml`, back up, write atomically, idempotent, `--dry-run`, fail-closed when none found".
  Model the docs refresh on it (and target the same location). Because the docs are generated, the
  refresh **overwrites** rather than merges — there is no operator content to preserve.
- **Respect the git-footprint invariants (§21).** Under the in-repo footprint the copied `worc/`
  files must not pollute the operator's `git status` (handle them like the other runtime artifacts —
  e.g. via `append_runtime_excludes`, or place them in the control workspace), and must never enter a
  code commit. Decide deliberately where the copy lands per footprint mode and document it.
- **Single source of truth, no silent drift.** The authored files live in `docs/worc/`; if a packaged
  copy under the package data tree is needed for wheel install, keep them in sync with a test (mirror
  the existing repo-root-vs-packaged `config.example.yaml` sync test) rather than hand-maintaining two
  diverging copies. Prefer distilled, agent-oriented content with pointers over duplicating large
  blocks of existing docs verbatim.
- **Docs/scaffolding only — no pipeline behavior change.** Do not touch the state machine, providers,
  routing, or the security policy; this adds documentation and an install-copy step. Honor the hard
  invariants in [CLAUDE.md](../../CLAUDE.md) (core knows no CLI syntax; only the orchestrator
  commits/pushes; no secrets in artifacts; no weakening of the security policy).
- No new heavy runtime dependencies; keep the change minimal and in the style of the surrounding code.
