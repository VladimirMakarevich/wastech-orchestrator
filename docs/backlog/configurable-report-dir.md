# Configurable report directory for the report output policies

Status: **proposed** Date: 2026-07-25 Owner: Vladimir Makarevich

## Problem

A `deep_research` flow always writes its deliverable to `{repo}/docs/research/<task_id>/` — the operator cannot point the flow at a different location (`docs/adr/`, `research/`, `docs/analysis/`, …). The directory is a property of the _engine_, not of the flow, so forking the packaged flow into `.worc/flows/` does not help.

## Current behavior (verified)

`output_policy` is a closed set of three names, and each resolves to a hardcoded directory in [`output_policy.py`](../../src/wastech_orchestrator/core/flow/output_policy.py):

```python
_RESEARCH_DIR = "docs/research"
_PRIVATE_REPORT_DIR = f"{PRIVATE_HOME_DIRNAME}/security-reports"
...
report_subdir = f"{_RESEARCH_DIR}/{task_id}"
required_files = ("report.md", "sources.json")
```

This is deliberate and documented — [flow-authoring.md → Output policy](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/flow-authoring.md#output-policy) states "You cannot specify anything else, and you cannot point a flow at an arbitrary directory". The item below proposes relaxing _where_, not _what_.

The second, less obvious half: the packaged `deep_research` role prompts hardcode the path as **literal text** (`{repo}/docs/research/{task_id}/` in `synthesis.md`, `architecture_design.md`, `verifier.md`, `critic.md`), and there is no `{report_dir}` prompt variable in [`ALLOWED_PROMPT_VARS`](../../src/wastech_orchestrator/core/prompts.py) / [`build_path_context`](../../src/wastech_orchestrator/core/flow/context_paths.py). An engine-only change would therefore "work" while the agent still writes to the old path — and the after-stage containment guard would hard-stop the task at `manual_action_required` on the first write. The prompt-variable seam is part of the feature, not an optional extra. (`security_audit` needs no prompt change: its report comes back as structured output and the orchestrator writes it — WRI-001.)

## Proposed minimal design

`flow.report_dir: <base>` (default `docs/research`), with the engine still appending `/<task_id>` itself. Keeps per-task collision-freedom and the self-identifying report directory, and reduces the validator to checking one base directory instead of an arbitrary template.

Touch points:

| What | Where | Size |
| --- | --- | --- |
| `report_dir` field on `FlowDoc` | [`schema.py`](../../src/wastech_orchestrator/core/flow/schema.py) | ~2 lines |
| `_FLOW_FIELDS` allowlist + `_parse_flow_doc` | [`snapshot.py`](../../src/wastech_orchestrator/core/flow/snapshot.py) | ~5 lines (the fingerprint is SHA-256 over the raw `flow:` dict, so it covers the new key for free) |
| Override parameter on `resolve_output_policy` | [`output_policy.py`](../../src/wastech_orchestrator/core/flow/output_policy.py) | ~5 lines |
| 4 call sites pass it | `core/orchestrator.py`, `flow/nodes/checks.py`, `flow/nodes/agent.py`, `flow/nodes/publish.py` | 1 line each |
| **Path validation** (the actual work) | [`validator.py`](../../src/wastech_orchestrator/core/flow/validator.py) | ~30 lines |
| `{report_dir}` prompt variable | `core/prompts.py`, `flow/context_paths.py` (+3 callers; the `tool` node gets it in `paths` for free) | ~10 lines |
| 4 `deep_research` role prompts | `packaged/flows/deep_research/` | literal → `{report_dir}` |
| Docs | `flow-authoring.md`, `glossary.md`, `worc_architecture.md`, `packaged/guide/flows/{README,reference}.md`, `packaged/guide/skills/worc-flow/SKILL.md` | the bulk of the change |
| Tests | `test_flow_{output_policy,snapshot,validator,deep_research}.py` | ~5 files |

Roughly half a day to a day including tests and docs.

## Pitfalls

1. **The private policy must not escape.** For `private_control_workspace_report` an override outside `.worc/` breaks the "never enters git" invariant. Simplest and most honest: reject the override for that policy outright.
2. **Path validation is the security surface** — reject absolute paths, drive letters, `..`, backslashes, `.git/`, `tasks/`, `.worc/`, `.worc-io/`, and Windows device names per segment. Reuse [`security/identifiers.py`](../../src/wastech_orchestrator/security/identifiers.py) rather than writing a new check.
3. **Prompts are mandatory, not optional** — see above.

## Non-goals

- **`required_files` stays fixed** (`report.md` + `sources.json`). Making the deliverable filenames configurable is the separate, larger "declared target document" idea (raised 2026-07-14), not part of this change.
- No per-task override. Flow level only; per-task flow overrides today are limited to `nodes.<id>.enabled`.

## Workaround today

Fork the flow with `output_policy: code_change` and name the directory in the prompt. Cost: the after-stage containment guard is off, and the `citation` node degenerates into a vacuous pass — with no report directory the checker looks for `sources.json` in the checks dir, does not find it, and [returns `passed=True`](../../src/wastech_orchestrator/core/flow/checkers/citation.py) as "uncheckable". Both guarantees `repository_document` exists for are lost.
