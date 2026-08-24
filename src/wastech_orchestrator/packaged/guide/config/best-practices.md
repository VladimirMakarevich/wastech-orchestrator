# Best practices for `config.yaml`

Read [README.md](README.md) first. This file is about keeping a project-specific orchestrator config safe, understandable, and easy to maintain.

## 1. Start minimal

Prefer the smallest config that can run safely:

- one `primary` provider;
- a deliberate `strict_isolation` — `install` writes `false`, which **is** the advanced mode rather than a milder sandbox; set `true` if you want the fail-closed one;
- `security.protected_paths` naming the repo's sensitive surfaces — the default `trust_level: auto` raises the approval gate on nothing else, and the default empty list is no floor at all;
- `auto_merge: false`;
- Telegram disabled until a human-in-the-loop path is really needed;
- one or a few clear `checks.command_sets`.

Every extra knob increases the chance that a later operator misunderstands why it is there.

## 2. Keep one clear provider story

`agents.allowed` should describe reality, not aspiration.

- If the team only uses Codex today, allow only `codex`.
- If both CLIs are installed and intentionally supported, allow both.
- Exactly one provider is `primary: true`; make that the provider the team expects most nodes to run on.

Do not add a provider "for later" if preflight would fail on every machine today. A provider in `allowed` whose CLI reports no stored credentials is fatal in any role: it fails preflight, and `run` / `rerun` / `watch` refuse to start at all — whether or not any node routes to it.

## 3. Design check sets around change ownership

The biggest config quality lever is usually `checks.command_sets`.

- Single-project repo: one catch-all set is fine.
- Monorepo: split by real path ownership, not by wishful architecture.
- Shared root files (`package.json`, `pyproject.toml`, lockfiles, CI config) deserve explicit coverage.
- When in doubt, prefer a catch-all set over accidental gaps.

Keep command names obvious (`tests`, `lint`, `types`, `ios-tests`) so logs stay readable.

## 4. Treat `skip_if_unavailable` as an exception

Use `skip_if_unavailable: true` only for toolchains that are genuinely optional on the current host, such as iOS checks on a Linux machine.

Do not use it to paper over a broken default developer environment. If a check is required for safe delivery, let it fail loudly.

## 5. Keep secrets and machine-local data out of the file

- Secret values belong in environment variables or `.worc/.env`, never in YAML.
- `allowed_environment` should name variables, not embed their contents. Prefer a prefix pattern (`DOTNET_*`) over guessing a toolchain's ten variable names one at a time, and read back what it matched in `worc preflight` before relying on it. In strict mode, a prefix match alone never forwards a name loaded from `.worc/.env` to agent-side children; an exact entry is the explicit grant. In advanced mode the list gates only orchestrator-owned `git`/`gh`, while agent-side children receive the parent environment minus all `.worc/.env` names.
- `extra_environment` is the one place a **value** enters the config, and it is in plaintext: put toolchain roots and cache paths there (`NUGET_PACKAGES`, `npm_config_cache`) and never a credential. Nothing can check a value for secrecy, so this rule is a contract, not a gate — the load-time refusal only covers secret-looking *names*. Agent/check/tool children receive these; orchestrator-owned `git`/`gh` receives them only after a whitelist over the `GIT_*`/`GH_*`/`GITHUB_*` namespace — `GIT_CONFIG_GLOBAL` and the two token names pass, everything else in those namespaces is dropped, including a name a future release invents. `worc preflight` prints names, never values, and the agent CLIs get their own credentials from their own stores, so a token is never needed here.
- Avoid host-specific absolute paths unless the orchestrator really runs on a single fixed machine.
- Leave `agents.providers.claude.allow_native_memory` off (its default) unless you deliberately accept the risk: turning it on lets Claude's own auto-memory write to a HOME store that is **outside** the orchestrator's redaction net and audit trail. It is off by default and `install` never writes it — enable it only as a conscious choice.

If several operators share the same repo, a config with fewer machine assumptions survives longer.

## 5a. Redirect toolchain caches into the clone

Under strict isolation, `dotnet build`, `cargo build` and `npm ci` can fail on a `workspace-write` node for a reason that has nothing to do with the tool being allowed: they write to `~/.nuget/packages`, `~/.cargo`, `~/.npm`, while the sandbox permits writes only inside the clone. Point those caches at the clone and the problem disappears — the clone is writable, and the orchestrator never runs `git clean`, so the cache survives from one task to the next instead of being re-downloaded. Advanced mode permits writable paths outside the clone, so this relocation is optional there.

