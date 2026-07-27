# Codex canary: the throwaway `CODEX_HOME` disables Codex on native Windows

Status: **accepted** Date: 2026-07-26 Owner: Vladimir Makarevich

A blocking defect, not a deferred feature. On native Windows the WRI-003 permission-profile canary runs every probe under a throwaway `CODEX_HOME`. The Codex Windows sandbox keeps its capability-SID and account state in `CODEX_HOME`, so an empty home has no grants and cannot create them without the elevated backend — **every probe is denied, including the positive control**. The canary therefore fails on a host where the profile is in fact enforced, and because the same canary gates every `codex exec` in [`_pre_launch_check`](../../src/wastech_orchestrator/providers/codex.py#L653), Codex is effectively unusable: each attempt raises `CAPABILITY_UNAVAILABLE` and the Router falls over to Claude.

Found while investigating a `worc preflight` warning on the `WastimeApp` target:

```
codex: WARN — isolation smoke: codex workspace-write sandbox: permission-profile canary could not
demonstrate the requested policy: 'private-read-allowed' (a required read) was blocked on this host
(alias probe skipped: host could not create a symlink fixture) (a fallback provider will cover)
```

The message is misleading twice over: the probe named is simply the first in the set, and nothing was "blocked by policy" — the sandbox never got a usable grant.

## Verified evidence

Host: Windows 10 Pro 19045, `codex-cli 0.144.4` (npm package), orchestrator `0.10.3a2.dev30+g5d22d96af`, config `security.disable_read_isolation: true`, `codex.command` pointing at the npm vendored `…\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe`.

Same binary, same generated profile, same fixture — only `CODEX_HOME` differs:

| `CODEX_HOME` | profile shape | repo read (positive control) | `.worc` read |
| --- | --- | --- | --- |
| real `~/.codex` | `.worc = read` (read-isolation OFF) | `rc=0` | `rc=0` |
| real `~/.codex` | `.worc = deny` (read-isolation ON) | `rc=0` | `rc=0` — **deny not enforced**, see below |
| throwaway (empty) | `.worc = read` | `Access is denied.` | `Access is denied.` |
| throwaway (empty) | `.worc = deny` | `windows sandbox failed: Restricted read-only access requires the elevated Windows sandbox backend` | same |

The full preflight battery, run with the real home substituted for the throwaway one (everything else untouched), **passes**:

```
--- read_isolation_off=True: passed
    {'probe': 'private-read-allowed',       'expect_denied': False, 'denied': False}
    {'probe': 'private-shell-read-allowed', 'expect_denied': False, 'denied': False}
    {'probe': 'private-write-denied',       'expect_denied': True,  'denied': True}
    {'probe': 'repo-read-allowed',          'expect_denied': False, 'denied': False}
    {'probe': 'repo-write-allowed',         'expect_denied': False, 'denied': False}
    {'probe': 'exchange-read-allowed',      'expect_denied': False, 'denied': False}
    {'probe': 'exchange-write-denied',      'expect_denied': True,  'denied': True}
    {'probe': 'mcp-inventory',              'clean_exit': True,     'empty': False}
```

So the profile the orchestrator generates **is** OS-enforced on this host for the configured (read-isolation OFF) shape. Only the canary's own home substitution makes it look otherwise.

### What lives in `CODEX_HOME` on Windows

- `cap_sid` — capability SIDs, keyed per workspace path (`workspace_by_cwd`, `writable_root_by_path`). The grant mechanism itself.
- `.sandbox/setup_marker.json` — the one-time setup record naming the `CodexSandboxOffline` / `CodexSandboxOnline` local accounts (created 2026-03-08 on this host; both still present and enabled).
- `.sandbox-bin/` — the copied `codex-command-runner-<ver>.exe` launched via `CreateProcessWithLogonW`.
- `.sandbox-secrets/sandbox_users.json` — credentials for those accounts.
- `.sandbox/sandbox.<date>.log` — the sandbox's own trace, the primary diagnostic for this class of failure.

With an empty home Codex does not even attempt a setup refresh (no `setup refresh: spawning …` line appears in the log) and goes straight to a launch whose token has no grant for the workspace.

## Root cause

[`codex_canary.py:320`](../../src/wastech_orchestrator/providers/codex_canary.py#L320):

```python
with tempfile.TemporaryDirectory(prefix="worc-codexhome-") as codex_home:
    probe_env = {**dict(env), "CODEX_HOME": codex_home}
```

Introduced by the hardening documented in the function's own docstring — "the probes run under a throwaway `CODEX_HOME` so the operator's `~/.codex/config.toml` cannot alter profile resolution". Correct intent, and correct on POSIX, where the seatbelt/Landlock backends keep no state in `CODEX_HOME`. On Windows it strips the sandbox's entire grant substrate.

The same substitution is applied to the MCP inventory probe in [`_inventory_via_runner`](../../src/wastech_orchestrator/providers/codex_canary.py#L414) — there it is harmless and should stay (no sandbox is involved; it is a pure config-neutralization concern).

### There is nothing to revert

`codex_canary.py`, `codex_profile.py` and `_pre_launch_check` were **all introduced** by 61ef90f5 (`Feat/agent worc read isolation`, #39, 2026-07-25) — confirmed with `git log --diff-filter=A`. There is no earlier version of this code to roll back to; Codex ran without a pre-launch canary before that PR, which is why the last successful Codex runs on this host date to 2026-07-22/23. Reverting #39 would also drop WRI-001/002/003/009/010/011, `runtime_layout`, the frozen bundles and the whole Claude isolation half. The fix is a scoped edit, not a revert.

## Proposed direction

1. **Do not substitute `CODEX_HOME` for the sandbox probes on Windows.** Keep the throwaway home on POSIX. Sketch:

   ```python
   # Windows: the sandbox keeps its capability-SID + account state in CODEX_HOME; a throwaway home
   # has none and cannot create it without the elevated backend, so every grant is denied. The
   # inline `-c permissions.worc={...}` still wins over anything the operator's config.toml defines.
   if system == "Windows":
       probe_env = dict(env)
       ...  # run the probe loop
   else:
       with tempfile.TemporaryDirectory(prefix="worc-codexhome-") as codex_home:
           probe_env = {**dict(env), "CODEX_HOME": codex_home}
   ```

   The H4 guarantee survives in substance: the profile is passed inline via `-c permissions.<name>={…}` and selected with `-P`, and a CLI `-c` override wins over the file config. Document that explicitly where the guarantee is claimed, so the Windows exception is a recorded decision rather than a silent weakening.

2. **Keep the throwaway home for `codex mcp list`** ([`_inventory_via_runner`](../../src/wastech_orchestrator/providers/codex_canary.py#L414)). With the real home the inventory reports the operator's own MCP servers (`MCP inventory NOT confirmed empty` in the run above) and the tool-surface check loses its meaning.

3. **Add the two unrecognized Windows messages to [`_CAPABILITY_MARKERS`](../../src/wastech_orchestrator/providers/codex_canary.py#L61)**: `restricted read-only access requires the elevated windows sandbox backend` and `helper copy failed`. Today neither matches, so a sandbox that could not start is reported as "a required read was blocked" — and, worse, on the read-isolation-ON shape every `expect_denied` probe "passes" for the wrong reason (the sandbox failed to start, so the command failed). Only the positive control keeps that from certifying a non-enforcing profile; the classification should not depend on that safety net alone.

### Rejected alternative (tested)

Seeding the throwaway home with the sandbox state instead of dropping the substitution — hardlinking `cap_sid`, `.sandbox/`, `.sandbox-bin/`, `.sandbox-secrets/` and `.codex-global-state.json`, deliberately without `config.toml`. **Does not work**: Codex skips the setup refresh entirely in such a home and every probe still returns `Access is denied.`. It would also mean copying sandbox account credentials into a temp directory, which is worse than the problem it solves.

## Secondary finding: `deny` is not enforced on this host

Row 2 of the table: with the real home and the read-isolation-**ON** profile (`.worc = deny`), reading the private file **succeeded**. `~/.codex/.sandbox/deny_read_acl_state.json` is `{"principals": {}}`, and Codex's own message for the restricted-read shape is `Restricted read-only access requires the elevated Windows sandbox backend`. So on a non-elevated Windows host, read-deny rules are either inert or refuse to start, depending on the code path.

Consequence, once fix (1) lands: with `security.disable_read_isolation: true` (the configuration in use) the smoke reports `passed`; flipping read isolation back **on** makes the canary return `policy-failed` → `CONFIGURATION_ERROR` → Codex blocked with no fallback. That is the correct fail-closed outcome, but it must be a *documented* one: on native Windows without the elevated sandbox backend, Codex cannot be run under read isolation. Worth stating in the operations docs next to the `disable_read_isolation` escape hatch rather than leaving an operator to discover it as a hard stop mid-run.

## Reproduction

From a repo with `.worc/` installed, with `<CMD>` = the value of `agents.providers.codex.command`:

```python
import os
from pathlib import Path
from wastech_orchestrator.providers.codex_canary import default_canary_runner, run_codex_capability_smoke

def real_home_runner(argv, cwd, env):           # simulates the proposed fix
    return default_canary_runner(argv, cwd, {**dict(env), "CODEX_HOME": str(Path.home() / ".codex")})

for runner in (default_canary_runner, real_home_runner):
    r = run_codex_capability_smoke(command=r"<CMD>", home_dir=Path.home(), env=dict(os.environ),
                                   permission_profile="workspace-write", runner=runner,
                                   read_isolation_off=True)
    print(runner.__name__, "->", r.status, "|", r.detail)
```

Expected today: `default_canary_runner -> unsupported`, `real_home_runner -> passed`. `~/.codex/.sandbox/sandbox.<date>.log` carries the sandbox-side trace for both.

## Acceptance criteria

- On native Windows, `worc preflight` reports `codex: isolation smoke OK` on a host where the profile is enforced, instead of the current `WARN`, and a real `codex exec` node runs on Codex rather than falling over to Claude.
- The throwaway `CODEX_HOME` still isolates the operator's `config.toml` on POSIX, and still applies to the MCP inventory probe on every platform.
- A sandbox that cannot start is classified `CAPABILITY_UNAVAILABLE` with a message naming the host limitation — never reported as a policy-level "required read was blocked", and never able to make `expect_denied` probes look enforced.
- A genuine leak is still `CONFIGURATION_ERROR` and still non-fallback; the read-isolation-ON shape on a non-elevated Windows host lands there, by design.
- Deterministic unit tests with an injected runner assert the env each probe receives: `CODEX_HOME` unchanged for the sandbox probes with `system="Windows"`, substituted with `system="Linux"`/`"Darwin"`, and always substituted for the inventory probe. New markers covered by classifier tests using the verbatim Codex strings.
- Docs updated in the same change: the WRI-003 hardening note that claims the throwaway home, [docs/operations.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/operations.md) (Windows + read-isolation limitation), and the shipped `packaged/guide/` copy if it repeats the claim.
- `/run-checks` green.

## Out of scope

- Making read-deny work on Windows. That needs Codex's elevated sandbox backend and is upstream; here it is only classified and documented.
- The alias probe (`alias probe skipped: host could not create a symlink fixture`) — a separate, cosmetic gap: unprivileged Windows cannot create the symlink fixture, which is already reported rather than silently passed.
- The Windows sandbox-helper discovery path. Verified working as designed: [`resolve_codex_resources_dir`](../../src/wastech_orchestrator/providers/codex.py#L265) resolves `codex-resources` from `codex.command` and [`_augment_child_env`](../../src/wastech_orchestrator/providers/codex.py#L740) prepends it to the child `PATH`. (Operator-environment note, not an orchestrator defect: on this host bare `codex` on `PATH` resolves to the app-managed standalone build `0.142.5` under `~/.codex/packages/standalone/current/bin/`, whose package root has **no** `codex-resources` — so a config using `command: codex` would fail helper discovery and the sandbox would report `program not found` / `CreateProcessWithLogonW failed: 2`. The configuration in use points at the npm vendored binary and is unaffected.)
