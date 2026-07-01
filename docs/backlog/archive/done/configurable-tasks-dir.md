# Configurable tasks directory

Status: **implemented** (2026-06-26) Date: 2026-06-25 Owner: Vladimir Makarevich

The tasks directory (`tasks/`) — where pending, processing, done, and failed task files live — is currently hardcoded across five modules. Operators cannot rename it or place it at a different path within the repo, which causes friction when a target project already uses `tasks/` for something else or when team conventions call for a different name (e.g. `.tasks/`, `worktasks/`). This document records the design decision for making the directory name and relative path configurable.

## The problem

The string `"tasks"` appears as a hardcoded literal in `cli.py` (`REPO_TASK_DIRS`, `tasks_root_for`, `pending_dir`), `orchestrator.py` (`_LIFECYCLE_FOLDERS` root resolution), `git_manager.py` (`EXCLUDED_DIRS`), and `install/config_writer.py`. An operator who installs the orchestrator into a repo that already has a `tasks/` directory (common in many frameworks) gets an immediate collision with no escape hatch — the only fix today is to patch the orchestrator source.

## Constraints

The tasks directory must always be inside the repo root (a relative path). The audit commit in `git_manager.commit_audit` stages files under `tasks/{state}/{task_id}.md` and requires git to track them; a path outside the working tree would silently break the audit trail without any error from git. Path traversal (`../`) is not permitted — the value is validated at config load time.

The lifecycle subfolder names (`pending`, `processing`, `done`, `failed`) are not part of this change; they remain hardcoded.

## Alternatives considered

| Option | Why rejected |
| --- | --- |
| Rename `tasks/` → `.tasks/` globally (new hardcoded default) | Gives zero flexibility for future name conflicts; operators with existing setups would still be stuck. |
| Leave `tasks/` as-is, solve by documentation/convention | Does not help operators with a pre-existing `tasks/` directory. |
| Allow path outside repo root (absolute or `../` relative) | Breaks the git audit trail: `git ls-files` and `git add` operate on the working tree only. |
| Add `tasks_tracked: bool` config key for gitignore mode | Unnecessary: adding `tasks/` (or whatever the configured name is) to `.gitignore` already works transparently today — see § Git-ignore behaviour below. |

## Decision

Add `paths.tasks_dir` to `config.yaml` with a default of `"tasks"`. The value is a path relative to the repo root, no `../` allowed, no absolute paths. The `worc install` interactive prompt asks for the directory name (default `tasks`) and writes the value into `config.yaml`. All modules that currently hardcode `"tasks"` are updated to read the configured value at startup.

The lifecycle subfolder names (`pending` / `processing` / `done` / `failed`) are not configurable — only the root directory.

Cost of not choosing this: operators who hit the collision must either fork the config or keep a parallel directory structure that confuses tooling.

## Git-ignore behaviour (no config key needed)

If the operator adds the tasks directory to `.gitignore` (e.g. to keep task files out of version history), the orchestrator degrades gracefully without any extra configuration:

- `_relocate_task_file` uses a plain `src.replace(dest)` — filesystem only, no git involved.
- `commit_audit` in `git_manager.py` calls `git add -A -- tasks/{state}/...`. When the path is gitignored, git returns a non-zero exit code; the surrounding guard (`if stageable and self._git("add", ...).ok:`) catches this silently — `sha` is `None`, the op is recorded as `"noop"`.
- `state.db` audit is unaffected — it lives in `.worc/` and tracks every lifecycle transition regardless of git.

**The operator just adds the directory to `.gitignore` — no orchestrator config key is needed.** The only observable difference is that the lifecycle transitions no longer appear in git history.

## Open questions (resolved at implementation)

- **Subdirectories?** — **Resolved: relative subpaths are allowed.** `paths.tasks_dir` accepts any repo-relative path (e.g. `tasks`, `.tasks`, `worktasks`, `config/tasks`), validated with the existing `is_safe_relpath` helper (no absolute, no `~`, no `..`). One extra guard: the value must not equal or live under `.worc/`, because that home is gitignored and would silently break the audit trail. The orchestrator's `_relocate_task_file` / `_resolve_task_source` already derive the tasks root structurally (walking up from the lifecycle folder), so subpaths need no extra code there.
- **Install UX / `--reconfigure`?** — **Resolved: config-only, no prompt and no CLI flag.** `worc install` always writes the default `paths.tasks_dir: tasks` and scaffolds `tasks/`. To use a different directory, the operator edits `config.yaml` and creates the lifecycle subfolders by hand. This keeps the installer's spec-driven, non-interactive shape; `--reconfigure` regenerates the default like every other value.

## Implementation notes

Modules and seams to update:

- `config/schema.py` — add a `PathsConfig` dataclass with `tasks_dir: str = "tasks"` and wire it into the top-level `OrchestratorConfig`.
- `config/loader.py` — read `paths.tasks_dir`, validate no `../` and no leading `/`, bump config version if needed.
- `install/config_writer.py` — add interactive prompt for directory name during `worc install`; write `paths.tasks_dir` into the generated `config.yaml`.
- `cli.py` — replace the `REPO_TASK_DIRS` tuple literal and the `tasks_root_for` / `pending_dir` helpers with values derived from the loaded config.
- `core/orchestrator.py` — `_relocate_task_file` resolves the tasks root from `_LIFECYCLE_FOLDERS` by walking `parent.parent`; no structural change needed, but the root must be consistent with the config value passed to the orchestrator at construction.
- `git_manager.py` — `EXCLUDED_DIRS` is a module-level constant today; make it instance-level (or pass the configured name at construction) so the code commit exclusion matches the configured directory name.
