# B14 — Dangerous-Diff Guardrail

> Reconstructed from code (`core/dangerous_diff.py`) and tests (`tests/core/test_hitl.py`, `tests/core/test_flow_node_runners.py`, `tests/core/test_orchestrator.py`). The code is the only source of truth; this document was rebuilt from the implementation, not from prose or comments. Significant claims carry a `file:line` reference.

**Status:** documented · **Source modules:** `src/wastech_orchestrator/core/dangerous_diff.py`

## Responsibility

A single pure, deterministic function that inspects a tuple of changed repository paths and decides whether the diff is "dangerous" — i.e. it removed files or touched a dependency manifest/lock. It returns a `DangerousDiff` describing the risk and the exact normalized paths, or `None` for an ordinary diff ([dangerous_diff.py:82-109](../../../src/wastech_orchestrator/core/dangerous_diff.py#L82)).

This block is only the **classifier**. It performs no I/O, holds no state, and knows nothing about approvals. The guard that consumes its verdict (write diff → classify → durable approval → reconsider-once → fail closed to manual) lives in the agent node runner ([B30](B30-flow-node-runners.md)); this document cross-links to it rather than re-documenting that flow.

## Public surface

- `classify_dangerous_diff(entries: tuple[ChangedPath, ...]) -> DangerousDiff | None` ([dangerous_diff.py:82](../../../src/wastech_orchestrator/core/dangerous_diff.py#L82)) — the pure classifier; `None` means no human approval is needed.
- `DangerousDiff` (frozen dataclass) ([dangerous_diff.py:72-79](../../../src/wastech_orchestrator/core/dangerous_diff.py#L72)) — fields `risk: str`, `paths: tuple[str, ...]`, `deleted_paths: tuple[str, ...]`, `dependency_paths: tuple[str, ...]`.
- `_DEPENDENCY_PATTERNS: tuple[str, ...]` ([dangerous_diff.py:10-69](../../../src/wastech_orchestrator/core/dangerous_diff.py#L10)) — module-private manifest/lock pattern set.
- `_is_dependency_path(path: str) -> bool` ([dangerous_diff.py:112-114](../../../src/wastech_orchestrator/core/dangerous_diff.py#L112)) — module-private basename matcher.

The input `ChangedPath` (`status`, `path`, `previous_path`) is owned by [B22](B22-git-manager.md) ([git_manager.py:139-145](../../../src/wastech_orchestrator/git_manager.py#L139)).

## Behavior

### Classification

The function scans every entry once ([dangerous_diff.py:86-94](../../../src/wastech_orchestrator/core/dangerous_diff.py#L86)). `status` is upper-cased, then:

- a status starting with `D` adds `entry.path` to the **deleted** set ([dangerous_diff.py:88-89](../../../src/wastech_orchestrator/core/dangerous_diff.py#L88));
- a status starting with `R` (rename) with a non-`None` `previous_path` adds the **previous** path to the deleted set — a rename-away is treated as deleting the original path ([dangerous_diff.py:90-91](../../../src/wastech_orchestrator/core/dangerous_diff.py#L90));
- both `entry.path` and `entry.previous_path` are independently tested against the dependency matcher; any hit is added to the **dependencies** set ([dangerous_diff.py:92-94](../../../src/wastech_orchestrator/core/dangerous_diff.py#L92)).

If neither set has anything, the diff is ordinary and the function returns `None` ([dangerous_diff.py:96-97](../../../src/wastech_orchestrator/core/dangerous_diff.py#L96)). Otherwise `risk` is assigned by which sets are non-empty: both → `other`, deletions only → `deletion`, dependencies only → `dependency` ([dangerous_diff.py:98-103](../../../src/wastech_orchestrator/core/dangerous_diff.py#L98)). The returned `DangerousDiff` carries the sorted **union** as `paths`, plus the two sorted subsets `deleted_paths` and `dependency_paths` ([dangerous_diff.py:104-109](../../../src/wastech_orchestrator/core/dangerous_diff.py#L104)).

Note that a single renamed manifest (e.g. `requirements.txt` → `requirements-dev.txt`) lands in **both** sets — the previous name is a deletion and a dependency match, the new name is a dependency match — so its risk is `other`, confirmed in [test_hitl.py:143-154](../../../tests/core/test_hitl.py#L143).

### Dependency-manifest matching

`_is_dependency_path` normalizes Windows separators (`\\` → `/`), takes the trailing path segment (the basename), and returns true if that basename matches any pattern via case-sensitive `fnmatch.fnmatchcase` ([dangerous_diff.py:112-114](../../../src/wastech_orchestrator/core/dangerous_diff.py#L112)). Matching is on the basename only, so a manifest in any directory is detected, and most patterns are exact filenames while a few are globs (`requirements*.txt`, `*.csproj`, `*.fsproj`, `*.vbproj`).

`_DEPENDENCY_PATTERNS` holds **58** patterns ([dangerous_diff.py:10-69](../../../src/wastech_orchestrator/core/dangerous_diff.py#L10)) spanning many ecosystems, covering: Python (`pyproject.toml`, `uv.lock`, `poetry.lock`, `Pipfile`/`Pipfile.lock`, `requirements*.txt`, `setup.py`, `setup.cfg`), JS/TS (`package.json`, `package-lock.json`, `npm-shrinkwrap.json`, `pnpm-lock.yaml`, `yarn.lock`, `bun.lock`, `bun.lockb`), Rust (`Cargo.toml`, `Cargo.lock`), Go (`go.mod`, `go.sum`), Ruby (`Gemfile`, `Gemfile.lock`), PHP (`composer.json`, `composer.lock`), JVM (`pom.xml`, `build.gradle`, `build.gradle.kts`, `gradle.lockfile`, `libs.versions.toml`), Swift/CocoaPods/Carthage (`Package.swift`, `Package.resolved`, `Podfile`, `Podfile.lock`, `Cartfile`, `Cartfile.resolved`), Dart (`pubspec.yaml`, `pubspec.lock`), Elixir (`mix.exs`, `mix.lock`), .NET (`*.csproj`, `*.fsproj`, `*.vbproj`, `Directory.Packages.props`, `packages.lock.json`, `packages.config`, `paket.dependencies`, `paket.lock`), Clojure (`deps.edn`, `project.clj`), Scala (`build.sbt`), C/C++ (`conanfile.py`, `conanfile.txt`, `conan.lock`, `vcpkg.json`), Bazel (`MODULE.bazel`, `WORKSPACE`), Nix (`flake.nix`, `flake.lock`), and Terraform (`.terraform.lock.hcl`).

### How the verdict is consumed (cross-link, not re-documented)

The classifier is called by the agent node runner's post-edit guard after a workspace-write edit, on `git.changed_code_entries()` ([agent.py:251](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L251)). A non-`None` result drives a durable HITL approval; on denial the stage reconsiders once and re-classifies, failing closed to manual review if the diff is still dangerous ([agent.py:251-279](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L251), [agent.py:324-336](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L324)). A matching planning pre-approval can bypass the request ([agent.py:264-265](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L264), [agent.py:338-343](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L338)). Approval matching compares `risk` and the sorted `paths` of the persisted request against a fresh `DangerousDiff` ([agent.py:506-516](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L506)); the `risk`/`paths` are surfaced to the operator in the approval signal ([agent.py:491-503](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L491)). See [B30](B30-flow-node-runners.md) for the full guard, [B12](B12-hitl-and-typed-output.md) for the durable approval.

## Invariants & guarantees

- **Pure and deterministic.** No I/O, no globals mutated, no clock; output depends only on the input tuple ([dangerous_diff.py:82-109](../../../src/wastech_orchestrator/core/dangerous_diff.py#L82)). `DangerousDiff` is a frozen dataclass ([dangerous_diff.py:72](../../../src/wastech_orchestrator/core/dangerous_diff.py#L72)).
- **Stable ordering.** All three path tuples are sorted, so equal change-sets produce byte-identical `DangerousDiff` values — this is what lets the guard match a persisted request against a re-classification ([dangerous_diff.py:106-108](../../../src/wastech_orchestrator/core/dangerous_diff.py#L106)).
- **`paths` is the union** of `deleted_paths` and `dependency_paths`; a single path can appear in both subsets (renamed manifest) ([dangerous_diff.py:106-108](../../../src/wastech_orchestrator/core/dangerous_diff.py#L106)).
- **Only deletions and dependency changes are dangerous.** Plain modifications and additions (status `M`, `A`, `??`) are never flagged on their own; an ordinary modified file returns `None`, confirmed in [test_hitl.py:126-128](../../../tests/core/test_hitl.py#L126).
- **Basename-scoped, case-sensitive matching.** Directory does not matter; case does (`fnmatchcase`) ([dangerous_diff.py:113-114](../../../src/wastech_orchestrator/core/dangerous_diff.py#L113)).

## Dependencies

- **Uses:** [B22](B22-git-manager.md) (`ChangedPath` shape; `changed_code_entries` supplies the input tuple).
- **Used by:** [B30](B30-flow-node-runners.md) (the agent node runner's post-edit guard applies the classifier), surfacing into [B12](B12-hitl-and-typed-output.md) (the durable HITL approval).

## Audit candidates

- `src/wastech_orchestrator/core/dangerous_diff.py:88,90` — status prefix matching (`startswith("D")` / `startswith("R")`) — minor fragility: any future code path that ever fed a status string beginning with those letters for an unrelated state would be misclassified; today inputs come only from `changed_code_entries`, which emits Git name-status codes, so this is latent, not active. See [the audit](../../backlog/2026-06-21-audit.md).

## Tests

- `tests/core/test_hitl.py` — the only **direct** unit tests of the classifier: ordinary modify → `None` ([test_hitl.py:126-128](../../../tests/core/test_hitl.py#L126)), deletion + dependency → `other` with sorted union ([test_hitl.py:131-140](../../../tests/core/test_hitl.py#L131)), renamed manifest → `other` ([test_hitl.py:143-154](../../../tests/core/test_hitl.py#L143)).
- `tests/core/test_flow_node_runners.py` — exercise the classifier indirectly through the agent-node guard: dangerous diff with no approval → manual ([test_flow_node_runners.py:370-381](../../../tests/core/test_flow_node_runners.py#L370)), approved → proceeds ([test_flow_node_runners.py:404-415](../../../tests/core/test_flow_node_runners.py#L404)), denied then clean ([test_flow_node_runners.py:418-429](../../../tests/core/test_flow_node_runners.py#L418)), denied and still dangerous → manual ([test_flow_node_runners.py:432-443](../../../tests/core/test_flow_node_runners.py#L432)).
- `tests/core/test_orchestrator.py` — end-to-end: an implementation edit that deletes a file or touches `pyproject.toml` triggers exactly one `approval` ask ([test_orchestrator.py:1455-1489](../../../tests/core/test_orchestrator.py#L1455)).
- No dedicated `tests/core/test_dangerous_diff.py` exists; the classifier's own behavior is covered by `test_hitl.py` above, with the guard integration covered by the node-runner and orchestrator tests.
