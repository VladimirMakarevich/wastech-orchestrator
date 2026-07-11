# Move flow validation out of preflight into a dedicated `worc validate-flow` command

Status: **implemented** (2026-07-11) Date: 2026-07-09 Owner: Vladimir Makarevich

Implemented on `feat/validate-flow-command`: `run_preflight` no longer touches flows; `FlowRegistry` gained `operator_flow_names()` (operator-only discovery, packaged built-ins excluded) and `check_flows(names)` (resolve + prompt-lint, no-raise, returning `FlowCheck`), replacing the now-unused `validate_all`/`lint_all`/`_all_flow_names`; and a thin `worc validate-flow [NAME] [--all]` CLI wrapper drives them config-aware, folding the prompt-variable lint in as WARN (exit `0`/`1`/`2`). `_find`/`resolve`/`_builtin_names` and the packaged fallback are unchanged. All four open questions resolved per their MVP proposals (strictly `.worc/` — no `--builtin`; `NAME` as stem or `NAME.yaml`; lint folded in as WARN; preflight fully flow-free). The `upgrade-flows` re-sync gap remains tracked separately.

`worc preflight` currently validates flows, and it does so wrongly on two axes: it validates the orchestrator's own packaged built-in flows (globbed from the installed package, not the operator's repo) and it hard-fails the whole health gate on any single invalid flow. This ADR removes flow validation from `preflight` entirely and introduces an on-demand `worc validate-flow [NAME] [--all]` command scoped to the operator's `.worc/flows/`.

## The problem

