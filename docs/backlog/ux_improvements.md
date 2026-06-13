# Backlog: UX improvements (stop/restart, WAL artifacts, gh check, worc alias)

Status: **backlog / not scheduled**
Date: 2026-06-13
Owner: Vladimir Makarevich

Four small, independent improvements that reduce friction when operating the orchestrator
day-to-day. None of them touches the Core pipeline, provider interface, or security policy.
Each section is a self-contained unit of work and can be scheduled independently.

---

## 1. `stop` and `restart` commands for the `watch` loop

### Problem

`watch` runs as a blocking polling loop (`cli.py:416 watch_loop`) and the only way to stop it is
`Ctrl-C` or `kill <PID>` from another terminal. There is no `wastech-orchestrator stop` and no
`wastech-orchestrator restart` for operators running the orchestrator as a background service or
via a process manager (systemd, launchd, `nohup &`).

### Proposed design

**PID file** — on `watch` startup write `<artifacts_root>/orchestrator.pid` containing the process
PID. Remove it on clean exit (including `KeyboardInterrupt`). This is the signalling target.

```
wastech-orchestrator stop   # send SIGTERM to PID in orchestrator.pid; wait for clean exit
wastech-orchestrator restart # stop (SIGTERM + wait) then exec watch with the same args
```

**`stop` command**

1. Read `<artifacts_root>/orchestrator.pid`.
2. If the file is absent or the PID is not running: print a warning and exit 0 (idempotent).
3. Send `SIGTERM`; wait up to a configurable timeout (default 30 s) for the process to exit.
4. If still alive after timeout: send `SIGKILL`, warn the operator.
5. Remove the PID file.
6. Exit 0 on success.

**`restart` command**

Equivalent to `stop` followed by `exec watch [same args]`. Because the watch loop is the only
long-running command, restart is just a convenience wrapper — it does not need to remember the
original `watch` arguments; the operator provides them as usual
(`wastech-orchestrator restart --poll-interval 10`).

**Graceful shutdown in `watch_loop`** — `watch_loop` already exits cleanly on `KeyboardInterrupt`.
The SIGTERM handler needs to be wired the same way: set a `threading.Event` that the loop checks
between ticks, so an in-progress task run is not interrupted mid-stage.

### Implementation notes

- PID file location: `<artifacts_root>/orchestrator.pid` (excluded from git commits via
  `_EXCLUDED_FILE_PREFIXES` or an explicit `_EXCLUDED_FILES` addition in `git_manager.py:55`).
- If two watch processes start for the same artifact root, the second should detect a live PID
  file and refuse to start (prints an error with the path; operator can `stop` first).
- `stop` and `restart` are thin CLI commands; the PID-file logic lives in a small
  `cli/process_control.py` module, not in the Core.

### Related

- `cli.py` `watch_loop` (`cli.py:416`), `cmd_watch` (`cli.py:518`).
- [[auto_merge_bypass]] — restart must not re-trigger an already-fired auto-merge.
- [[session-persistence]] (follow_ups.md) — restart loses in-memory session IDs; this is
  the existing known gap.

---

## 2. Suppress `state.db-shm` / `state.db-wal` from `git status` output

### Problem

SQLite WAL mode creates two journal sidecar files (`state.db-wal`, `state.db-shm`) next to
`state.db` while the database is open. These are **already excluded from orchestrator-managed
commits** via `git_manager.py:55`:

```python
_EXCLUDED_FILES = ("state.db", "state.db-wal", "state.db-shm", "config.yaml")
```

However they are **not listed in the repository's `.gitignore`**, so `git status` in the target
repo (in-repo footprint mode) shows them as untracked files. This is noisy and confusing —
operators see apparent "dirty" state that the orchestrator itself doesn't care about.

There is already a related follow-up in `follow_ups.md` (2026-06-12, "`.gitignore` for in-repo
runtime files") covering `state.db` and `config.yaml`; the WAL/SHM sidecars are a natural
extension of the same fix.

### Proposed design

Extend the `init`/`install` command's setup step (currently `cli.py cmd_init`/
`install/wizard.py`) to append the full set of orchestrator runtime files to the repository's
`.gitignore` (or `.git/info/exclude` for per-clone exclusion without touching the tracked
`.gitignore`):

```gitignore
# wastech-orchestrator runtime files (auto-appended by `worc init`)
state.db
state.db-shm
state.db-wal
config.yaml
config.yaml.bak-*
orchestrator.pid
```

The append must be idempotent (check before writing) and must preserve any existing `.gitignore`
content. Use `git/info/exclude` as the default to avoid polluting the target repo's tracked
`.gitignore` with tool-specific entries — offer `.gitignore` as an opt-in flag
(`--gitignore-tracked`).

### Implementation notes

- Check whether each line is already present before appending (idempotent across re-runs of
  `init`).
- `orchestrator.pid` is also a candidate for exclusion (see item 1).
- External-footprint mode does not write into the clone at all, so this only matters for in-repo
  footprint.

