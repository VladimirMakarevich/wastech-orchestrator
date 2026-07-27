# Re-sync the `wastech-mdlint` target after the `deep_research` campaign

Status: **proposed** Date: 2026-07-27 Owner: Vladimir Makarevich

An operator checklist, not a code change: **nothing here lives in this repository.** The `deep_research` post-mortem campaign improved the packaged flow, its role prompts and several defaults, and none of it reaches the validation target (`wastech-mdlint`, the repo the campaign's only production run came from) until that repo's own `.worc/` is refreshed. Extracted from the campaign's `follow_ups.md` because that folder is deleted once its items land, while these chores stay undone.

## 1 — refresh `.worc/flows/`

Flows resolve **only** from `<repo>/.worc/flows/`; the packaged tree is delivery-only. A target carrying its own `deep_research.yaml` keeps running that copy, so six changes are queued behind one refresh:

| Change | If skipped |
| --- | --- |
| **P1.4** — four new role files (`analysis_core.md`, `analysis_surfaces.md`, `analysis_docs_tests.md`, `coverage.md`); `repository_analysis.md` retired | **the flow fails to load** on a missing `role_file` — the only one that breaks a run rather than withholding an improvement |
| **P0.1** — `gate_severity` on the packaged evaluators | a `medium` finding is recorded and then dropped |
| **P1.4a** — `git_evidence: true` on the three analysis nodes | harmless: inert unless `security.allow_git_evidence` is on |
| **P2.8** — `output_file: report.md` on `synthesis` + rewritten `verifier.md`/`critic.md` | **must go together**: without the flow field `{synthesis_path}` resolves to the node's closing message, so refreshing only the prompts points both evaluators at a summary instead of the deliverable. Only the field is harmless |
| **P2.9** — rewritten `architecture_design.md`/`synthesis.md` | the intermediate blueprint keeps shipping in the pull request |
| **P3.10** — `refinement` without its `when:`; new `document_checks` node + two edges | the scoping pass never runs; no documentation gate before the commit |

Do it as one refresh, not six:

```bash
worc install --reconfigure     # in the target repo
worc validate-flow deep_research
```

`--reconfigure` snapshots the existing `.worc/flows/` to a timestamped sibling (under the gitignored `.worc/`, so it never shows in `git status`) and re-copies the packaged tree, so all six land together and a YAML can never arrive without its role files. Two things it also does:

- **it backs up and regenerates `config.yaml`** — re-apply `checks.command_sets`, Telegram wiring and any tuned `agents.retry.*` from the backup. That coupling is why this refresh keeps not happening; a targeted command is proposed in [upgrade-flows.md](upgrade-flows.md);
- **it removes nothing** — `deep_research/repository_analysis.md` stays behind as a harmless orphan.

## 2 — give the new document gate something to run

`document_checks` runs the operator's own `checks.command_sets`, diff-selected. With no set matching the committed documents it selects nothing and passes vacuously, so the gate is present and inert until the target adds one:

```yaml
checks:
  command_sets:
    docs:
      paths: ["**/*.md"]
      commands:
        - { run: "npm run format:check" } # a CHECKING command, not a rewriting one
```

Two ways to get this wrong, both now documented in the shipped guide but worth repeating here:

- a command that **rewrites** files passes green and then trips the core's green-but-dirtying guard → the task parks;
- a **catch-all** set (no `paths`) runs on any non-empty diff, so a research run that added two Markdown files pulls the whole code gate in behind it. Either scope the catch-all to code or keep it and add the `docs` set above.

Also worth knowing before a long run: this gate is fail-closed, so if the command's toolchain is absent on the host the task parks rather than publishing. `skip_if_unavailable: true` only half helps — a skipped set that was the _only_ one selected leaves nothing run, which parks on the same path. The clean escape is per-task `nodes.document_checks.enabled: false`.

## 3 — two config edits and one task-file habit

- **`schema_version: 24` against a packaged `31`.** Seven versions of missing guidance. For the stale _example_ file, re-copy from `packaged/config.example.yaml`; if the real `config.yaml` is what is behind, the command is `worc upgrade-config` (adds new keys from the template, strips removed ones) — copying the example over a live config takes your own settings with it.
- **`agents.retry.max_blocked_s: 3600.0` against a current default of `21600.0`.** The 6 h default is chosen to outlast a provider's ~5 h usage window so a rate-limited task waits out the reset and resumes; at 3600 an expensive run that hits a subscription limit fails an hour in and is lost. Did not fire during the campaign run.
- **Stop setting `nodes.refinement.enabled: false` in the task file.** With the `when:` predicate gone, that switch is now the _only_ thing keeping the scoping pass from running — and that pass is where an audit-shaped question gets its per-subsystem sub-questions.

Plus the ≈ −$0.7 reasoning trim: the packaged flow pins no `reasoning`, so it is a target-side edit, and only the `architecture_design` half of it (P3.10 declined the `fact_verification` half).

**Not on this list, though the campaign claimed it:** `agents.providers.codex.model: gpt-5.4` was reported as stale against a packaged `gpt-5.5`. Verified 2026-07-27 — the packaged value **is** `gpt-5.4` (changed from `gpt-5.5` in `5b36af0`, 2026-07-11, before the campaign), so the target is correct on that key and there is nothing to edit.

## Scope / risk

No code, no tests, nothing in this repository. The only risk is in step 1: run the refresh as one command rather than copying files by hand, because a hand copy is how the P1.4 ordering gets reversed and the flow stops loading.
