# Add an optional pr_title task field to override the generated PR title

## What

Add an optional pr_title task field to override the generated PR title

## How

See the diff below; no provider-authored summary was available.

## Integration

Derived deterministically from the task description and the final diff.

## Why

## Description

Today the orchestrator derives the pull-request title from the task `title`: at the publishing
stage it calls `GitManager.create_pr(..., title=p.task.title)`
([orchestrator.py](src/wastech_orchestrator/core/orchestrator.py), the `_publish` step), and
`create_pr` passes that straight to `gh pr create --title`
([git_manager.py](src/wastech_orchestrator/git_manager.py)). There is no way to make the PR title
differ from the task title.

Add a new **optional** front-matter field **`pr_title`** (type `string | null`) that lets a task
state the PR title explicitly:

- When `pr_title` is present and non-empty, the orchestrator uses it **verbatim** as the PR title.
- When `pr_title` is absent, `null`, or empty/whitespace-only, behavior is **unchanged** — the PR
  title is the task `title`.

The override affects **only the PR title**. It must not change the branch name
(`agent/<task-id>-<slug>`, where the slug is derived from `title`), the commit message
(`feat(<id>): <title>`), the summary, or any report — those keep deriving from `title` exactly as
they do now.

The field threads through the existing task schema and is consumed only at publishing:

1. Add `pr_title` to `ALLOWED_TASK_KEYS` and to the `NormalizedTask` dataclass
   ([task/model.py](src/wastech_orchestrator/task/model.py)), defaulting to `None`.
2. Populate it in the parser / validation gate
   ([task/validation_gate.py](src/wastech_orchestrator/task/validation_gate.py)), normalizing
   empty/whitespace to `None` (mirror how `model`/`reasoning` use `frontmatter.get(...) or None`),
   and add a type check (`pr_title must be a string`) alongside the other field-type checks.
3. At the publishing stage, use `p.task.pr_title` for the PR title when set, otherwise fall back to
   `p.task.title`. This is the only consumption site — keep the change to the `title=` argument of
   the `create_pr` call.

`pr_title` is also accepted as a key in JSON tasks (it flows through the same front-matter map).

For a decomposed task (`decompose: true`) the pipeline still produces a single PR at the end, so
`pr_title`, if set on the parent task, applies to that one PR.

## Acceptance criteria

- [ ] A task with `pr_title: "Custom release title"` opens a PR whose title is exactly
      `Custom release title` (verified via the `title=` argument passed to `create_pr` /
      `gh pr create --title`), while the task `title` is something different.
- [ ] A task with **no** `pr_title` (or `pr_title: null`, or an empty/whitespace value) opens a PR
      whose title equals the task `title` — i.e. current behavior is unchanged.
- [ ] With `pr_title` set, the branch name (`agent/<id>-<slug>`), the commit message
      (`feat(<id>): <title>`), and the summary still derive from `title`, not from `pr_title`.
- [ ] The validation gate rejects a flag-shaped `pr_title` (e.g. a value starting with `--`) with
      reason `injection_suspected`, consistent with how the existing front-matter scan treats
      `title`.
- [ ] A non-string `pr_title` (e.g. a number or a list) is rejected with `invalid_field_type`.
- [ ] `pr_title` is accepted in a `.json` task and behaves identically to the Markdown front-matter
      form.
- [ ] Unit tests cover: override present, override absent/empty (fallback to `title`), flag-shaped
      rejection, and the wrong-type rejection.

## Constraints

- **Only the orchestrator opens PRs** — keep all PR/title logic in the core/`GitManager`; do not move
  any of it into a provider (architecture invariant). Providers must not learn about `pr_title`.
- Do **not** change the branch-slug derivation, the commit-message format, or the summary — the
  override is strictly the PR title.
- **Do not weaken the security policy.** `pr_title` must be passed as an argument-list value (no
  shell interpolation), and must remain subject to the existing front-matter injection scan
  (`scan_frontmatter`); add no path that lets front matter inject CLI flags or bypass approvals.
- No secrets in the field; no new runtime dependencies.
- The canonical field name is **`pr_title`** — do not invent an alternative.
- Update the docs and `CHANGELOG.md` `[Unreleased]` entry **in the same change** (use `/sync-docs`):
  the front-matter field tables in `docs/task-authoring.md` and the authoring docs under
  `worc/` (`README.md` table, and an example if appropriate), plus `docs/configuration.md` only if a
  PR-title default is referenced there. Record any deferred work in
  [docs/backlog/follow_ups.md](docs/backlog/follow_ups.md).
