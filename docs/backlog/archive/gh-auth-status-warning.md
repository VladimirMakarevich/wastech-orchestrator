# `gh auth status` warning at startup

Status: **done** (2026-06-22 — see the Done entry in [archive/follow_ups_history.md](archive/follow_ups_history.md)) Date: 2026-06-22 Owner: Vladimir Makarevich

Detail file for the [follow_ups.md](follow_ups.md) item "`gh auth status` warning at startup".

## Problem

The `gh` pre-flight gate `require_gh` ([preflight.py:17-29](../../src/wastech_orchestrator/preflight.py#L17-L29)) only checks that `gh` is on `PATH` (via `has_gh` — [detect.py:110-115](../../src/wastech_orchestrator/install/detect.py#L110-L115)). It does **not** check that `gh` is authenticated. A logged-out `gh` passes the gate and then fails far downstream at `gh pr create` inside the publish stage, surfacing as an opaque `GitCommandError` → `manual_action_required` after the whole pipeline has already run. The operator gets the bad news at the most expensive possible moment.

## Goal

A **non-blocking** warning at `run` / `watch` / `rerun` startup: when `gh` is present but not authenticated, print/log "gh present but not logged in — PR creation will fail at publish; run `gh auth login`". It must never raise and never block the run (a valid `GH_TOKEN` in the environment, or a transient probe failure, must not stop a run). `require_gh` stays the hard `PATH` gate; this is the soft auth advisory layered on top.

## Design

- **Read-only probe in `detect.py`.** Add `gh_auth_ok() -> bool | None`, mirroring `_run_git` ([detect.py:40-60](../../src/wastech_orchestrator/install/detect.py#L40-L60)): run `gh auth status` through the safe `run_process` (operator-side, `dict(os.environ)` so an env `GH_TOKEN`/`GITHUB_TOKEN` is honored — `gh auth status` already accounts for env tokens, which is exactly why it is the right probe). Return `True` on exit 0 (authenticated), `False` on non-zero, `None` on launch failure / timeout (unknown — `gh` missing is `require_gh`'s job, not this one).
- **Advisory gate in `preflight.py`.** Add `warn_if_gh_logged_out(emit)` (or return a message the caller logs): if `has_gh()` and `gh_auth_ok()` is `False`, emit the generic warning. `None` (unknown) → no warning. Never raises.
- **Call sites.** Immediately after each `require_gh()` call, still under `if config.git.create_pull_request:` — [cli.py:616-617](../../src/wastech_orchestrator/cli.py#L616-L617) (`cmd_run`), [cli.py:707-708](../../src/wastech_orchestrator/cli.py#L707-L708) (`cmd_rerun`), [cli.py:948-949](../../src/wastech_orchestrator/cli.py#L948-L949) (`cmd_watch`).

## Security

`gh auth status` writes the account login (and sometimes token scopes) to stderr. Do **not** surface the raw process output — emit our own fixed message. The probe runs through the safe `run_process` (no shell, fixed argv); its stdout/stderr are not propagated into logs or artifacts. Consistent with the "no secrets in logs/SQLite/artifacts" invariant.

## Tests

- Fake runner exit 0 → authenticated → no warning.
- Fake runner non-zero → logged out → warning emitted (once per startup, generic text, no raw `gh` output).
- Fake runner launch failure → unknown → no warning, no raise (and `require_gh` independently handles the missing-binary case).
- The warning never changes the exit code / never blocks the run.

## Docs

- `docs/operations.md` — the startup / pre-flight section: note the new non-blocking auth advisory alongside the existing `require_gh` hard gate and the `gh auth login` prerequisite.

## Out of scope

- A **hard** auth gate. A transient `gh auth status` failure (or env-token auth that the probe can't see) must not block a run; keep it advisory only.
- Re-checking auth mid-run or per-PR. One startup advisory is enough; the real failure path (`gh pr create`) already degrades to `manual_action_required` safely.