```yaml
security:
  extra_environment:
    NUGET_PACKAGES: "/abs/path/to/repo/.toolcache/nuget"
    CARGO_HOME: "/abs/path/to/repo/.toolcache/cargo"
    npm_config_cache: "/abs/path/to/repo/.toolcache/npm"
    GOMODCACHE: "/abs/path/to/repo/.toolcache/go"
```

`.toolcache/` is this guide's suggested name and nothing more — any path inside the clone works. Write the path out in full: there is no `{repo}` substitution, so it has to match `repo.local_path`, and a mismatch is what `worc preflight` warns about.

You do not have to add the cache to `.gitignore`. For a path inside the clone the orchestrator adds the exclusion itself, to the clone-local `.git/info/exclude` — untracked, so it never appears in a task's diff or its pull request, and out of reach of an agent that can rewrite `.gitignore` but cannot write inside `.git`. The exclusion follows the values you assign, one rule per path, so a cache you point somewhere else — on a command line, in a `.npmrc`, through a variable you did not put in `extra_environment` — is not covered: assign every cache you actually redirect. `worc preflight` reports `assigned-paths: OK` once git actually ignores the path, and FAIL if something in the repository's own ignore rules still exposes it. Each task run repairs the same rule at branch preparation, so a cache path you add after your last preflight is still covered. That FAIL is worth having: an un-ignored cache puts thousands of files into the next task's diff and trips a review gate that has nothing to do with caches, after the agent has already done its expensive work.

Three limits to know before relying on this:

- **Not under `.worc`** — nor `.git`, `.worc-io`, the tasks directory, or anything `denied_read_paths` covers. Those hold the orchestrator's own state, and a build writing into them corrupts the run or the repository. A value that overlaps one in either direction is refused when the config loads; `worc preflight` additionally refuses a symlink, or a Windows case/UNC alias, that reaches one — that check needs the filesystem, which is why it lives there and not at load time.
- **A strict-mode `read-only` node cannot use an in-clone writable cache,** even when its provider gives it a shell: the clone remains read-only. In advanced mode a `read-only` node may write outside the clone, so the strict-mode recipe and warning do not apply.
- **The cache grows without bound.** Nothing prunes it — not the `.worc/runs` retention, not task cleanup — because deleting a toolchain's cache is more dangerous than occupying disk. When gigabytes go missing, look here first.

A path outside the clone is a warning rather than a refusal only under strict isolation: it may be exactly what you meant, but a strictly sandboxed node cannot write there, and the failure it produces reads like a broken toolchain instead of a misplaced cache. Advanced mode deliberately emits no such warning because outside-clone writes are part of that mode.

## 6. Default to conservative Git behavior

The safe default publish shape is:

- create a PR;
- do not auto-merge it;
- keep the audit commit on the task branch.

Move away from that only when the repository already has branch protection and required checks enforcing the same bar.

## 7. Preserve intent when you change defaults

When you deviate from the shipped defaults, keep the reason easy to recover:

- add a short YAML comment for unusual settings;
- keep command-set names descriptive;
- group related settings together instead of scattering overrides.

That matters because `upgrade-config` preserves values but re-emits the file and strips inline comments when it rewrites it.

## 8. Re-run preflight after every meaningful config edit

`worc preflight` is the fastest way to catch:

- provider binaries missing from `PATH`, and any allowed provider whose CLI reports no credentials;
- invalid or unsafe provider settings;
- `gh` missing from `PATH` while `git.create_pull_request` is on;
- Telegram misconfiguration.

`worc preflight` never spends a model call. When you want the isolation claim proved rather than described — after a host change, a CLI upgrade, or before letting a flow write unattended — run `worc preflight --paid-isolation-probe`: it adds one billed call per provider that supports it and lets an agent try to write into the Git directories and the control home, reading the verdict off the filesystem. Treat `NOT DEMONSTRATED` as "unproven", not as "safe" (see [security.md](security.md#how-the-isolation-claim-is-actually-proved)).

It does not probe or run your checks: it lists each configured command set with its `paths`, commands, and flags, which is enough to eyeball selection coverage. A malformed set never gets that far — the config validator rejects it on load, so every command exits 2 with the reason.

Preflight does **not** validate flow files — run `worc validate-flow --all` for that (it is config-aware, so it also catches flows made invalid by a config edit, e.g. a node pinned to a provider you just removed from `agents.allowed`). It covers your own flows under `.worc/flows/` only — the packaged built-ins are excluded, so with no operator flows `--all` reports nothing to check and exits 0. Do not treat config editing as done until both preflight and `validate-flow --all` are green.
