# Refresh installed flows without reinstalling: `worc upgrade-flows`

Status: **proposed** Date: 2026-07-27 Owner: Vladimir Makarevich

## Problem

Packaged flows improve with the orchestrator; installed flows do not. A target repository resolves flows **only** from `<repo>/.worc/flows/`, so every improvement to a packaged flow is inert in an already-installed repo until someone refreshes that tree — and the only command that refreshes it also rewrites `config.yaml`. The result is that the improvements pile up unclaimed, which is exactly what happened to the `deep_research` campaign: six changes across five items, all waiting behind one refresh nobody wants to run.

This is not the git-ref/pipx install friction covered by the separate install-and-upgrade item — that one is about getting a newer `worc` onto the machine at all. This one starts after that succeeded: the new `worc` is installed and its better flows still do not run.

## Current behavior (verified)

**Flows resolve from the operator tree alone.** [`registry.py`](../../src/wastech_orchestrator/core/flow/registry.py): "Flows live in the operator's `<repo>/.worc/flows/` — the sole resolution source. The bundled `packaged/` tree is delivery-only (`worc install` copies it into `.worc/`) and is never read here."

**A plain re-install changes nothing.** [`_copy_packaged_flows`](../../src/wastech_orchestrator/cli.py) skips existing files unless `overwrite`, deliberately: "so a plain re-run preserves operator edits". So `worc install` on an installed repo fills in missing files and leaves every stale one.

**The one refresh path is coupled to config.** `worc install --reconfigure` snapshots `.worc/flows/` via `_backup_flows_dir` and re-copies with `overwrite=True` — correct for flows, but the same flag also backs up and regenerates `config.yaml` from the installer's defaults. An operator with `checks.command_sets`, Telegram wiring and a tuned `agents.retry.max_blocked_s` has to re-apply all of it afterwards. That cost, not the copy, is what stops the refresh from happening.

**The targeted-refresh pattern already exists for docs.** `worc upgrade-docs` ([`cmd_upgrade_docs`](../../src/wastech_orchestrator/cli.py)) resolves the install from `config.yaml`, diffs packaged against installed, reports `+ ~ -` per file, no-ops when current, previews under `--dry-run`, writes atomically, and removes files no longer in the package. It takes no backup, and says why: the guide is "generated content with no operator edits to preserve". **Flows are the opposite** — they are the editable, active copies that override the built-ins — so the shape transfers but that one justification does not.

**What the coupling cost, concretely.** The `deep_research` refresh backlog carries `gate_severity` on the evaluators, `git_evidence` on three analysis nodes, `output_file` on `synthesis` plus two rewritten evaluator prompts, two rewritten role prompts, a dropped `when:` predicate and a whole new `document_checks` node — and P1.4's four new role files, which are the one entry that **fails the flow to load** if the YAML arrives without them. A hand copy is the likely response to "refresh your flows" with no command named, and a hand copy is exactly how that order gets reversed.

## Proposed minimal design

`worc upgrade-flows` — `cmd_upgrade_docs`'s shape, with a backup, over `.worc/flows/`:

- resolve the install from `config.yaml` (same fail-closed exit 2 as `upgrade-docs`/`upgrade-config`);
- diff the packaged flow tree against the installed one, and report `+ added` / `~ differs` per file;
- `--dry-run` previews and writes nothing;
- no-op with a clear message when the trees already match;
- `_backup_flows_dir` (already written, already used by `--reconfigure`) before the first write, so operator edits and custom flows stay recoverable under the gitignored `.worc/`;
- atomic writes via `_install_atomic_write`, whole tree at once so a YAML never lands without its role files;
- **never touch `config.yaml`.**

## Open questions

These are the actual decisions; the copy is the easy part.

1. **Orphans.** `upgrade-docs` deletes installed files absent from the package. Doing that here would delete the operator's **own** flows (`my_flow.yaml` and its role dir) — the feature that makes the operator tree authoritative in the first place. Options: never delete (leave `deep_research/repository_analysis.md` lying around as a harmless orphan); report them as `?` without deleting; or delete only inside a packaged flow's own namespace (its `<name>.yaml` plus `<name>/`), which is narrower but needs a rule for what "namespace" means for a flow whose role dir the operator also extended.
2. **Operator edits are silently clobbered.** A backup makes them recoverable, not visible. Should a file that differs from packaged **block** the refresh until `--force`, or is the `~ differs` line in the report enough? A hand-tuned packaged flow is a normal, encouraged thing here, so overwriting one without a word is the sharpest edge in this feature.
3. **Whole tree or one flow?** `worc upgrade-flows deep_research` matches the campaign's actual need and shrinks the blast radius; the whole tree matches `upgrade-docs`. Cheap either way, but it changes what the backup means.
4. **Or split the existing flag instead.** `install --reconfigure-flows` (config untouched) would add no new command and no new resolution path. Weigh against discoverability: an operator looking for "how do I get the new flows" greps the command list, and `upgrade-docs` has already taught them the verb.

## Scope / risk

CLI-only: no engine change, no schema change, nothing on the publish path. The risk is not correctness but **data loss of operator work**, which is why the backup and the pre-write report are the feature and the copy is the detail. Greenfield, so no versioning or migration of flow files — this is a refresh, not an upgrade path.

## Depends on

Nothing. Independent of the campaign that motivated it; the campaign's own refresh can be done by hand with `install --reconfigure` in the meantime.
