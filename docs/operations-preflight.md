# Operations: Preflight and Validation

Part of the [operations guide](operations.md): what `preflight` proves before the first task runs, the command-set summary, and validating flows.

## 3. Preflight (both CLIs)

Before processing tasks, verify the environment with the read-only diagnostics command. It runs each allowed provider's `preflight()` (`<cli> --version` — no task is processed) and the deterministic `strict_isolation` policy check, then prints a secret-free verdict. Preflight is a **run-surface health gate** — it does **not** validate flows; flow correctness is an on-demand concern handled by [`validate-flow`](#validating-flows-validate-flow) (and enforced fatally at task dispatch regardless).

```bash
python -m wastech_orchestrator preflight
```

```text
env: OK — loaded 2 variable(s) from .worc/.env
claude: OK — claude 2.1.210 available (version=2.1.210, auth=logged_in (claude.ai))
claude-binary: /opt/homebrew/bin/claude -> /opt/homebrew/Cellar/claude/2.1.210/bin/claude
codex: OK — codex 0.144.4 available (version=0.144.4, auth=logged_in)
codex-binary: /Users/me/.codex/bin/codex (inside CODEX_HOME)
codex: isolation smoke OK — codex workspace-write sandbox: OS-enforced (empty MCP inventory)
isolation: OK (enforced)
isolation-floor: OK — sandbox enforceable on this host
allowed-environment: OK — 9 name(s) after expansion (agent + git/gh scope)
extra-environment: OK — 2 name(s) assigned: DOTNET_ROOT, NUGET_PACKAGES
assigned-paths: OK — 2 path(s) resolved; 2 .git/info/exclude rule(s) present
gh-repo-pin: OK — every gh call pinned to OWNER/REPO (from repo.url)
checks: 2 command set(s):
  repo (paths: always): ruff check .; mypy src; pytest
  ios (paths: ios/**) [skip_if_unavailable]: xcodebuild test
gh: OK
telegram: SKIP (disabled)
preflight: ready
```

Line order is fixed: `env`; then per allowed provider its health line, its `<provider>-binary:` line, any `WARN`/`FAIL`, and its isolation smoke; then `isolation`, `isolation-floor`, the optional `read-isolation` / `git-evidence` / advanced-mode notices, the environment lines (`allowed-environment`, `extra-environment`, `assigned-paths`), `gh-repo-pin`, the command sets, `gh` (only when `git.create_pull_request` is on), `telegram`, verdict.

> **Preflight runs no task, but it is not filesystem-read-only.** `assigned-paths:` resolves symlinks, case aliases, and the env-file against the real filesystem, and it may **repair** the clone-local `.git/info/exclude` rules that keep an in-clone toolchain cache out of the diff.

