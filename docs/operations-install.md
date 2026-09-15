# Operations: Install, Upgrade, Authorize

Part of the [operations guide](operations.md): binding the orchestrator to a repository, moving it to a new version, and where the credentials it never stores actually live.

## 1. Installation

Prerequisites: **Python 3.12+**, **git**, the **GitHub CLI** (`gh`) if you want PRs opened automatically, and the agent CLIs you intend to route to (`codex` and/or `claude`) on `PATH`.

**Officially supported CLI versions:** `claude` **≥ 2.1.210** and `codex` **≥ 0.144.4** — the versions the orchestrator is developed and tested against. Older versions may work but are not guaranteed; provider argv contracts and structured-output behavior are pinned to these. `preflight` reports the installed version of each CLI so you can confirm.

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows PowerShell  (source .venv/bin/activate on Unix)
pip install -e ".[dev]"             # or: pip install wastech-orchestrator
```

`install` is the single setup command. It sets up `<repo>/.worc/` in the current repository — a single gitignored home for everything the orchestrator generates: `config.yaml` (plus a commented `config.example.yaml` reference copy), a `guide/` (the installed docs bundle: task docs, a config helper, the flow-authoring reference, and the copy-ready skills for all three), the editable `flows/` copies (each built-in flow + its own per-node prompt dir, plus the shared `roles/supervisor.md`), the `tools/` copies (executables that packaged `tool` nodes resolve against, e.g. `check_chapter`), `state.db`, `logs/`, `runs/`, `memory/`, and `workspace/`. There is no sibling workspace and no separate clone requirement — the orchestrator branches/commits/pushes in the repo you run it in.

### Bind the repository (`install`)

Install the CLI once, then run `install .` in the repo (the same commands work on Windows and macOS):

```powershell
pipx install "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git"
cd C:\projects\my-repo
worc install .                          # interactive wizard
# or, without relying on console-script PATH resolution:
python -m wastech_orchestrator install .
```

The wizard detects the Git root, `origin`, base branch, and cleanliness; finds `codex`/`claude`/`gh`; proposes checks from the repo's ecosystem (`pyproject.toml` / `package.json` / `Cargo.toml` / `go.mod`); writes a validated `config.yaml` into `<repo>/.worc/`; and copies the packaged `worc/` guide to `.worc/guide/`. That installed guide now includes the task docs under `.worc/guide/tasks/`, the compact config-helper section under `.worc/guide/config/`, the flow-authoring reference under `.worc/guide/flows/` (flow schema, role prompts, prompt variables), the top-level `best-practices.md` / `decision-guide.md` / `footprint.md` pages, and the copy-ready skills gathered under `.worc/guide/skills/` — task/config authoring (`worc-task` / `worc-deco-task` / `worc-config`) plus flow authoring (`worc-flow` / `worc-flow-role` / `worc-flow-tune`). It appends three lines to the repo's tracked `.gitignore` — `.worc/` and the sibling `.worc-io/` exchange, so both the runtime home and the agent-facing exchange are ignored, plus an anchored `/tasks/` for the task lifecycle tree, which is therefore **gitignored by default**. Answer yes to the wizard's "track task files in git" question (or pass `--track-tasks`) to get the audit commit instead, and `--tasks-dir DIR` to scaffold, configure and ignore a directory other than `tasks/`; a lifecycle directory git already tracks something in is never ignored, whatever the answer. See [How-To → Track your task files in git](how-to.md#6-track-your-task-files-tasks-in-git). (`install --reconfigure` refreshes the `.worc/guide/` docs and the `.worc/config.example.yaml` reference to the packaged version. It snapshots what it replaces first, and **bounds its own backups**: only the newest three of each `config.yaml.bak-*` / `flows.bak-*` / `tools.bak-*` are kept, so repeated reconfigures no longer accumulate. Your own `state.db*.bak*` files are never touched.) To keep `.worc/flows/` itself git-tracked instead — so custom flows get history and go through a PR — see [How-To → Track your operator flows in git](how-to.md#4-track-your-operator-flows-worcflows-in-git). It never installs or authorizes the CLIs; it reports what is missing and auto-runs `preflight` at the end (a failed preflight keeps the config but exits non-zero with instructions).

Subsequent commands need no `--config` — they walk up from the current directory to the Git root and use `<root>/.worc/config.yaml`, run from anywhere inside the repo:

```text
worc preflight
worc watch
worc status
```

Config discovery order: explicit `--config` > `<repo-root>/.worc/config.yaml` (walk up to the Git root) > a hint to run `install .`. Re-running `install` is idempotent; `--reconfigure` writes a timestamped backup and atomically replaces the config. For automation: `worc install . --non-interactive --provider codex --no-create-pr` (`--non-interactive` replaces scripted setup). `--no-create-pr` disables the PR but not commit/push.

### Windows 10 and 11 / PowerShell

For a native PowerShell setup, use this sequence:

```powershell
# 1. Install pipx itself (official PyPA path).
py -m pip install --user pipx

