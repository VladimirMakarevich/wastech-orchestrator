# Backlog: Install/refresh templates into an existing install (`install-templates` command)

Status: **implemented** (2026-06-14) — `worc install-templates` (add-missing-only, `--force`,
`--dry-run`). See [CHANGELOG](../../CHANGELOG.md) `[Unreleased]`, [docs/operations.md](../operations.md)
"Upgrading the orchestrator", the spec [§20.5](../implementation_stages/00_orchestrator_final_plan.md),
and the tests in `tests/test_cli_install_templates.py`. The sections below are the design record; the
**Outcome** box captures what shipped and the decisions taken.
Date: 2026-06-14
Owner: Vladimir Makarevich

## Outcome (as implemented)

> **Superseded in part by schema v6** (prompt-templates simplification — see
> [../backlog/task_prompt_templates_simplification.md](../backlog/task_prompt_templates_simplification.md)).
> The delivered tree is now **prompts-only**: `skills/`, `AGENTS.md`, `CLAUDE.md`, and `task.md` are no
> longer shipped, which **resolves** the "Scope of the tree" question below (prompts-only; the deferred
> `--only prompts|skills` selector is moot). `prompts.templates_dir` now resolves **relative to
> `config.yaml`** (closes the §6/§11 deferral), and `prompts.overrides`/`prompts.strict` are removed —
> a delivered `prompts/<stage>.md` is auto-detected by file presence, so "no config mutation" below
> means there is nothing to opt into. The command behavior (add-missing-only, parity with `init`,
> fail-closed resolution) is unchanged.

A separate, idempotent `worc install-templates` that delivers the packaged `templates/` tree beside
the resolved `config.yaml`, **add-missing-only**.

- **Decisions taken.** (1) Name kept = `install-templates` (operator-confirmed). (2) Scope = the whole
  `templates/` tree minus `config.example.yaml`; no `--only prompts|skills` selector (deferred). (3)
  Location resolved via `resolve_config_path` (explicit `--config` → `./config.yaml` → repo→config
  registry binding) and written beside `config.yaml`; fail-closed (exit 2) with the same actionable
  hint as `upgrade-config`/`upgrade-docs`. (4) Add-missing-only: absent → write, present → skip,
  present + `--force` → overwrite; an all-present run is a no-op ("already complete"). (5) **No orphan
  removal** (the deliberate asymmetry with `upgrade-docs`, since templates are operator-editable). (6)
  **No config mutation** — never writes `prompts.overrides` or touches `config.yaml`.
- **Shared helper (no drift).** Extracted `cli._copy_templates_tree(dest_root, *, overwrite, dry)`
  mirroring the existing `_copy_worc_docs`; **both** `cmd_init` (its template-tree step) and the new
  `cmd_install_templates` call it. A parity test (`test_init_and_install_templates_produce_same_tree`)
  pins that `init` and `install-templates` produce a byte-identical `templates/` tree. The helper uses
  `Path.write_bytes` (matching `_copy_worc_docs`, keeping `cmd_init` byte-identical), not the atomic
  writer `upgrade-docs` uses.
- **Code.** `cli.cmd_install_templates`; `cli._copy_templates_tree`; the `install-templates` subparser
  (`--force`/`--dry-run`) + `main` dispatch; the `cmd_init` step-3 refactor onto the shared helper.
- **Docs.** Spec §20.5 (and a §20.2 fix: the stale `plan.md`/`implement.md`/`fix.md` prompt names →
  `planning.md`/`implementation.md`/`fixing.md`, plus the real `skills/` subtree); CHANGELOG; README
  command list; operations.md "Upgrading the orchestrator" (incl. the external-footprint
  `templates_dir` caveat).
- **Deferred (stay `candidate`).** Config-relative `prompts.templates_dir` resolution (§6/§11); the
  umbrella `upgrade` that folds `upgrade-config` + `upgrade-docs` + `install-templates` (§8); the
  `--only prompts|skills` selector (§11).