`run_preflight` ([cli.py:2126-2138](../../../../src/wastech_orchestrator/cli.py#L2126)) builds a `FlowRegistry` over `.worc/flows/` and calls `validate_all()` (fatal) plus `lint_all()` (WARN). But `FlowRegistry._all_flow_names()` ([registry.py:158-163](../../../../src/wastech_orchestrator/core/flow/registry.py#L158)) enumerates the **union** of the operator's `.worc/flows/*.yaml` and the packaged built-ins globbed from `_PACKAGED_DIR` inside the installed package ([registry.py:44](../../../../src/wastech_orchestrator/core/flow/registry.py#L44), [registry.py:165-167](../../../../src/wastech_orchestrator/core/flow/registry.py#L165)). So preflight validates built-in flows that are **not physically present** in the operator's repo, against that repo's config.

Two concrete failures follow. (1) Because `validate_all()` runs the config-aware layer `validate_flow_against_config(snap, config, tools)`, a packaged flow the operator never touched can fail against a config that does not fit it — e.g. `content_book`/`content_chapter`/`content_translate` each fail with `1 violation` in a repo that has no `check_journey` tool in `.worc/tools/` (they contain a `tool: check_journey` node, [content_chapter.yaml:37-38](../../../../src/wastech_orchestrator/packaged/flows/content_chapter.yaml#L37)). (2) `run_preflight` folds every flow result into one shared `ok` accumulator, so a single invalid flow — used or not, operator or built-in — flips the whole gate to `preflight: NOT ready` and exit code 1. Observed on `wastech-mdlint`: `claude`/`codex`/isolation/gh/telegram all OK, three unused content flows FAIL, verdict `NOT ready`.

This is the wrong shape. Preflight is a **health gate** for the run surface (CLIs, auth, isolation, gh, telegram); it should not double as a flow linter. An operator may author hundreds of custom flows in `.worc/flows/`, and validating them all on every preflight — with one bad file blocking the gate — is noise, not health. Flow correctness is already re-checked where it matters: `resolve()` runs the full validator at task dispatch ([orchestrator.py:1567](../../../../src/wastech_orchestrator/core/orchestrator.py#L1567)), so a broken flow that a task actually requests fails safely at run time (task → failed/quarantine), not on a global gate.

## Constraints

The core must not learn CLI syntax — validation logic stays in `core/flow/` (validator + registry); the new command is a thin CLI wrapper (architecture invariant). Cross-platform: discovery is `pathlib` glob, paths displayed as POSIX. The command is read-only diagnostics — no mutation, no secrets in output (the validator's messages are already redaction-safe). Greenfield MVP — no migration machinery; the built-in fallback in `resolve()`/`_find` ([registry.py:148-156](../../../../src/wastech_orchestrator/core/flow/registry.py#L148)) stays exactly as is so tasks can still use packaged flows without copying them into `.worc/`.

## Alternatives considered

| Option | Why rejected |
| --- | --- |
| Do nothing | Preflight keeps reporting `NOT ready` for built-in flows the operator does not have and does not use; false negative on the health gate. |
| Two-tier fatality in preflight (fatal only for flows that will run — default + merge + queued task_types; WARN for the rest) | Correct on fatality, but still validates flows inside preflight and still walks potentially hundreds of custom flows on every run. Owner's call: preflight is a health gate, not a flow linter — the linting belongs in its own command. |
| Keep `validate_all` in preflight but scope to `.worc/` only and make it non-fatal WARN | Half-measure: still runs the whole flow-lint on every preflight and clutters the health output with per-flow lines. |
| **Chosen: remove flow validation from preflight; add on-demand `worc validate-flow` scoped to `.worc/`** | Preflight stays a clean health gate; flow validation is explicit, operator-driven, and only over the operator's own flows. Dispatch-time `resolve()` remains the safety net. |

## Decision

Remove the flow-validation and flow prompt-lint blocks from `run_preflight` so preflight no longer touches flows at all; add `worc validate-flow [NAME] [--all]` that validates the operator's `.worc/flows/` on demand, config-aware, ignoring the packaged built-ins. We do this because preflight is a run-surface health gate and per-flow linting is a separate, on-demand concern over operator-authored content; the cost is that a broken flow is no longer surfaced at preflight time — it is caught at task dispatch (`resolve()`) or when the operator runs `validate-flow`, which the owner accepts.

Command shape (owner-selected): a flat `worc validate-flow`. A positional `NAME` validates one flow (`worc validate-flow test100023`); `--all` validates every `*.yaml` in `.worc/flows/`; passing neither is a usage error. Depth (owner-selected): **config-aware** — the full validator, `validate_flow` + `validate_flow_against_config` including the tool-registry check, so it catches exactly what the engine sees at dispatch (a disallowed provider/model/reasoning, a missing `.worc/tools/` tool). This means the command loads config like the other commands. Discovery scope: `.worc/flows/` only — packaged built-ins are excluded from `validate-flow`; they are covered by the orchestrator's own test suite, not by validating them against an arbitrary target repo's config.

## Open questions

Should `validate-flow NAME` also be able to target a packaged built-in (e.g. a `--builtin` flag) for debugging, or stay strictly `.worc/`? MVP proposal: strictly `.worc/`.

`NAME` accepted as bare stem, `NAME.yaml`, or an arbitrary path? Proposal: stem or filename resolved within `.worc/flows/`, not arbitrary paths (keeps it a repo-scoped operator command).

The non-fatal prompt-variable lint (`lint_prompt_variables`, currently `lint_all()` in preflight) — fold it into `validate-flow` as a WARN, or drop it from the product? Proposal: fold it into `validate-flow`.

Preflight will have zero flow awareness. Is that fully acceptable, or do we want the default flow (`implementation`) / merge flow smoke-checked at install time instead? Owner chose full removal; dispatch-time `resolve()` is the net. (Recorded so it is a conscious choice, not an oversight.)

## Implementation notes

Preflight: delete the `validate_all()` loop and the `lint_all()` loop from `run_preflight` ([cli.py:2123-2138](../../../../src/wastech_orchestrator/cli.py#L2123)); leave the rest of the gate untouched. The preflight subparser help ([cli.py:310](../../../../src/wastech_orchestrator/cli.py#L310)) already omits flows.

New command: add a `validate-flow` subparser near the preflight one, with a positional `name` (`nargs="?"`) and `--all`; dispatch to a new `cmd_validate_flow` that loads config (`load_config_for`), builds `FlowRegistry(operator_flows_dir=worc_home_for(config) / "flows", config=config)`, enumerates **operator-only** flow names, `resolve()`s each (config-aware), prints per-flow `OK`/`FAIL`, and returns 0 (all ok) / 1 (any invalid) / 2 (name not found or config load error).

Registry seam: add operator-only discovery so packaged built-ins are excluded from validation — e.g. a `FlowRegistry.operator_flow_names()` that globs only `_operator_dir`, or an operator-only parameter on `validate_all`. Do **not** change `_find`/`resolve` — the packaged fallback must stay so tasks can dispatch built-in flows without copying them. `_all_flow_names()` and the union it builds become unused by preflight; repurpose or drop with the change.

Docs: update the shipped operator docs (`packaged/guide/` preflight description + the new command), `docs/functional/` CLI reference, and any preflight docs that claim it validates flows. No config schema change (no new config key), so no `schema_version` bump.

Related (separate) defect — not in scope here: `worc install` seeds flows only on first install / `--reconfigure`; an orchestrator upgrade that ships new packaged flows does not add them to an existing `.worc/flows/` (no `upgrade-flows` re-sync). This ADR makes the missing-flow case harmless for preflight regardless, but the re-sync gap is tracked separately (see the "Single umbrella `upgrade`/`doctor`" and the "paths cited in `.worc/flows/*.md` still exist" rows in [follow_ups.md](../../follow_ups.md)).
