# Backlog: Prompt template customization

Status: **backlog / not scheduled**
Date: 2026-06-12
Owner: Vladimir Makarevich

This document captures the task of making stage prompts configurable by operators. It is a backlog
item, not part of the currently implemented runtime behavior. Nothing here overrides
[00_orchestrator_final_plan.md](../implementation_stages/00_orchestrator_final_plan.md), [CLAUDE.md](../../CLAUDE.md), or the hard
invariants in [docs/rules/](../rules/).

## 1. Goal

Allow users to extend or replace the instructions passed to Codex / Claude Code for each agent-run
stage (`refinement`, `planning`, `implementation`, `review`, `fixing`, `summary`) without editing
Python code.

The main use cases:

- add repository-specific engineering rules to implementation prompts;
- add review rubrics for security, performance, or architecture;
- require a specific planning format;
- customize summary text for PR handoff conventions;
- add domain-specific constraints while keeping the orchestrator pipeline deterministic.

## 2. Current behavior

The runtime currently uses short hardcoded prompts in
`src/wastech_orchestrator/core/orchestrator.py` (`_STAGE_PROMPTS`). The packaged files under
`src/wastech_orchestrator/templates/prompts/*.md` are copied by `init` into `templates/prompts/`,
but the Core does not read those files when it starts an agent run.

Provider adapters then append a deterministic context footer containing artifact file paths:

```text
Context files (read them as needed; do not assume their contents):
- task: ...
- plan: ...
- diff: ...
- checks: ...
- review: ...
```

The prompt is sent to the CLI on stdin. Task content and artifact content are not interpolated into
the provider argv.

## 3. Desired behavior

Add a prompt-template layer between the Core stage driver and `AgentRunRequest.prompt`:

```text
Core stage -> PromptTemplateStore -> PromptRenderer -> AgentRunRequest.prompt -> Provider adapter
```

Target behavior:

- packaged prompt templates remain the default;
- `init` continues to copy editable templates to `templates/prompts/`;
- operators can override any stage prompt from `config.yaml`;
- missing user templates fall back to packaged defaults;
- rendered prompts are registered as artifacts for audit/debugging;
- provider adapters continue to append context file paths and continue to own CLI-specific syntax.

The Core may know stage names and prompt-template variables, but it must not gain provider-specific
CLI knowledge.

## 4. Proposed configuration

Add a new top-level block:

```yaml
prompts:
  templates_dir: "./templates/prompts"
  mode: "append"                 # append | replace
  strict: false                  # false = fallback to packaged default when a file is missing
  overrides:
    implementation: "implementation.md"
    review: "review.md"
```

Semantics:

- `templates_dir` resolves relative to the orchestrator project root, not the target repo.
- `overrides.<stage>` maps only known agent-routed stages to files inside `templates_dir`.
- `mode: append` renders packaged default first, then appends the user template.
- `mode: replace` uses only the user template for stages that have an override.
- `strict: true` fails config validation if an override file is missing.
- `strict: false` logs a warning and uses the packaged default.

Open decision: whether `mode` should be global only, or allow per-stage mode in a later version.
Start with global mode to keep the interface small.

## 5. Template variables

Use a small allowlisted variable set. Do not expose arbitrary Python objects or environment values.

Suggested variables:

| Variable | Meaning |
|---|---|
| `{task_id}` | Normalized task id. |
| `{stage}` | Current agent-routed stage. |
| `{repo_path}` | `repo.local_path`. |
| `{task_path}` | Path to the original task file. |
| `{plan_path}` | Path to `plan.md`, when present. |
| `{diff_path}` | Path to `current.diff`, when present. |
| `{checks_path}` | Path to the latest check artifact, when present. |
| `{review_path}` | Path to review findings, when present. |
| `{subtask_order}` | Active subtask order, when decomposed. |
| `{subtask_count}` | Total subtask count, when decomposed. |
| `{subtask_spec_path}` | Active subtask spec path, when decomposed. |

Important constraint: variables should be metadata and artifact paths only. Do not inject full task
body, full diffs, check logs, or secrets directly into the prompt template. Large content should
remain in artifact files referenced by path.

## 6. Security and invariants

This feature must preserve the existing invariants:

- provider adapters still launch CLIs with argv lists and stdin prompts;
- user templates cannot change provider command, `extra_args`, credentials, sandbox, approval mode,
  denied commands, denied reads, or environment allowlists;
- template paths are normalized and must remain inside `prompts.templates_dir`;
- rendered prompt artifacts are redacted before storage;
- secrets and full environment values are never available as template variables;
- task front matter cannot choose an arbitrary template path;
- fallback remains infrastructure-only; prompt customization must not change fallback policy.

## 7. Implementation sketch

Suggested components:

- `config.schema`: add `PromptsConfig`.
- `config.loader`: parse `prompts.templates_dir`, `mode`, `strict`, and `overrides`.
- `config.validation`: reject unknown stages, path traversal, unsupported modes, and invalid
  extension/suffixes.
- `core/prompts.py`: add `PromptTemplateStore` and `PromptRenderer`.
- `core/orchestrator.py`: replace direct `_STAGE_PROMPTS[stage]` lookup with prompt rendering.
- `providers/artifacts.py` or Core artifact registration: write `rendered-prompt.md` per stage
  attempt for audit.
- `templates/prompts/*.md`: keep packaged defaults in sync with the hardcoded prompts before
  deleting `_STAGE_PROMPTS`.

Keep provider adapters unchanged except for receiving the rendered prompt through the existing
`AgentRunRequest.prompt` field.

## 8. Testing plan

Unit tests:

- default config renders packaged templates;
- append mode combines packaged + user template in deterministic order;
- replace mode uses the user template only;
- missing template with `strict: false` falls back with a warning;
- missing template with `strict: true` is a config error;
- unknown stage in `prompts.overrides` is rejected;
- path traversal in override paths is rejected;
- rendered prompt variables are limited to the allowlist;
- rendered prompt artifact is redacted.

Integration tests with fake providers:

- implementation stage receives a custom instruction in stdin;
- review stage receives its custom rubric;
- provider argv is unchanged by prompt customization;
- fallback receives the same rendered prompt policy plus the fallback partial diff path.

Regression tests:

- no template can enable `git commit`, `git push`, or `gh pr create`;
- no template can alter sandbox/permission settings;
- no task-level field can point to an arbitrary template file.

## 9. Documentation updates

When implemented, update:

- [configuration.md](../configuration.md) with the `prompts:` block;
- [cookbook.md](../cookbook.md) with a recipe for adding repository-specific implementation/review
  instructions;
- [operations.md](../operations.md) with troubleshooting for missing or malformed templates;
- packaged `templates/prompts/*.md` comments/examples.

## 10. Open questions

- Should custom templates be append-only by default, or should replace mode be allowed in v1?
- Should rendered prompts be stored once per stage or once per provider attempt?
- Should prompt customization support a global preamble shared by all stages?
- Should project-specific agent stubs (`AGENTS.md`, `CLAUDE.md`, skills) be managed by the same
  `prompts:` config block or by a separate `agent_instructions:` feature?