- Exit `0` when every allowed provider is healthy and the required isolation can be enabled; non-zero otherwise. The command-set summary is informational — an empty `command_sets` (`checks: no command_sets configured (no quality gate)`) is valid and does not fail preflight.
- A `FAIL` line names the problem without leaking secrets (e.g. `codex executable not found`).
- `isolation: OK (enforced)` — every provider in `agents.allowed` passed the offline isolation check (Codex sandbox, Claude permission mode + tool/sandbox policy) and its configured isolation is **legal**. That check runs at **either** value of `security.strict_isolation` — under `false` it matters most, because there the generated permission profile is the whole local floor.
- `isolation: OK (advanced mode)` — the check still passed, and `security.strict_isolation: false` is in effect. **This is what a fresh `install` writes**, so expect this line rather than the one above on a new workspace. It is one line by design and it points at [the advanced mode](configuration-agents.md#the-advanced-mode-strict_isolation-false) rather than reciting the whole floor into every report — read that section before you keep the key. It does **not** mean full access is available: the two provider full-access selectors are refused at every value of the key.
- `isolation-floor: …` — whether an **OS-enforced write floor** exists for this run at all. Advisory, never a refusal: the line states loudly that `.git` and `.worc` are writable here and the run continues. Two different things print `isolation-floor: NONE`, and the line says which. A **host** that cannot sandbox — native Windows, or Linux/WSL2 missing `bubblewrap` + `socat` — at either value of `strict_isolation`, and there the line carries the remedy. And **`strict_isolation: false` itself, on every host**, because the advanced mode raises no OS sandbox for Claude anywhere; that arm carries no remedy on purpose, since the remedy is `strict_isolation: true`, an operator decision rather than a missing dependency. So a macOS machine that reported `isolation-floor: OK` under `true` reports `NONE` under `false` — expect this line on a fresh `install`. **Codex is not symmetric**: it always gets a generated permission profile, so its floor does not move with the key; on native Windows its own line says only that the answer cannot be classified offline and points at the live smoke. Under `strict_isolation: true` a refusal still happens, just later and narrower — the adapter raises `capability_unavailable` for the attempt that actually needs a sandboxed shell (native Windows drops the shell instead), which is a per-node verdict a fallback provider can cover.
- `git-evidence: ON` — `security.allow_git_evidence: true` is in effect, so a flow node declaring `git_evidence: true` gets the read-only git verbs (it stays `read-only` on disk). Off is a **grant** switch, not a kill switch: a Codex `read-only` node reads git history either way, so with it off the same flow has different reach per provider. **Inert under advanced mode** — there every node already has an unscoped shell, so the grant has nothing left to add. See [`allow_git_evidence`](configuration-agents.md#allow_git_evidence-the-read-only-git-evidence-grant).
- `read-isolation: OFF (<why>)` — `security.disable_read_isolation: true` is in effect, or it is forced by `strict_isolation: false`. **This line is on a fresh install:** `disable_read_isolation` defaults to `true`, so read-isolation is off out of the box and you turn it **on** by setting the key `false` (`strict_isolation` is still the master switch and always wins toward relaxation, so `strict_isolation: false` forces it off again). Providers run with **native project-instruction/config discovery** restored (Claude reloads `CLAUDE.md` + project settings/hooks/MCP/skills; Codex reloads the user `config.toml` + project `.codex` config/hooks/rules) — deliberately accepting the reduced isolation, since a re-enabled hook can run arbitrary commands. **It does not lift the private read-deny projection:** `.worc`, the resolved env-file and the frozen `runs/` bundles stay read- and write-denied at either value of this key. (The agent CLIs' own config homes — `~/.claude` / `$CLAUDE_CONFIG_DIR`, `$CODEX_HOME` — are a separate matter: they are outside every deny projection at any value of any key, and no setting restores one.) The **write** side (exchange/`.git`/`tasks/` write-deny, commit/staging gates, PR control) and the `denied_read_paths` blacklist stay in force. The run log emits the same warning at task start, so this is never a silent weakening.
- `isolation: FAIL` lists the offending provider/setting — an **illegal configuration**, at either value of `strict_isolation`. A run fails before any branch is created; fix the config (don't reach for a weaker sandbox/permission profile — the full-access selectors are refused outright) and re-run. Note the split from the previous bullet: a _missing host capability_ is `isolation-floor:`, advisory; an _illegal config_ is `isolation: FAIL`, fatal. A required-but-unavailable sandbox that surfaces at run time is instead the deterministic pre-model `capability_unavailable` class. The router may fall over from it only to a provider whose **own configuration** permits isolating — a host-independent question, deliberately: whether the host can enforce a floor is advisory everywhere else, and the fallback provider decides it per attempt with the node's declaration in hand. If no provider qualifies, the task ends `manual_action_required`.
- `allowed-environment: …` — what each `security.allowed_environment` entry matched **on this host**, including what a prefix pattern (`DOTNET_*`) expanded to, anything dropped as secret-bearing, and which child-process scope the list describes (under advanced mode it gates only orchestrator-owned `git`/`gh`). A clean expansion is INFO; a secret-name drop is WARN. **`allowed-environment: FAIL`** is launch-critical rather than cosmetic: on Windows a list missing `SystemRoot` fails here _and_ at `run` / `watch` / `rerun` start, because the Node-based `claude.exe` was observed aborting `0xC0000409` before printing anything. A resumed run repeats the posture before launching more work.
- `extra-environment: …` / `assigned-paths: …` — the names `security.extra_environment` assigns (**names only, never values**) and where their path values resolve to after symlinks, `~`, and case/UNC aliases. A value overlapping `.git`, `.worc`, `.worc-io`, the tasks dir, the env-file, or a `denied_read_paths` match is rejected — lexically at config load, and again here against the real filesystem. Outside-clone paths warn only under strict isolation.
- `gh-repo-pin: …` — whether every `gh` call is pinned to a repository with `--repo`, taken from `repo.url` or, when that names no hosted repository, from the clone's `origin` read once before any agent runs. When neither parses into `owner/name` — an ssh **alias**, a `file://` URL, a local path — there is **no pin at all**, on any call, including the probe that decides which pull request the task appends to. That is a **FAIL** when `git.create_pull_request` is on and a warning otherwise, said out loud rather than letting the guarantee switch off quietly.
- `<provider>-binary: …` — the launch path, the real file behind it after following symlinks, and whether that file lies inside the provider's config home. Purely informational, never a failure: the standalone-package layout (Codex keeps its binary inside `$CODEX_HOME`) is the one fact that explains why the same build behaves differently on two hosts, and learning it should not require reading a failed attempt's stderr.
- **`<codex>: isolation smoke OK — …`** (WRI-006 / H7) — beyond the offline check above, `worc preflight` runs a **live, no-model** `codex sandbox` capability smoke of the generated `worc` profile on this host: it stands up a throwaway fixture and proves the private home is denied (direct + shell + through a workspace symlink alias), a repo read is allowed (the positive control), a repo write matches the profile, the exchange is read-only, and records the effective `codex mcp list` inventory. `OK` means the sandbox is genuinely OS-enforcing here. A **`WARN — isolation smoke: … sandbox could not run …`** means the host cannot demonstrate the sandbox (old Codex CLI, missing sandbox helper) — the `capability_unavailable` class; it fails preflight only when Codex has no fallback provider. A **`FAIL — isolation smoke: … not enforcing …`** means a denied path was actually readable/writable — a `configuration_error` security result that fails preflight unconditionally. The smoke runs for a healthy Codex at **either** value of `strict_isolation` — under `false` most of all, since there the generated profile is the whole local floor, which makes it the last configuration to excuse from demonstrating it; a Claude-only deployment never spawns it. Beyond the paths above, the battery proves that the CLI binary itself **executes** under the profile (`codex --version` inside the sandbox — the one capability every read probe misses; a refusal there is reported as a **configuration error**, never a host gap, because the capability provably exists and only the profile can be taking it away) and that a write into each Git-control root — the gitdir, a linked worktree's shared common dir, the hooks dir, and `tasks/` — is refused. One rule governs how to read any of it: **"nothing was written" is not a pass.** If not even the allowed control file appears, the probe cannot tell an enforced sandbox from a model that never attempted the write, so it reports `NOT DEMONSTRATED` — a warning when another provider can cover the work, a preflight failure when none can. A proven leak is always fatal. One asymmetry: a declared Git-control root with **no directory on disk** (a repository with no `tasks/` tree) makes preflight's battery report the whole fixture `NOT DEMONSTRATED`, while a real attempt logs a warning naming that root and runs the remaining probes — so read that warning as "this one was not proved", not "this one held".

#### `--paid-isolation-probe` — the one probe that cannot be free

Claude builds its sandbox inside its own session and offers no way to run a command under it without the model, so the only honest test is to let an agent try:

```bash
worc preflight --paid-isolation-probe
```

It spends **one real (billed) model call** per supporting provider, asking the agent to write into the two Git directories, the control home, and one allowed path inside the workspace. The verdict is read from the **filesystem** afterwards, never from what the model said it did. Because it costs money it is never implied — no run, and no plain `worc preflight`, spends it. It leaves its evidence behind at `.worc/preflight/claude-paid-isolation-probe.json`: the per-path verdicts plus the model's own last message (redacted), which is the only way to tell "the sandbox refused" from "the model never tried" when a verdict is `NOT DEMONSTRATED`. **Two configurations make it decline instead of spending anything, and it says which:** a host with no OS Bash sandbox, and `security.strict_isolation: false`, where no sandbox is raised on any host — there is no enforcement left to demonstrate, and the `isolation-floor: NONE` line already reports its absence. So on a fresh `install` (which writes `strict_isolation: false`) this flag costs nothing and proves nothing; set `strict_isolation: true` first if you want the proof.

> **Cross-platform verification (WRI-006).** Cross-platform behaviour is a hard invariant, so the deterministic test suite (provider command generation, typed layout, exchange publication, lifecycle sealing/restoration, redaction, security validation) runs **natively on Windows, macOS, and Linux** in CI — never only Ubuntu. That deterministic suite is credential-free and spends no model tokens. **Real OS-enforcement proof** needs the actual CLIs, which hosted CI runners do not ship: the `codex sandbox` smoke and Claude's Bash sandbox are therefore **local/manual** gates (the `codex` one runs wherever the CLI is installed, including native Windows; `worc preflight`'s live capability smoke is the operator-facing form). **WSL2** is treated as the Linux policy branch and is covered by the same local/manual smoke when hosted CI cannot supply a WSL host. Fake-CLI/settings-serialization tests prove **wiring only** — never OS-enforcement.

### Command-set diagnostics

`preflight` and `status` print a static summary of the configured `checks.command_sets` — the operator-authored gate. There is no resolution, probing, caching, or readiness verdict: the commands are exactly what the operator wrote (see [`checks`](configuration-checks-git.md#checks)), and the orchestrator does not auto-detect them. Each line names a set, its selecting `paths` (`always` when it has none), any `timeout=Ns` / `skip_if_unavailable` flags in brackets, and then the set's commands verbatim, `;`-joined — the two `checks:` lines in the [preflight sample](#3-preflight-both-clis) above are exactly that.

- `status` prints the same read-only summary (it never runs anything). An empty `command_sets` shows `checks: no command_sets configured (no quality gate)` — a valid configuration in which every task passes the checks node.
- At task time the runner runs the **union** of the sets whose `paths` match the task diff (a set with no `paths` always runs; an empty diff runs nothing; a changed path claimed by no set runs no set on its account — cover shared/root files with a no-`paths` catch-all set). All selected checks run and the verdict is aggregated: a **required toolchain absent** (a non-`skip_if_unavailable` set whose binary cannot launch) or every check skipped leaves the gate **incomplete** → the task goes to **manual** (the agent cannot install host toolchains); otherwise a quality failure → `fixing`, else pass. A skipped `skip_if_unavailable` set is recorded loudly in `check_runs` and **blocks `git.auto_merge`** even when the node passes.

### Validating flows (validate-flow)

Flow validation is separate from preflight and on demand. Validate the flows you author in `.worc/flows/` config-aware, exactly as the engine sees them at dispatch:

```bash
worc validate-flow implementation   # one flow (bare stem, or implementation.yaml)
worc validate-flow --all             # every *.yaml in .worc/flows/
```

- Scope is your own `.worc/flows/` only — packaged built-ins are excluded (they ship validated with the orchestrator; a built-in not copied into `.worc/flows/` is reported "not found"). Passing neither a name nor `--all` is a usage error.
- It runs the full fatal validator (graph + security ceiling + the config-aware layer, including the `.worc/tools/` tool-name check), so it catches a disallowed provider/model/reasoning, an over-ceiling node, or a `tool:` node naming a tool you have not delivered — the same failures a task would hit at dispatch. It also emits a non-fatal `WARN` when a role prompt references an unknown `{token}` (which would render verbatim to the agent).
- A failing flow prints `flow <name>: FAIL — <header>` followed by **every** violation as its own indented line — not just the header. (Earlier versions printed the header line alone, leaving a trailing colon and no findings.)
- Exit `0` when every checked flow is valid, `1` when any is invalid, `2` when the named flow is not found, on a usage error, or when no config can be loaded. `WARN` lines never change the exit code.

```text
flow content_chapter: OK
flow my_flow: FAIL — node 'review': provider 'gpt-5.5' not in agents.allowed
flow my_flow: WARN — roles/review.md references unknown {plann_path} (renders verbatim to the agent)
```

### Verify the executable seen by the runtime

Check versions from the same shell and environment that will launch the orchestrator:

```bash
command -v codex
codex --version
codex exec --help
command -v claude
command -v gh
```

On Windows use `where <command>`. WSL, PowerShell, a global npm install, and the Codex IDE extension may expose different binaries. Different reported versions therefore do not necessarily indicate a broken installation; they usually mean different `PATH` resolution. Set a specific provider `command` only to an executable that can run inside the orchestrator's OS environment. If `codex --version` and `worc preflight` disagree, follow [How-To §5](how-to.md#5-fix-conflicting-codex-installations-on-windows) to pin the intended native executable and verify its matching sandbox helpers.

---