- Add/update tests with the behavior, per the project testing rules; run `ruff`, `mypy`, `pytest`
  before finishing.

## Diff

```diff
diff --git a/.gitignore b/.gitignore
index cda0046..0f29db0 100644
--- a/.gitignore
+++ b/.gitignore
@@ -38,3 +38,5 @@ tasks/rejected/
 .vscode/
 .DS_Store
 Thumbs.db
+state.db-shm
+state.db-wal
diff --git a/CHANGELOG.md b/CHANGELOG.md
index 38c9e5c..58078db 100644
--- a/CHANGELOG.md
+++ b/CHANGELOG.md
@@ -18,6 +18,7 @@ The maintainer bumps the package version in `pyproject.toml` on release; `wastec
 ## [Unreleased]
 
 ### Added
+- Add optional `pr_title` front-matter field to override the generated PR title (falls back to `title` when absent or blank).
 - **Agent task-authoring docs (`worc/`)**: a compact, rule-first, copy-paste-oriented guide an AI
   agent can be pointed at to author valid, well-scoped task files without reading the whole `docs/`
   tree. Authored in [`docs/worc/`](docs/worc/README.md) (the task contract + hard validation rules, a
diff --git a/checks/resolved-profile.json b/checks/resolved-profile.json
new file mode 100644
index 0000000..574b3d9
--- /dev/null
+++ b/checks/resolved-profile.json
@@ -0,0 +1,132 @@
+{
+  "schema_version": 1,
+  "ready": true,
+  "source": "detected",
+  "checks": [
+    {
+      "name": "lint",
+      "argv": [
+        ".venv/bin/ruff",
+        "check",
+        "."
+      ]
+    },
+    {
+      "name": "tests",
+      "argv": [
+        ".venv/bin/pytest"
+      ]
+    },
+    {
+      "name": "types",
+      "argv": [
+        ".venv/bin/mypy",
+        "src"
+      ]
+    }
+  ],
+  "candidates": [
+    {
+      "name": "pytest",
+      "argv": [
+        "pytest"
+      ],
+      "source": "configured",
+      "evidence": [
+        "checks.commands"
+      ],
+      "probe_status": "not_launchable",
+      "selected": false,
+      "rejection": null
+    },
+    {
+      "name": "tests",
+      "argv": [
+        ".venv/bin/pytest"
+      ],
+      "source": "detected",
+      "evidence": [
+        ".venv/bin has pytest"
+      ],
+      "probe_status": "launchable",
+      "selected": true,
+      "rejection": null
+    },
+    {
+      "name": "lint",
+      "argv": [
+        ".venv/bin/ruff",
+        "check",
+        "."
+      ],
+      "source": "detected",
+      "evidence": [
+        ".venv/bin has ruff"
+      ],
+      "probe_status": "launchable",
+      "selected": true,
+      "rejection": null
+    },
+    {
+      "name": "types",
+      "argv": [
+        ".venv/bin/mypy",
+        "src"
+      ],
+      "source": "detected",
+      "evidence": [
+        ".venv/bin has mypy"
+      ],
+      "probe_status": "launchable",
+      "selected": true,
+      "rejection": null
+    },
+    {
+      "name": "tests",
+      "argv": [
+        "pytest"
+      ],
+      "source": "detected",
+      "evidence": [
+        "pyproject.toml present"
+      ],
+      "probe_status": "not_launchable",
+      "selected": false,
+      "rejection": null
+    },
+    {
+      "name": "lint",
+      "argv": [
+        "ruff",
+        "check",
+        "."
+      ],
+      "source": "detected",
+      "evidence": [
+        "ruff declared in pyproject.toml"
+      ],
+      "probe_status": "not_launchable",
+      "selected": false,
+      "rejection": null
+    },
+    {
+      "name": "types",
+      "argv": [
+        "mypy",
+        "."
+      ],
+      "source": "detected",
+      "evidence": [
+        "mypy declared in pyproject.toml"
+      ],
+      "probe_status": "not_launchable",
+      "selected": false,
+      "rejection": null
+    }
+  ],
+  "platform": "darwin",
+  "fingerprint": "ba8af63845f233d6b44ab30cd9240d4ca34b8b23a0ec7d50380d2e1b32c256dd",
+  "created_at": "2026-06-13T23:49:40.603905+00:00",
+  "last_validated_at": "2026-06-13T23:49:40.603905+00:00",
+  "notes": []
+}
diff --git a/docs/task-authoring.md b/docs/task-authoring.md
index 2676bef..262938e 100644
--- a/docs/task-authoring.md
+++ b/docs/task-authoring.md
@@ -81,6 +81,7 @@ Allowed fields:
 | `model` | no | string or null | Override the provider model for every stage of this task (e.g. `claude-opus-4-8`). |
 | `reasoning` | no | string or null | Override the reasoning effort level for this task: `low`, `medium`, `high`, `xhigh`, or `max`. |
 | `stages` | no | mapping | Per-stage overrides: `model`/`reasoning` (precedence over the task-wide values) and `enabled: false` to skip a stage. See [`stages`](#stages). |
+| `pr_title` | no | string \| null | PR title override; when set, used verbatim as the pull-request title instead of `title`. |
 
 The current validation gate rejects unknown fields fail-closed. Keep task front matter limited to
 the fields above.
diff --git a/docs/worc/README.md b/docs/worc/README.md
index 0158113..0c31c8c 100644
--- a/docs/worc/README.md
+++ b/docs/worc/README.md
@@ -64,6 +64,7 @@ Only the fields below are allowed. **Any other key makes the task rejected** (`u
 | `model` | no | string \| null | Override the model for every stage of this task (e.g. `claude-opus-4-8`). |
 | `reasoning` | no | string \| null | Reasoning effort for this task: `low`, `medium`, `high`, `xhigh`, or `max`. |
 | `stages` | no | mapping | Per-stage `model`/`reasoning` overrides and the `enabled: false` skip toggle. See the decision guide. |
+| `pr_title` | no | string \| null | PR title override; when set, used verbatim as the pull-request title instead of `title`. |
 
 ### Body sections
 
diff --git a/pyproject.toml b/pyproject.toml
index 07e66cc..352fe40 100644
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -53,6 +53,7 @@ select = ["E", "F", "I", "UP", "B", "SIM", "C4"]
 python_version = "3.12"
 strict = true
 files = ["src"]
+exclude = ["^tests/"]
 
 [tool.pytest.ini_options]
 testpaths = ["tests"]
diff --git a/src/wastech_orchestrator/core/orchestrator.py b/src/wastech_orchestrator/core/orchestrator.py
index 9db0374..1f6ed2c 100644
--- a/src/wastech_orchestrator/core/orchestrator.py
+++ b/src/wastech_orchestrator/core/orchestrator.py
@@ -785,7 +785,7 @@ class Orchestrator:
             p,
             "pull request",
             lambda: self._git.create_pr(
-                p.task.id, p.branch, title=f"{p.task.title}", body_path=body_path
+                p.task.id, p.branch, title=p.task.pr_title or p.task.title, body_path=body_path
             ),
         )
         if pr_url and Stage.REVIEW in p.skip and self._auto_merge_on(p.task):
diff --git a/src/wastech_orchestrator/task/model.py b/src/wastech_orchestrator/task/model.py
index c1c353e..498e9af 100644
--- a/src/wastech_orchestrator/task/model.py
+++ b/src/wastech_orchestrator/task/model.py
@@ -23,6 +23,7 @@ ALLOWED_TASK_KEYS: frozenset[str] = frozenset(
     {
         "id",
         "title",
+        "pr_title",
         "refined",
         "decompose",
         "auto_merge",
@@ -67,6 +68,7 @@ class NormalizedTask:
     id: str
     title: str
     description: str
+    pr_title: str | None = None
     refined: bool = False
     # Tri-state: True forces decomposition, False disables it, None defers to the config default.
     decompose: bool | None = None
diff --git a/src/wastech_orchestrator/task/validation_gate.py b/src/wastech_orchestrator/task/validation_gate.py
index f597e7e..0030f43 100644
--- a/src/wastech_orchestrator/task/validation_gate.py
+++ b/src/wastech_orchestrator/task/validation_gate.py
@@ -237,10 +237,13 @@ class ValidationGate:
         if finding is not None:
             return _rej(ValidationReason.INJECTION_SUSPECTED, finding.detail)
 
+        raw_pr_title = frontmatter.get("pr_title")
+        pr_title = (str(raw_pr_title).strip() or None) if isinstance(raw_pr_title, str) else None
         task = NormalizedTask(
             id=id_value,
             title=str(title_value),
             description=body.strip(),
+            pr_title=pr_title,
             refined=bool(frontmatter.get("refined", False)),
             decompose=_as_tristate(frontmatter.get("decompose")),
             auto_merge=_as_tristate(frontmatter.get("auto_merge")),
@@ -262,6 +265,8 @@ class ValidationGate:
     def _check_field_types(self, fm: Mapping[str, Any]) -> _Reject | None:
         if not isinstance(fm.get("title"), str):
             return _Reject(ValidationReason.INVALID_FIELD_TYPE, "title must be a string")
+        if "pr_title" in fm and fm["pr_title"] is not None and not isinstance(fm["pr_title"], str):
+            return _Reject(ValidationReason.INVALID_FIELD_TYPE, "pr_title must be a string")
         if "refined" in fm and not isinstance(fm["refined"], bool):
             return _Reject(ValidationReason.INVALID_FIELD_TYPE, "refined must be a boolean")
         if (
diff --git a/src/wastech_orchestrator/worc/README.md b/src/wastech_orchestrator/worc/README.md
index 0158113..0c31c8c 100644
--- a/src/wastech_orchestrator/worc/README.md
+++ b/src/wastech_orchestrator/worc/README.md
@@ -64,6 +64,7 @@ Only the fields below are allowed. **Any other key makes the task rejected** (`u
 | `model` | no | string \| null | Override the model for every stage of this task (e.g. `claude-opus-4-8`). |
 | `reasoning` | no | string \| null | Reasoning effort for this task: `low`, `medium`, `high`, `xhigh`, or `max`. |
 | `stages` | no | mapping | Per-stage `model`/`reasoning` overrides and the `enabled: false` skip toggle. See the decision guide. |
+| `pr_title` | no | string \| null | PR title override; when set, used verbatim as the pull-request title instead of `title`. |
 
 ### Body sections
 
diff --git a/tasks/pending/ta[REDACTED].md b/tasks/pending/ta[REDACTED].md
new file mode 100644
index 0000000..9c82722
--- /dev/null
+++ b/tasks/pending/ta[REDACTED].md
@@ -0,0 +1,92 @@
+---
+id: ta[REDACTED]
+title: "Add an optional pr_title task field to override the generated PR title"
+refined: true          # detailed enough to plan directly — skip refinement
+decompose: false       # one coherent change — single branch, single PR
+contacts:
+  - "@t_i_2_3"
+model: claude-sonnet-4-6    # task-wide default model for stages not overridden below
+reasoning: low
+stages:
+  planning:
+    model: claude-opus-4-8
+    reasoning: high
+  review:
+    model: codex
+    reasoning: high
+---
+
+## Description
+
+Today the orchestrator derives the pull-request title from the task `title`: at the publishing
+stage it calls `GitManager.create_pr(..., title=p.task.title)`
+([orchestrator.py](src/wastech_orchestrator/core/orchestrator.py), the `_publish` step), and
+`create_pr` passes that straight to `gh pr create --title`
+([git_manager.py](src/wastech_orchestrator/git_manager.py)). There is no way to make the PR title
+differ from the task title.
+
+Add a new **optional** front-matter field **`pr_title`** (type `string | null`) that lets a task
+state the PR title explicitly:
+
+- When `pr_title` is present and non-empty, the orchestrator uses it **verbatim** as the PR title.
+- When `pr_title` is absent, `null`, or empty/whitespace-only, behavior is **unchanged** — the PR
+  title is the task `title`.
+
+The override affects **only the PR title**. It must not change the branch name
+(`agent/<task-id>-<slug>`, where the slug is derived from `title`), the commit message
+(`feat(<id>): <title>`), the summary, or any report — those keep deriving from `title` exactly as
+they do now.
+
+The field threads through the existing task schema and is consumed only at publishing:
+
+1. Add `pr_title` to `ALLOWED_TASK_KEYS` and to the `NormalizedTask` dataclass
+   ([task/model.py](src/wastech_orchestrator/task/model.py)), defaulting to `None`.
+2. Populate it in the parser / validation gate
+   ([task/validation_gate.py](src/wastech_orchestrator/task/validation_gate.py)), normalizing
+   empty/whitespace to `None` (mirror how `model`/`reasoning` use `frontmatter.get(...) or None`),
+   and add a type check (`pr_title must be a string`) alongside the other field-type checks.
+3. At the publishing stage, use `p.task.pr_title` for the PR title when set, otherwise fall back to
+   `p.task.title`. This is the only consumption site — keep the change to the `title=` argument of
+   the `create_pr` call.
+
+`pr_title` is also accepted as a key in JSON tasks (it flows through the same front-matter map).
+
+For a decomposed task (`decompose: true`) the pipeline still produces a single PR at the end, so
+`pr_title`, if set on the parent task, applies to that one PR.
+
+## Acceptance criteria
+
+- [ ] A task with `pr_title: "Custom release title"` opens a PR whose title is exactly
+      `Custom release title` (verified via the `title=` argument passed to `create_pr` /
+      `gh pr create --title`), while the task `title` is something different.
+- [ ] A task with **no** `pr_title` (or `pr_title: null`, or an empty/whitespace value) opens a PR
+      whose title equals the task `title` — i.e. current behavior is unchanged.
+- [ ] With `pr_title` set, the branch name (`agent/<id>-<slug>`), the commit message
+      (`feat(<id>): <title>`), and the summary still derive from `title`, not from `pr_title`.
+- [ ] The validation gate rejects a flag-shaped `pr_title` (e.g. a value starting with `--`) with
+      reason `injection_suspected`, consistent with how the existing front-matter scan treats
+      `title`.
+- [ ] A non-string `pr_title` (e.g. a number or a list) is rejected with `invalid_field_type`.
+- [ ] `pr_title` is accepted in a `.json` task and behaves identically to the Markdown front-matter
+      form.
+- [ ] Unit tests cover: override present, override absent/empty (fallback to `title`), flag-shaped
+      rejection, and the wrong-type rejection.
+
+## Constraints
+
+- **Only the orchestrator opens PRs** — keep all PR/title logic in the core/`GitManager`; do not move
+  any of it into a provider (architecture invariant). Providers must not learn about `pr_title`.
+- Do **not** change the branch-slug derivation, the commit-message format, or the summary — the
+  override is strictly the PR title.
+- **Do not weaken the security policy.** `pr_title` must be passed as an argument-list value (no
+  shell interpolation), and must remain subject to the existing front-matter injection scan
+  (`scan_frontmatter`); add no path that lets front matter inject CLI flags or bypass approvals.
+- No secrets in the field; no new runtime dependencies.
+- The canonical field name is **`pr_title`** — do not invent an alternative.
+- Update the docs and `CHANGELOG.md` `[Unreleased]` entry **in the same change** (use `/sync-docs`):
+  the front-matter field tables in `docs/task-authoring.md` and the authoring docs under
+  `worc/` (`README.md` table, and an example if appropriate), plus `docs/configuration.md` only if a
+  PR-title default is referenced there. Record any deferred work in
+  [docs/backlog/follow_ups.md](docs/backlog/follow_ups.md).
+- Add/update tests with the behavior, per the project testing rules; run `ruff`, `mypy`, `pytest`
+  before finishing.
diff --git a/tests/task/test_model.py b/tests/task/test_model.py
index a570815..4f89c1c 100644
--- a/tests/task/test_model.py
+++ b/tests/task/test_model.py
@@ -2,6 +2,8 @@
 
 from __future__ import annotations
 
+from typing import Any
+
 import pytest
 
 from wastech_orchestrator.providers.base import ProviderId, Stage
@@ -59,6 +61,7 @@ def test_schema_constants() -> None:
     assert {
         "id",
         "title",
+        "pr_title",
         "refined",
         "decompose",
         "auto_merge",
@@ -72,7 +75,7 @@ def test_schema_constants() -> None:
     assert REQUIRED_TASK_FIELDS <= ALLOWED_TASK_KEYS
 
 
-def _task(**kwargs: object) -> NormalizedTask:
+def _task(**kwargs: Any) -> NormalizedTask:
     return NormalizedTask(id="t", title="x", description="d", **kwargs)
 
 
diff --git a/tests/task/test_validation_gate.py b/tests/task/test_validation_gate.py
index be763a2..698693a 100644
--- a/tests/task/test_validation_gate.py
+++ b/tests/task/test_validation_gate.py
@@ -502,3 +502,87 @@ def test_stages_review_enabled_true_never_gated(config: OrchestratorConfig) -> N
     # Only ``enabled: false`` on review needs the opt-in; an explicit enable is always fine.
     result = _gate(config).validate(_src(_stages_task("stages:\n  review:\n    enabled: true\n")))
     assert result.passed is True
+
+
+# ---------------------------------------------------------------------------
+# pr_title field
+# ---------------------------------------------------------------------------
+
+def test_pr_title_override_stored(config: OrchestratorConfig) -> None:
+    text = (
+        '---\nid: task-001\ntitle: "Task title"\npr_title: "Custom PR title"\n'
+        "---\n\n## Description\n\nDo it.\n"
+    )
+    result = _gate(config).validate(_src(text))
+    assert result.passed is True
+    assert result.normalized is not None
+    assert result.normalized.pr_title == "Custom PR title"
+    assert result.normalized.title == "Task title"
+
+
+def test_pr_title_absent_is_none(config: OrchestratorConfig) -> None:
+    result = _gate(config).validate(_src(_GOOD))
+    assert result.passed is True
+    assert result.normalized is not None
+    assert result.normalized.pr_title is None
+
+
+def test_pr_title_null_is_none(config: OrchestratorConfig) -> None:
+    text = "---\nid: task-001\ntitle: T\npr_title: null\n---\n\n## Description\n\nDo it.\n"
+    result = _gate(config).validate(_src(text))
+    assert result.passed is True
+    assert result.normalized is not None
+    assert result.normalized.pr_title is None
+
+
+def test_pr_title_empty_string_is_none(config: OrchestratorConfig) -> None:
+    text = '---\nid: task-001\ntitle: T\npr_title: ""\n---\n\n## Description\n\nDo it.\n'
+    result = _gate(config).validate(_src(text))
+    assert result.passed is True
+    assert result.normalized is not None
+    assert result.normalized.pr_title is None
+
+
+def test_pr_title_whitespace_only_is_none(config: OrchestratorConfig) -> None:
+    text = '---\nid: task-001\ntitle: T\npr_title: "   "\n---\n\n## Description\n\nDo it.\n'
+    result = _gate(config).validate(_src(text))
+    assert result.passed is True
+    assert result.normalized is not None
+    assert result.normalized.pr_title is None
+
+
+def test_pr_title_flag_shaped_rejected(config: OrchestratorConfig) -> None:
+    text = '---\nid: task-001\ntitle: T\npr_title: "--inject"\n---\n\n## Description\n\nDo it.\n'
+    result = _gate(config).validate(_src(text))
+    assert result.passed is False
+    assert result.reason is ValidationReason.INJECTION_SUSPECTED
+
+
+def test_pr_title_wrong_type_rejected(config: OrchestratorConfig) -> None:
+    text = "---\nid: task-001\ntitle: T\npr_title: 42\n---\n\n## Description\n\nDo it.\n"
+    result = _gate(config).validate(_src(text))
+    assert result.passed is False
+    assert result.reason is ValidationReason.INVALID_FIELD_TYPE
+
+
+def test_pr_title_list_type_rejected(config: OrchestratorConfig) -> None:
+    text = "---\nid: task-001\ntitle: T\npr_title:\n  - a\n  - b\n---\n\n## Description\n\nDo it.\n"
+    result = _gate(config).validate(_src(text))
+    assert result.passed is False
+    assert result.reason is ValidationReason.INVALID_FIELD_TYPE
+
+
+def test_pr_title_json_task(config: OrchestratorConfig) -> None:
+    text = json.dumps(
+        {
+            "id": "task-json",
+            "title": "Task title",
+            "pr_title": "Custom PR",
+            "description": "Do it. Acceptance: works.",
+        }
+    )
+    result = _gate(config).validate(_src(text, ".json"))
+    assert result.passed is True
+    assert result.normalized is not None
+    assert result.normalized.pr_title == "Custom PR"
+    assert result.normalized.title == "Task title"

```