# 2. If `pipx` is not yet on PATH, run the launcher directly from the warning's Scripts dir.
#    Typical location:
cd "$env:LOCALAPPDATA\Python\pythoncore-3.14-64\Scripts"
.\pipx.exe ensurepath

# 3. Restart PowerShell (and your IDE's integrated terminal, if you use one).

# 4. Install the orchestrator CLI.
pipx install "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git"

# 5. Verify the console script seen by this exact shell.
where.exe worc
worc --version

# 6. Bind the repository and run diagnostics.
cd C:\projects\my-repo
worc install .
worc preflight
```

If you prefer plain `pip` instead of `pipx`, or a shell still does not see `worc`, use the module form directly:

```powershell
python -m pip install --user "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git"
python -m wastech_orchestrator install .
python -m wastech_orchestrator preflight
```

The distinction matters:

- `worc` and `wastech-orchestrator` are **console scripts** placed in Python's `Scripts` directory.
- `wastech_orchestrator` is the **Python module**.
- `python -m wastech_orchestrator ...` is valid.
- `python -m worc ...` is **invalid** (`worc` is not a Python module).

#### Windows troubleshooting

- `python -m pip ensurepath` fails with `unknown command "ensurepath"`: `ensurepath` belongs to **`pipx`**, not to `pip`. Use `python -m pipx ensurepath` when `pipx` is already importable, or run `.\pipx.exe ensurepath` from the Scripts directory shown by the install warning.
- `python -m wastech_orchestrator preflight` works, but `worc` / `wastech-orchestrator` says "not recognized": the package is installed, but the current shell does not see Python's `Scripts` directory on `PATH`. Restart the shell first. If it still fails, ask Python for the exact Scripts dir and prepend it for the current session:

  ```powershell
  $scripts = python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
  $env:Path = "$scripts;$env:Path"
  where.exe worc
  ```

- `worc` works in a standalone PowerShell window, but not in the IDE terminal: the IDE was started before `PATH` changed, or it launches PowerShell with a different environment. Restart the IDE, or apply the temporary `$env:Path = ...` fix in the integrated terminal.
- `python -m worc preflight` fails with `No module named worc`: expected. Use `worc preflight` or `python -m wastech_orchestrator preflight`.
- You can verify what the current shell resolves with `where.exe worc`, `where.exe wastech-orchestrator`, and `python -c "import sysconfig; print(sysconfig.get_path('scripts'))"`.
- `preflight` reports `codex: FAIL — Codex sandbox helper codex-windows-sandbox-setup.exe is not discoverable …`: on Windows the `workspace-write` sandbox launches that helper from the Codex standalone package's `codex-resources` directory. The orchestrator resolves it from `codex`'s own install and prepends it to the run's `PATH` automatically, so a normal install passes; this failure means the helper is genuinely missing (an incomplete or partial Codex install). Reinstall or upgrade the Codex CLI so `%USERPROFILE%\.codex\packages\standalone\current\codex-resources\codex-windows-sandbox-setup.exe` exists, then re-run `preflight`. (The healthy line shows where the helper resolved.)

---

## Upgrading the orchestrator

The orchestrator is a CLI, not a daemon — "updating the implementation" means upgrading the package and restarting any `watch` loop. Because the package is installed **from a pinned git tag** (not from a version index), the reliable recipe is a clean uninstall + reinstall at the new tag:

```bash
pipx uninstall wastech-orchestrator
pipx install "wastech-orchestrator[shell] @ git+https://github.com/VladimirMakarevich/wastech-orchestrator.git@vX.Y.Z"
worc --version           # confirm the new version
```

> **Why not `pipx upgrade` / `--force`?** With a pinned git tag, `pipx upgrade wastech-orchestrator` treats the pinned ref as "already at latest" and does nothing (it reports the old version as current — even with `--pip-args="--pre"`, which only applies to a PyPI index, not a git ref). And `pipx install --force` fails on the uv backend ("A virtual environment already exists … not created in this session"). The `uninstall` + `install` pair sidesteps both. This friction is tracked as the backlog's install-and-upgrade item (the durable fix is publishing to PyPI). Drop the `@vX.Y.Z` suffix to track the branch head; append it to pin a specific (pre)release tag (see the tag note below).
>
> **The `[shell]` extra** (for `worc shell`) must be carried in the PEP 508 spec as shown — `"wastech-orchestrator[shell] @ git+…"`. Always quote it, and the same quoted form works identically on `zsh`, `bash`, and PowerShell. Unquoted, the bare `[shell]` misbehaves per shell: `zsh` errors (`no matches found`); `bash` treats `[shell]` as a glob character class and silently mangles the spec if a file named `s`/`h`/`e`/`l` happens to exist in the current directory; PowerShell needs the quotes for the `@` and spaces. And in every shell `pip install wastech-orchestrator[shell]` targets PyPI rather than the installed git package — the `@ git+…` form is what pins it to source.

Do it **between tasks**, not mid-run: an in-flight task holds the single processing slot and a live working branch, and its state lives in `state.db`. Wait until `status` shows no active task, then upgrade and re-run `preflight` / `watch`.

> **Linux/WSL2 operators — new runtime dependency (WRI-002).** A **workspace-write Claude** node now runs its `Bash` tool inside Claude's OS Bash sandbox, which on Linux/WSL2 requires **`bubblewrap` (`bwrap`) + `socat`** on `PATH`. A host missing them cannot enforce the sandbox, and that is reported **advisorily** at either value of `strict_isolation`: `preflight` prints an `isolation-floor:` line stating that `.git` and `.worc` are writable here, and the run continues. Under `strict_isolation: true` a refusal still happens, just later and narrower — the attempt that actually needs a sandboxed shell ends `capability_unavailable` → fallback or `manual_action_required` — rather than running Bash unsandboxed. **Install both before upgrading** (`apt install bubblewrap socat` or your distro's equivalent), or set that provider `read-only`. macOS (Seatbelt) and native Windows (restricted no-Bash mode) are unaffected.

The persisted state survives an upgrade — back it up first so you can roll back. That is everything under `<repo>/.worc/` (`config.yaml` and `state.db` live there), plus the `tasks/` lifecycle dirs at the repo root (gitignored by default — git-tracked only if you asked `install` to track them). Copy at least `.worc/config.yaml` + `.worc/state.db`. The orchestrator **fail-closes on a backward-incompatible workspace**: if the `config.yaml` `schema_version` or the `state.db` schema is **newer** than the installed version understands, the command prints a clear `error:` and exits non-zero (2) instead of running against a format it cannot read. To recover, upgrade the package to a version that supports it (or, for a throwaway setup, start a fresh workspace via `install --reconfigure`).

The current schema versions are `state.db` **v27** and `config.yaml` `schema_version` **40**, and the two are handled differently because the orchestrator is **greenfield** (no production data to preserve):

- **`config.yaml`** does **not** auto-migrate, and an **older or absent** `schema_version` is accepted as-is (a new release adds keys with safe defaults, so an older config still runs). To materialize the new keys in your file run **`upgrade-config`** (below).
- **`state.db`** does **not** migrate across versions. A brand-new (or pre-versioning) database is created at the current shape; a database stamped an **older** version (`1 ≤ v < 27`) is **refused fail-closed** — several past bumps dropped/renamed tables, and the store only ever adds columns, so an old shape cannot be reshaped in place. Recovery is to delete the local `state.db` and start a fresh workspace (greenfield: there is nothing to preserve). Do this **between tasks**, never with a task in flight. (v16 added additive normalized-token-usage columns: a per-run delta on `provider_attempts` and a running-cumulative snapshot on the lineage tables. v19 added `provider_attempts.task_id` — so a cost/usage roll-up sums by task without a `node_runs` join — and made `node_run_id` nullable, so the supervisor layer records its own provider calls with `node_run_id` NULL. v20 added `provider_attempts.supervisor_function` (`observe`/`finalize`/`handoff`, NULL for a graph node), so that spend splits per phase in one `GROUP BY`. v21 added `tasks.blocked_until` — the provider-reported wake instant of a parked task, see [provider outage behavior](operations-running.md#provider-outage-behavior). The later bumps added `publish_operations.pushed_sha` — the commit a push actually left on the remote, so a branch someone else moved is distinguishable from the one we put there — and three nullable per-task reference points, each of which means something by being NULL rather than holding a placeholder: `tasks.gate_reference_sha` (the commit the dangerous-diff gate measures from; NULL = the task's diff base), `tasks.push_url_digest` (the sha256 of where a push would go, read once at branch prep before any provider ran, so the unconditional pre-push re-read has a baseline; NULL = no baseline, never read as a rewrite) and `tasks.base_ref` (the commit the working branch sat at when the task started; NULL = the config base branch). All three must survive the process: re-derived from `HEAD` after the run has committed they would walk forward, and the task's reported change would shrink to whatever is still uncommitted. v27 added `tasks.source_sha256` — the newline-normalized digest of the task file each attempt read, which is how a finished task's own file resurfacing in `tasks/pending/` is recognised and left alone rather than re-run or quarantined, see [§7](operations-diagnostics.md#7-recovery-playbook--manual_action_required); an existing `state.db` stamped 26 must be recreated.)

To bring the rest of your deployment current after a package upgrade, run **`upgrade-config`** (and **`upgrade-docs`**):

```bash
worc upgrade-config              # uses the discovered/bound config
worc --config path/to/config.yaml upgrade-config --dry-run   # preview only
```

It adds any keys the current format introduced (from the packaged template's defaults), **keeps every existing value**, stamps the current `schema_version`, and backs up the original to `config.yaml.bak-<UTC>` before writing. It is idempotent (an already-current config is left untouched) and fail-closed (it refuses a config that is unparsable or already newer than this version, and never writes a config that would fail validation).

**It confirms before writing, and the prompt is not ceremony.** `install` delivers a deliberately small `config.yaml` that omits every key it left at a default; this command's add-missing-only merge writes **all of them back out**, so a file you kept short comes back long. `-y` / `--yes` skips the prompt; `--dry-run` never asks and lists what would be added without writing. **Caveat:** when it does rewrite the file it re-emits via YAML and **drops inline comments** — keep the _reason_ for an unusual value recoverable elsewhere, and see `config.example.yaml` / [configuration.md](configuration.md) for field docs.

Two keys it deliberately does **not** strip, because there is nothing to migrate them toward: `agents.providers.<id>.sandbox` (removed in `schema_version` 38) and the whole `skills` block (repo-skill selection was removed outright). A config still carrying either is a **load error** telling you to delete the line yourself; stripping them silently would read as "the key still works, it just does nothing".

The installed guide bundle also ships with the package (packaged source dir `worc/`, copied to `.worc/guide/`), so an upgrade brings newer docs than your already-installed copy. That bundle includes the task docs, the copy-ready skills gathered under `.worc/guide/skills/` — task/config authoring (`worc-task`, `worc-deco-task`, `worc-config`) and flow authoring (`worc-flow`, `worc-flow-role`, `worc-flow-tune`) — and the `config/` and `flows/` subtrees. Refresh the installed copy (under `.worc/guide/`) with **`upgrade-docs`**:

```bash
worc upgrade-docs                # uses the discovered/bound config location
worc upgrade-docs --dry-run      # preview added/updated/removed files only
```

Unlike `config.yaml`, the `worc/` docs are generated content with **no operator edits to preserve**, so this is a straight overwrite to the packaged version: it writes missing or changed files, removes files no longer shipped, and makes no backup. It is idempotent (an already-current copy is a no-op), `--dry-run` writes nothing, and it fails closed (exit 2 with the same hint as `upgrade-config`) when no install location can be resolved.

After a package upgrade, run **`upgrade-config`** and **`upgrade-docs`** to bring your deployment fully current. (A single umbrella `upgrade` that does both is not implemented yet.) `install` delivers **every** built-in flow — the shipped set is whatever `packaged/flows/*.yaml` contains, today `implementation`, `merge`, `deep_research`, `security_audit`, `content_chapter`, `content_translate`, `blog_article`, and `blog_article_revise` — and their per-node prompt templates as **editable copies** under `.worc/flows/` (each `<task_type>.yaml` plus its own `<task_type>/*.md` prompt dir); a flow node's prompt is its `role_file`, so you customize a node by editing the delivered role file. `.worc/flows/` is the **sole** source the orchestrator resolves flows from — the packaged tree is never read at run time — so a package upgrade does **not** refresh these copies (and a **newly-shipped** built-in is not runnable until it is delivered): re-run **`install --reconfigure`** to refresh/add them to the packaged version (it snapshots your existing `.worc/flows/` to a `flows.bak-<UTC>` sibling first, so edits stay recoverable). A dedicated flow/prompt re-sync step (the analogue of `upgrade-docs`) does not exist yet.

To install or pin a specific published (pre)release, append its tag to the `pipx`/`pip` source — e.g. `pipx install "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git@v0.1.1a1"`. Releases are tag-driven and pre-releases (`aN`/`bN`/`rcN` tags) are marked as such on GitHub; maintainers cut them by pushing a `v*` tag, which runs the [release workflow](../.github/workflows/release.yml).

---

## 2. Authorization (configured outside the orchestrator)

The orchestrator passes child processes **only** the allowlisted environment variables (`security.allowed_environment`) and never reads or stores credentials. Set authorization up yourself, once, in the environment the orchestrator runs in:

- **git / GitHub** — configure push access for `repo.url` (SSH key or credential helper) and authenticate `gh` (`gh auth login`) so `gh pr create` works. The orchestrator never embeds tokens.
- **Codex** — sign in with the Codex CLI as usual (e.g. `codex login`); its config lives under `CODEX_HOME`, which is on the default allowlist.
- **Claude Code** — sign in with the Claude CLI (e.g. `claude login` or an API key in its own config); its config dir `CLAUDE_CONFIG_DIR` is on the default allowlist. On macOS a subscription/OAuth login keeps its token in the Keychain, which the CLI reaches via `$USER` — `USER` is on the default allowlist for this reason (drop it and the spawned CLI reports "Not logged in").

Install only the providers you intend to route to. When Claude Code is unavailable, remove it from `agents.allowed` and route every agent-driven stage to Codex. GitHub CLI is required only when `git.create_pull_request: true`; disabling PR creation does not disable commit or push. When PR creation is enabled, `run`, `watch`, and `rerun` **pre-flight `gh` at startup** and exit `2` with an actionable message if it is not on `PATH`, rather than failing later inside the publish stage. On top of that hard gate there is a **non-blocking auth advisory**: if `gh` is present but not logged in, startup logs a `WARNING` ("gh present but not logged in — run `gh auth login`") and continues — it never blocks the run (a valid `GH_TOKEN`/`GITHUB_TOKEN` in the environment, or a transient probe failure, is honored, and the real `gh pr create` failure still degrades to `manual_action_required` safely). The advisory emits a fixed message only — never the raw `gh auth status` output, which would carry the account login and token scopes.

**Where the values live.** The orchestrator reads secrets from its **own** process environment. Provide them either by `export`ing them in the shell/service that launches `worc`, or by putting them in `<repo>/.worc/.env`, which the orchestrator auto-loads at startup (an exported variable always wins over the file). `.worc/` is gitignored so the file is never committed; `install` writes a `.worc/.env.example` template to copy. Point at a file elsewhere with the global `--env-file PATH` (a missing explicit `--env-file` fails closed with exit 2; a missing auto-discovered `.worc/.env` is a silent no-op). Loading `.env` only populates the orchestrator's own environment — it does **not** widen what child processes receive.

If a credential must reach a child process, add **only its variable name** to `security.allowed_environment`. Never place a secret value in `config.yaml`, a task file, or `extra_args`.

---