### Related

- `git_manager.py:55` `_EXCLUDED_FILES` — the source of truth for what is excluded from commits.
- `follow_ups.md` 2026-06-12 "`.gitignore` for in-repo runtime files" — this item supersedes
  that entry and broadens the scope to WAL sidecars.

---

## 3. Pre-flight `gh` CLI check at startup / publish stage

### Problem

`git_manager.py` calls `gh pr create` (and will call `gh pr merge` after [[auto_merge_bypass]] is
implemented) via `_gh()` (`git_manager.py:193`). If `gh` is not on `PATH`, the failure surfaces
as a raw `GitCommandError` from `_run()` deep inside the publish stage — confusing for operators
who don't immediately associate "command not found" with a missing `gh` installation.

By contrast, the install wizard already does a soft check: `detect.has_gh()` (
`install/detect.py:111`) and sets the PR-creation default based on the result. But this check
does not run at startup or before the publish stage — it only runs interactively during `install`.

### Proposed design

Add a **hard pre-flight gate** that fires before the publish stage (or at `watch`/`run` startup):

```
if config.publishing.enabled and shutil.which("gh") is None:
    raise EnvironmentError(
        "'gh' (GitHub CLI) is not installed or not on PATH. "
        "Install it from https://cli.github.com/ and run 'gh auth login' before starting."
    )
```

This mirrors the existing agent-CLI check in `install/wizard.py:164`:

```python
"no agent CLI found on PATH (codex / claude); install at least one, or pass …"
```

**Where to add the check**:

- `install/detect.py` already has `has_gh()` and `find_executable()` — extend with a
  `require_gh()` helper that raises a structured `EnvironmentError` / `InstallError`.
- Call `require_gh()` in `cmd_watch` (and `cmd_run` if it exists) before starting the loop, so
  the operator gets a clear error message immediately — not 10 minutes into a task run when
  publishing fires.
- Optionally skip the check when `config.publishing.enabled = false` (PR creation is disabled).

**Auth check** — `gh auth status` returns non-zero when not logged in. Consider a secondary check
that warns (but does not block) when `gh` is present but not authenticated, to surface a common
misconfiguration early.

### Implementation notes

- Keep `has_gh()` (non-raising) for the install wizard's default-setting logic.
- Add `require_gh()` (raising) as a strict variant used at startup.
- The check must go through the existing safe process runner (no raw `subprocess` shell calls).
- Unit test: monkeypatch `shutil.which` to return `None`; assert startup raises with a
  human-readable message.

### Related

- `install/detect.py:111` `has_gh()` — existing soft check.
- `install/wizard.py:164` — model for the agent-CLI hard check.
- `git_manager.py:193` `_gh()` — the call site that would fail without the pre-flight gate.
- [[auto_merge_bypass]] — `gh pr merge` is an additional `gh`-dependent operation planned there.

---

## 4. Short `worc` alias for the `wastech-orchestrator` command

### Problem

`wastech-orchestrator` is 22 characters and awkward to type in day-to-day use
(`wastech-orchestrator watch`, `wastech-orchestrator status`, …). A short alias reduces friction
significantly.

### Proposed design

Add a second `console_scripts` entry in `pyproject.toml`:

```toml
[project.scripts]
wastech-orchestrator = "wastech_orchestrator.cli:main"
worc = "wastech_orchestrator.cli:main"
```

Both names point to the same `main` entry point — no new code, just a second installation
symlink. `pip install -e .` (and the published package) will register both.

### Considerations

- **Name collision** — `worc` is short and could conflict with other tools on some systems.
  Verify that it is not a known package name on PyPI before releasing. Alternative short forms:
  `worch`, `worchestrator` (still long), or a configurable alias.
- **Docs and README** — update examples to show `worc` as the primary short form, with
  `wastech-orchestrator` noted as the canonical long form.
- **Shell completions** — if completions are added in the future, both entry points need them.
- **Deprecation path** — `wastech-orchestrator` must never be removed; it is the canonical
  name used in docs, CI scripts, and package metadata.

### Implementation notes

- One-line change to `pyproject.toml:30`.
- Update `README.md`, `docs/operations.md`, and any quick-start guides to show `worc`.
- Add `worc` to the `_EXCLUDED_FILES`-style list if a PID file per binary name is used (item 1).

### Related

- `pyproject.toml:30` — current single entry point.
- `README.md` / `docs/operations.md` — docs to update.

---

## Implementation priority (suggested)

| Item | Effort | Impact | Notes |
|---|---|---|---|
| 4 — `worc` alias | Trivial (1 line) | High daily UX | Do first; zero risk. |
| 2 — WAL/SHM gitignore | Small | Medium | Extends existing backlog item. |
| 3 — `gh` pre-flight check | Small | Medium | Prevents confusing late failures. |
| 1 — `stop` / `restart` | Medium | Medium | Only matters when running as a service. |
