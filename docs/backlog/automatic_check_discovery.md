# Backlog: Automatic check discovery and environment resolution

Status: **in progress — Phases 1–3 implemented** (see [§17 Implementation status](#17-implementation-status))
Date: 2026-06-12 (updated 2026-06-13)
Owner: Vladimir Makarevich

This document captures the product task of making repository quality checks discoverable and
portable without requiring every operator to hand-write technology-specific commands. It is a
backlog item, not current runtime behavior. Nothing here overrides the canonical specification,
[CLAUDE.md](../../CLAUDE.md), [AGENTS.md](../../AGENTS.md), or the hard invariants in
[docs/rules/](../rules/).

## 1. Background

The current Check Runner executes `checks.commands` exactly as configured. This keeps the quality
gate deterministic, but creates a long setup path for repositories that use different ecosystems,
package managers, virtual environments, or project wrappers.

The problem became concrete while running `task-telegram-hitll` against this repository:

- `checks.commands` contained `pytest`;
- the orchestrator's allowlisted `PATH` did not contain the repository's `.venv/bin`;
- both Check Runner invocations failed to launch `pytest` before any test ran;
- the launch failure was treated as a quality failure and consumed two fixing iterations;
- an agent later found `.venv/bin/python -m pytest` and independently proved that all 629 tests
  passed, but the Check Runner continued using the unresolved configured command.

An absolute path to one developer's `.venv` would fix that single workspace, but would not be
portable across users, clones, operating systems, CI, or repositories using `uv`, Poetry, npm,
pnpm, Cargo, Go, Make, tox, or other tooling.

The broader requirement is therefore not "configure a better pytest path". The orchestrator should
discover, validate, remember, and execute the repository's own quality-gate profile.

## 2. Goal

Make the default onboarding path:

```text
install repository
    -> discover likely check commands
    -> validate safe candidates
    -> probe launchability
    -> persist a resolved check profile
    -> run the deterministic Check Runner
```

NOTE: it is necessary to provide for, and for a given agent, take a less expensive model and reasoning!

Most repositories should work without manually editing `checks.commands`. Explicit commands remain
available as an override for unusual or policy-sensitive projects.

## 3. Design principles

1. **Discovery may be intelligent; pass/fail remains deterministic.**
   An agent may propose candidates, but only the orchestrator executes the selected checks and
   decides their result from exit code, timeout, and process-launch status.
2. **Deterministic evidence comes first.**
   Inspect manifests, lock files, CI workflows, project documentation, and local environments before
   spending a provider run on agent-assisted discovery.
3. **A launch failure is not a code-quality failure.**
   A missing executable or module should try the next candidate or fail check preflight. It must not
   enter `fixing` or consume the fix budget.
4. **A check never mutates the environment.**
   Discovering `uv run pytest` is safe and read-only. A dependency-install/setup command (`uv sync`,
   `npm install`, `pip install`) is not a check: it is rejected as a candidate and never run by the
   orchestrator.
5. **Resolved profiles are cached and audited.**
   Discovery should not repeat before every task when the relevant repository inputs and environment
   have not changed.
6. **The Check Runner stays independent from providers.**
   Providers do not declare a check successful and do not gain control of state transitions.

## 4. Proposed architecture

Add a provider-agnostic component between configuration/install and the Check Runner:

```text
RepositoryInspector
    -> CheckCandidateDetector
    -> AgentCheckDiscovery (fallback, read-only)
    -> CheckCandidateValidator
    -> CheckProbeRunner
    -> ResolvedCheckProfileStore
    -> CheckRunner
```

Suggested responsibilities:

- `RepositoryInspector`: collect bounded, non-secret evidence from known project files.
- `CheckCandidateDetector`: produce deterministic candidates for recognized ecosystems.
- `AgentCheckDiscovery`: propose structured candidates only when deterministic confidence is low.
- `CheckCandidateValidator`: enforce argv, path, environment, and security rules.
- `CheckProbeRunner`: determine whether a candidate can be launched without running the full suite.
- `ResolvedCheckProfileStore`: persist the selected profile and its input fingerprint.
- `CheckRunner`: execute the resolved argv and remain the sole quality-gate authority.

The Core may call this provider-agnostic resolver, but must not learn Codex or Claude CLI syntax.

## 5. Discovery sources and precedence

Use evidence in approximately this order:

1. explicit operator override in `config.yaml`;
2. repository-owned quality entry points such as `make check`, `just check`, `task test`, tox, or
   nox;
3. CI workflows and scripts already used for pull requests;
4. package manifests and lock files;
5. repository instructions (`AGENTS.md`, `CLAUDE.md`, README, contributing and operations docs);
6. existing local environments such as `.venv`, `venv`, `node_modules`, or tool caches;
7. agent-assisted read-only discovery.

Examples of deterministic signals:

| Signal | Candidate checks |
|---|---|
| `uv.lock` | `uv run pytest`, `uv run ruff check .` |
| `poetry.lock` | `poetry run pytest` |
| `.venv/bin/python` | `.venv/bin/python -m pytest` |
| `.venv/Scripts/python.exe` | `.venv/Scripts/python.exe -m pytest` |
| `tox.ini` | `tox` |
| `noxfile.py` | `nox` |
| `package.json` script `test` | selected package manager plus `test` |
| `pnpm-lock.yaml` | `pnpm test` |
| `package-lock.json` | `npm test` or `npm run <script>` |
| `Cargo.toml` | `cargo test` |
| `go.mod` | `go test ./...` |
| `Makefile` target `check` | `make check` |

Detection must inspect the actual manifest before proposing a script or target. File presence alone
is not enough to claim that a command exists.

## 6. Agent-assisted discovery

When deterministic detection cannot produce a sufficiently confident profile, run a dedicated
read-only discovery stage. The agent receives bounded repository metadata and returns structured
data, not an executable shell script:

```json
{
  "checks": [
    {
      "name": "tests",
      "argv": [".venv/bin/python", "-m", "pytest"],
      "evidence": [".venv/bin/python exists", "pytest is declared in pyproject.toml"],
      "confidence": "high"
    },
    {
      "name": "lint",
      "argv": [".venv/bin/python", "-m", "ruff", "check", "."],
      "evidence": ["ruff is declared in project.optional-dependencies.dev"],
      "confidence": "high"
    }
  ]
}
```

The agent is advisory:

- it cannot mark a candidate as passing;
- it cannot change sandbox, approvals, environment allowlists, credentials, or denied commands;
- its response is rejected unless it matches a strict output schema;
- every candidate must pass deterministic validation and probing;
- task content cannot supply a command or weaken discovery policy.

Agent discovery is an infrastructure capability. Provider failure may use the normal
infrastructure-only fallback, but discovery must remain bounded.

## 7. Candidate validation and probing

Commands should be represented as structured argv:

```yaml
checks:
  commands:
    - name: tests
      argv: [".venv/bin/python", "-m", "pytest"]
    - name: types
      argv: [".venv/bin/python", "-m", "mypy", "src"]
```

Validation requirements:

- no shell interpolation or `shell=True`;
- executable and relative paths normalized against `repo.local_path`;
- no task-controlled argv;
- only allowlisted environment variables reach the process;
- mandatory timeout;
- existing security blacklist applied;
- a dependency-install/setup command is rejected, never run as a check;
- command provenance and evidence recorded in the profile artifact.

Probing should be lightweight and tool-specific where practical:

- verify executable/path presence;
- for Python, probe `python -c "import <module>"` or a safe version command;
- for package scripts, verify the script exists in the parsed manifest;
- for Make/Just/Task, parse or list targets without running the full target when supported;
- distinguish `launchable`, `not_launchable`, and `unsupported`.

Failed probes try the next candidate. They do not enter `fixing`.

## 8. Proposed configuration

```yaml
checks:
  discovery:
    mode: auto             # auto | deterministic | configured | disabled
    agent_fallback: true
    refresh: on_change     # on_change | always | never
  commands: []             # non-empty list is an explicit operator override
  timeout_seconds: 7200
```

Semantics:

- `auto`: deterministic detection, then agent fallback when confidence is insufficient;
- `deterministic`: inspect known project evidence only;
- `configured`: use explicit `commands` as-is (the backward-compatible default);
- `disabled`: explicit no-check mode with a prominent warning and audit record;
- non-empty `commands`: authoritative override regardless of discovery mode;
- an empty `commands` list under `auto` is not a successful no-op: resolution must produce a valid
  profile or stop before implementation.

## 9. Profile persistence and invalidation

Store a machine-readable artifact in the control workspace, for example:

```text
checks/resolved-profile.json
```

It should contain:

- schema version;
- resolved command names and argv;
- source (`configured`, `detected`, or `agent`);
- evidence and probe results;
- platform and relevant executable paths;
- fingerprint of discovery inputs;
- creation time and last validation time.

Fingerprint inputs should include relevant files such as:

- `pyproject.toml`, `uv.lock`, `poetry.lock`, `tox.ini`, `noxfile.py`;
- `package.json` and package-manager lock files;
- `Cargo.toml`, `Cargo.lock`, `go.mod`, `go.sum`;
- `Makefile`, `Justfile`, `Taskfile.yml`;
- selected CI workflow files;
- configured instruction/documentation files;
- presence or identity of selected local executables/environments.

Rediscover when the fingerprint changes or a previously resolved command becomes unlaunchable.

## 10. Runtime behavior and error semantics

Before creating a task branch:

1. load or resolve the check profile;
2. validate and probe every required check;
3. fail preflight if no valid profile can be produced;
4. only then begin the task pipeline.

During testing:

- process launch failure is an infrastructure/configuration event, not a failed test;
- non-zero exit from a successfully launched check is a quality failure and enters `fixing`;
- timeout follows the existing bounded check-failure policy, with its cause recorded;
- the artifact records both the logical check name and resolved argv;
- fixing agents receive the real check output, not a missing-executable error that they cannot fix in
  repository code.

## 11. Security and invariants

This feature must preserve:

- argv-list execution with no shell interpolation;
- allowlisted child environment;
- no secrets in profile artifacts, logs, SQLite, or agent context;
- no provider-specific syntax in Core or Check Runner;
- no agent commit, push, or PR operations;
- infrastructure-only provider fallback;
- independent orchestrator-owned quality decisions;
- bounded discovery attempts and probes;
- no security-policy override through task fields or discovered commands.

Discovery must not scan denied paths or feed secret files to an agent. CI files and scripts may
contain secret variable names; values must never be resolved or persisted.

## 12. Implementation phases

### Phase 1: structured commands and deterministic resolver

- introduce argv-based check definitions;
- detect common ecosystems and project-owned check entry points;
- add preflight probing;
- classify launch errors separately from quality failures;
- persist a resolved profile.

### Phase 2: installer and cache integration

- make `install` generate or resolve the initial profile;
- fingerprint discovery inputs;
- refresh on relevant changes;
- add `status`/`preflight` diagnostics showing selected commands and evidence.

### Phase 3: agent-assisted fallback

- add a strict structured-output schema;
- run read-only discovery only when deterministic confidence is low;
- validate and probe every proposed command;
- audit provider, evidence, rejected candidates, and final selection.

## 13. Acceptance criteria

- A newly installed common Python, Node.js, Rust, Go, tox/nox, or Make-based repository can resolve
  a working check profile without manual command editing when sufficient evidence exists.
- Explicit configured commands remain authoritative.
- The resolver chooses the repository-local Python environment when that is the valid environment,
  without storing a user-specific absolute path when a portable relative path is available.
- Missing executables/modules try alternative candidates and do not consume fixing iterations.
- No task branch or provider implementation run starts when required checks are not launchable.
- Agent-assisted discovery returns schema-validated candidates and cannot execute or approve checks.
- Resolved profiles are cached, audited, and invalidated when relevant inputs change.
- A dependency-install/setup command is never run as a check.
- Check execution still uses argv lists, timeouts, environment allowlists, and redacted artifacts.
- Documentation explains auto, deterministic, configured, and disabled modes.

## 14. Testing plan

Unit tests:

- evidence parsing for each supported ecosystem;
- candidate ordering and confidence;
- POSIX and Windows virtual-environment path resolution;
- structured argv validation and path normalization;
- fingerprint stability and invalidation;
- launch error versus quality failure classification;
- install-shaped candidates are rejected as checks.

Integration tests:

- `.venv/bin/python -m pytest` selected when plain `pytest` is unavailable;
- `uv run`, Poetry, npm/pnpm/yarn, Cargo, Go, tox/nox, and Make profiles;
- first candidate missing, second candidate launchable;
- working check exits non-zero and enters `fixing`;
- no candidate launchable stops before branch creation;
- agent proposes malformed/unsafe argv and it is rejected;
- cached profile reused, then invalidated after manifest/lock-file change.

End-to-end:

- `install` on representative fixture repositories resolves checks and `preflight` reports ready;
- a task completes without manually editing `checks.commands`.

## 15. Documentation updates when implemented

- [configuration.md](../configuration.md): discovery modes, structured commands.
- [operations.md](../operations.md): diagnostics, profile refresh, rejected candidates.
- [cookbook.md](../cookbook.md): zero-config onboarding and explicit overrides.
- [task-authoring.md](../task-authoring.md): clarify that task files cannot provide check commands.
- `CHANGELOG.md`: behavior and config-schema changes.

## 16. Open decisions

- Which CI formats should be parsed in the first deterministic release?
- What confidence threshold triggers agent-assisted discovery?
- Should project-owned wrappers such as `make check` outrank language-native commands in all cases?
- Where should the resolved profile live under each git footprint mode?
- Should `disabled` be allowed globally, or require an explicit unsafe/no-quality-gate warning?

## 17. Implementation status

Implemented 2026-06-13 (Phases 1–3). Code lives in `src/wastech_orchestrator/checks/`; tests in
`tests/checks/` plus additions to `tests/check/`, `tests/config/`, `tests/install/`, and
`tests/core/`.

### Done

- **Phase 1 — structured commands + deterministic resolver + the launch/quality split.**
  Canonical `ResolvedCheck` and the backward-compatible `checks.commands` union (string and
  `{name, argv}`); `RepositoryInspector` → `CheckCandidateDetector` → `CheckCandidateValidator` →
  `CheckProbeRunner` → `CheckResolver`; profile persistence + input fingerprint; `CheckRunner` and
  the orchestrator now treat a **process-launch failure as infrastructure** (terminal/preflight,
  never `fixing`, no fix-budget spend) and a check-preflight resolves a launchable profile **before
  any branch** (`checks.discovery.mode`, empty-under-`auto` stops). The decision settled here vs the
  doc: the default mode is **`configured`** (zero behaviour change on upgrade); discovery is opt-in
  via `mode` (and `install` writes `auto`).
- **Phase 2 — installer & diagnostics.** `install` writes `checks.discovery` and (via auto-preflight)
  seeds the resolved profile; `preflight` reports the resolved commands, evidence, probe status, and
  rejected candidates; `status` surfaces the cached profile read-only; `refresh: on_change`
  invalidation via the fingerprint. (`install/detect.py` is retained for the wizard's quick
  ecosystem hint and not yet consolidated onto `checks/detect.py` — see follow-ups.)
- **Phase 3 — agent-assisted fallback.** `AgentCheckDiscovery` + strict `schema_validate` +
  `discovery_factory`; a read-only, advisory, schema-validated provider call (cheap model via
  `checks.discovery.{provider,model,reasoning,timeout_seconds}`) whose proposals pass the same
  validator + prober. Runs at install only, opt-in (requires a configured `model`), never inside the
  state machine, never spends the fix budget.

### Deferred (not implemented)

- **Per-stage model/reasoning.** Discovery uses a deliberate one-off knob
  (`checks.discovery.{model,reasoning}`); a general per-stage system
  ([per_stage_model_reasoning.md](per_stage_model_reasoning.md)) would later subsume it.
- **CI-format parsing depth** (only file presence is evidence today), the **confidence threshold**
  that triggers agent fallback, **wrapper-vs-native precedence** beyond the current "launchable
  wrapper wins", and **agent discovery at `preflight`** (currently install-only). See §16 + the
  follow-ups tracker.