The canonical contract is
[00_orchestrator_final_plan.md](../implementation_stages/00_orchestrator_final_plan.md). This document
must not override the hard invariants in [../../CLAUDE.md](../../CLAUDE.md),
[../../AGENTS.md](../../AGENTS.md), or [../rules/](../rules/) — in particular: the security policy
cannot be weakened through templates, only the orchestrator commits/pushes/PRs, and templates are
**prompt text only** (they cannot change provider, `extra_args`, sandbox/approvals, denied
commands/reads, or env — see [config.example.yaml](../../src/wastech_orchestrator/templates/config.example.yaml)
`prompts:` block).

## 1. Background

The packaged `templates/` tree (`prompts/<stage>.md`, `skills/`, `AGENTS.md`, `CLAUDE.md`, `task.md`)
is delivered to an operator's project **only by `init`**
([`cli.cmd_init`](../../src/wastech_orchestrator/cli.py)), which copies the whole tree into the target
directory at scaffold time (skip-existing, with `--force` to overwrite). After that first copy there
is **no command to (re)deliver the templates**:

- **`install` (the wizard, external workspace,
  [`cli.cmd_install`](../../src/wastech_orchestrator/cli.py))** writes `config.yaml` and the `worc/`
  docs into the sibling control workspace but **never copies the `templates/` tree at all**. An
  install-based operator therefore has no `templates/` to customize.
- **`upgrade-config`** refreshes `config.yaml` keys (add-missing-only); **`upgrade-docs`** refreshes
  the installed `worc/` docs (overwrite-with-packaged). Neither touches `templates/`, so the prompt
  and skill templates an operator copied at `init` time **silently drift** from the packaged version
  across orchestrator upgrades, and an install-based setup never receives them in the first place.

A relevant architectural fact: prompt templates are read at runtime **only as opt-in overrides**.
The runtime always loads the packaged default per stage
([`core/prompts.py`](../../src/wastech_orchestrator/core/prompts.py) `_packaged_default`); the
installed `templates/prompts/<stage>.md` is consulted **only** when the operator both edits it and
lists it under `prompts.overrides` (empty `{}` by default). So the installed templates are
*editing/reference material*, not live config — which shapes the design below (see §4 and §7).

## 2. Goal

A first-class, **separate**, idempotent command that delivers the packaged `templates/` tree into an
existing install **add-missing-only**: if every template already exists, do nothing; if the whole
tree is absent, write it; if a single file is missing, write **only that one** and skip the files
that already exist. Existing operator-edited templates are **never** clobbered by default — this is
the deliberate difference from `upgrade-docs` (which overwrites the generated `worc/` docs because
they carry no operator edits).

Working name: **`install-templates`** (alternatives in §11). Distinct from `init` (which scaffolds the
*whole* project once) and from `upgrade-docs` (overwrite of generated docs).

## 3. Non-goals and limits

- Not a re-scaffold of the project layout, `config.yaml`, runtime dirs, or git excludes — that is
  `init`. This command touches **only** the `templates/` tree.
- Not an overwrite of operator-edited templates by default (that is the whole point of add-missing).
  A separate, explicit `--force` (overwrite) may exist as an escape hatch (§7), mirroring `init --force`.
- Does **not** auto-activate templates by writing `prompts.overrides` — that is a near-no-op by design
  and a foot-gun (§7). Config materialization stays the job of `upgrade-config`.
- Does not weaken the security policy: templates remain prompt-text-only; this command copies files,
  it does not change how they are read or trusted.

## 4. Add-missing-only semantics (the core behavior the operator asked for)

For each file in the packaged `templates/` tree, compared against the install's `templates/` dir:

| Install state | Action |
|---|---|
| File absent | **write** the packaged file |
| File present | **skip** (preserve operator edits) — default |
| Whole tree absent | write every file (degenerate case of the above) |
| File present + `--force` | overwrite with the packaged file (explicit opt-in) |

This is exactly `init`'s existing `add_file(..., overwrite=False)` behavior
([`cli.cmd_init`](../../src/wastech_orchestrator/cli.py)), lifted into a command that can run **after**
install and that resolves the install location like the `upgrade-*` commands rather than taking a raw
scaffold `path`. `--dry-run` previews the add/skip set and writes nothing (mirror
`upgrade-config`/`upgrade-docs`).

Note the asymmetry with `upgrade-docs` is intentional: `worc/` docs are generated (overwrite is safe);
`templates/` are operator-editable (skip-existing is safe). Two different refresh contracts for two
different kinds of content.

## 5. CLI surface

`worc install-templates [--config PATH] [--force] [--dry-run]`:

- Resolves the install directory via `resolve_config_path`
  ([`cli.resolve_config_path`](../../src/wastech_orchestrator/cli.py)) — explicit `--config`, then
  `./config.yaml`, then the repo→config registry binding — and installs `templates/` **beside the
  resolved `config.yaml`**, consistent with where `init` puts it and where `upgrade-docs` puts `worc/`.
- Fail-closed (exit 2) when no install location resolves, with the same actionable hint as
  `upgrade-config`/`upgrade-docs` ("pass `--config PATH`, run from a dir with `config.yaml`, or
  `install` to bind this repo").
- Add-missing by default; `--force` overwrites existing files (explicit, like `init --force`).
- `--dry-run` prints the planned `+ add` / `skip` (and, under `--force`, `~ overwrite`) set and exits 0
  writing nothing.
- Idempotent: a second run with no `--force` reports "already complete (N files)" and writes nothing.

## 6. Where the templates land (and a `templates_dir` subtlety)

`init` puts `templates/` in the scaffolded directory and the default
`prompts.templates_dir: "./templates/prompts"` resolves from the **current working directory**, which
for the in-repo footprint is the repo root — so it lines up. For the **external-workspace** footprint
(`install`), `config.yaml` lives in the workspace but `templates_dir` would still resolve from the CWD
(the repo), so dropping `templates/` next to the workspace's `config.yaml` would **not** be found by
the default `templates_dir` unless either (a) the operator runs `worc` from the workspace, or (b)
`templates_dir` is made resolvable relative to the config file.

This command should therefore **install beside `config.yaml`** (predictable, matches `upgrade-docs`)
and the doc must call out that, for the external footprint, the operator may need to set
`prompts.templates_dir` to the absolute/config-relative path. Making `templates_dir` config-relative
is a separate (small) improvement worth tracking as an open question (§11) — it is the same class of
"resolve relative to the config, not the CWD" fix that would make install-based prompt overrides work
out of the box.

## 7. Config handling (what "auto-update config" should and should not mean)

The original idea paired "install templates" with "update config automatically". Two distinct things:

- **Materialize new config keys** — already done by `upgrade-config`. This command does **not**
  duplicate it; run `upgrade-config` for that (and ideally both fold into the umbrella `upgrade` —
  see the follow-up referenced in §12).
- **Auto-activate templates by writing `prompts.overrides`** — **rejected.** A freshly-copied template
  is byte-identical to the packaged default, so wiring it as an override is a near-no-op: in `append`
  mode it duplicates the default text, in `replace` mode it is the same text. Activation is only
  meaningful **after** the operator edits a template, and choosing which stages to override is an
  operator decision, not an install side effect. Auto-writing overrides would also be a foot-gun
  (silently changes prompt behavior). So this command copies files and leaves `prompts.overrides`
  alone; the operator opts in by editing config (as today).

## 8. Relationship to existing commands and the umbrella `upgrade`

There is already a `candidate` follow-up for a single umbrella **`upgrade`/`doctor`** that folds
`upgrade-config` + `upgrade-docs` (follow_ups.md, 2026-06-14). `install-templates` is the natural
**third member** of that family:

- Stands alone as a command (the operator's explicit request), but
- Should be **callable from the umbrella** so one `worc upgrade` brings config keys, `worc/` docs, and
  the `templates/` tree all current. Decide whether the umbrella runs `install-templates` in
  add-missing mode (safe default) or surfaces a `--force` pass-through.

Implementation reuse: the file-walk and add/skip logic already exist in `cmd_init`
(`_iter_template_files`, `add_file(..., overwrite=...)`) and `_copy_worc_docs`; factor the
template-tree copy into a shared helper so `init` and `install-templates` cannot drift.

## 9. Security requirements

- Templates remain **prompt-text-only**; this command does not change how templates are read or what
  they are allowed to influence (§7 of the spec / the `prompts:` block invariant). Copying a file
  cannot grant a template the ability to change provider/sandbox/approvals/env.
- The packaged templates are the trusted source; the command copies **from the wheel**
  (`resources.files("wastech_orchestrator")/templates`) to the install dir — it never fetches remotely
  and never reads operator input into the file contents.
- No secrets are read or written; the operation is a local file copy with the same atomic-write /
  skip-existing discipline as `init`/`upgrade-docs`.

## 10. Testing requirements

- Unit: add-missing writes only absent files and skips present ones; whole-tree-absent writes
  everything; a single missing file is added while siblings are skipped; `--force` overwrites;
  `--dry-run` writes nothing and reports the correct add/skip set; idempotent second run is a no-op.
- Location resolution: installs beside the resolved `config.yaml` for `--config`, for `./config.yaml`,
  and for the registry binding; fail-closed (exit 2) with the actionable hint when none resolves
  (mirror the `upgrade-config`/`upgrade-docs` tests).
- Shared-helper parity: `init` and `install-templates` produce the same `templates/` tree from the
  same packaged source (guards against drift).
- Integration: after `install` (external workspace) which omits `templates/`, `install-templates`
  populates the workspace; an operator-edited `prompts/implementation.md` survives a default re-run
  and is only replaced under `--force`.

## 11. Open questions

- **Name.** `install-templates` (parallels `install`/`upgrade-docs`) vs. `sync-templates` (signals
  add-missing reconciliation) vs. `add-templates`. Avoid `init templates` — `init` is the
  whole-project scaffold and this is a scoped, post-install operation with different (add-missing,
  config-resolving) semantics.
- **Scope of the tree.** All of `templates/` (prompts + skills + `AGENTS.md`/`CLAUDE.md`/`task.md`) or
  a `--only prompts|skills` selector? Default to the whole tree; a selector is a nice-to-have.
- **`templates_dir` resolution** (§6): make `prompts.templates_dir` resolvable relative to the config
  file so external-workspace installs find the templates without an absolute path. Small config/loader
  change; bumps `schema_version` only if the default value changes.
- **Umbrella membership** (§8): does `worc upgrade` run `install-templates` automatically (add-missing),
  and how does `--force` thread through?

## 12. Acceptance criteria

- [ ] `worc install-templates` delivers the packaged `templates/` tree into the resolved install dir
      **add-missing-only**: absent files are written, existing files are preserved, a single missing
      file is added without touching its siblings.
- [ ] `--force` overwrites existing templates; `--dry-run` writes nothing and reports the exact
      add/skip(/overwrite) set; a second default run is a no-op ("already complete").
- [ ] The install location is resolved like `upgrade-config`/`upgrade-docs` (`--config` → `./config.yaml`
      → registry binding) and the command is fail-closed (exit 2) with an actionable hint when none
      resolves; templates land beside `config.yaml`.
- [ ] An install-based (external-workspace) setup, which `install` leaves without `templates/`, can be
      populated by this command; operator edits survive a default re-run.
- [ ] The command does **not** write `prompts.overrides` (no auto-activation) and does not duplicate
      `upgrade-config`'s key materialization.
- [ ] `init` and `install-templates` share one copy helper (no drift), and tests cover the
      add/skip/force/dry-run matrix, location resolution, and the install-workspace integration case.

## 13. References

- Scaffold + current template copy: [`cli.cmd_init`](../../src/wastech_orchestrator/cli.py)
  (`_iter_template_files`, `add_file`, `_templates_root`); worc/ copy: `_copy_worc_docs`.
- Refresh siblings: `cli.cmd_upgrade_config` / `cli.cmd_upgrade_docs`; `config/upgrade.py`;
  location resolution `cli.resolve_config_path`.
- Install wizard (omits templates): [`cli.cmd_install`](../../src/wastech_orchestrator/cli.py);
  `install/wizard.py`, `install/config_writer.py`, `install/registry.py`.
- Runtime template loading (overrides only): [`core/prompts.py`](../../src/wastech_orchestrator/core/prompts.py)
  `_packaged_default`/`PromptTemplateStore`; `prompts:` block in
  [config.example.yaml](../../src/wastech_orchestrator/templates/config.example.yaml).
- Umbrella `upgrade`/`doctor` follow-up: [follow_ups.md](follow_ups.md) (2026-06-14, "Single umbrella
  `upgrade` command"). Prompt-customization origin:
  [prompt_template_customization.md](prompt_template_customization.md).
