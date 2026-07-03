# B14 — Dangerous-Diff Guardrail

> Reconstructed from code (`core/dangerous_diff.py`) and tests (`tests/core/test_hitl.py`, `tests/core/test_flow_node_runners.py`, `tests/core/test_orchestrator.py`). The code is the only source of truth; this document was rebuilt from the implementation, not from prose or comments. Significant claims carry a `file:line` reference.

**Status:** documented · **Source modules:** `src/wastech_orchestrator/core/dangerous_diff.py`

## Responsibility

Pure, deterministic functions that inspect a tuple of changed repository paths and decide whether the diff requires human approval, under the operator's approval policy (`security.trust_level`) and always-ask floor (`security.protected_paths`). They return a `DangerousDiff` describing the risk and the exact normalized paths, or `None` when nothing needs approval.

This block is only the **decision logic**. It performs no I/O, holds no state, and knows nothing about the durable approval round-trip. The guard that consumes its verdict (write diff → evaluate → durable approval → reconsider-once → fail closed to manual) lives in the agent node runner ([B30](B30-flow-node-runners.md)); this document cross-links to it rather than re-documenting that flow.

## Public surface

- `evaluate_diff_gate(entries: tuple[ChangedPath, ...], trust_level: str, protected_paths: tuple[str, ...] = ()) -> DangerousDiff | None` — the **policy resolver** the guard calls; `None` means no approval is needed. It layers the always-ask floor (any changed path matching `protected_paths`) over the level (`strict` also gates the base diff-shape rule; `auto` gates nothing else).
- `classify_dangerous_diff(entries: tuple[ChangedPath, ...]) -> DangerousDiff | None` — the level-independent **base rule** used by `evaluate_diff_gate` for the `strict` branch: deletions/renames or dependency-manifest edits. `None` means an ordinary diff.
- `DangerousDiff` (frozen dataclass) — fields `risk: str`, `paths: tuple[str, ...]`, `deleted_paths: tuple[str, ...]`, `dependency_paths: tuple[str, ...]`, `protected_paths: tuple[str, ...]`.
- `_DEPENDENCY_PATTERNS: tuple[str, ...]` — module-private manifest/lock pattern set.
- `_is_dependency_path(path: str) -> bool` — module-private basename matcher.
- `_protected_hits(entries, protected_paths) -> tuple[str, ...]` — module-private; changed paths (new path or a rename's previous path) matching the `protected_paths` allowlist, via `wastech_orchestrator.globmatch.path_matches_any` (the same dialect [B23](B23-check-discovery.md) selection uses).
- `_deleted_paths(entries) -> set[str]` / `_combined_risk(...)` — module-private; the diff's deletion set (status `D` ⇒ `path`, rename ⇒ `previous_path`) and the risk-category derivation.

The input `ChangedPath` (`status`, `path`, `previous_path`) is owned by [B22](B22-git-manager.md) ([git_manager.py:139-145](../../../src/wastech_orchestrator/git_manager.py#L139)).

## Behavior

### Classification

The function scans every entry once ([dangerous_diff.py:86-94](../../../src/wastech_orchestrator/core/dangerous_diff.py#L86)). `status` is upper-cased, then:

- a status starting with `D` adds `entry.path` to the **deleted** set ([dangerous_diff.py:88-89](../../../src/wastech_orchestrator/core/dangerous_diff.py#L88));
- a status starting with `R` (rename) with a non-`None` `previous_path` adds the **previous** path to the deleted set — a rename-away is treated as deleting the original path ([dangerous_diff.py:90-91](../../../src/wastech_orchestrator/core/dangerous_diff.py#L90));
- both `entry.path` and `entry.previous_path` are independently tested against the dependency matcher; any hit is added to the **dependencies** set ([dangerous_diff.py:92-94](../../../src/wastech_orchestrator/core/dangerous_diff.py#L92)).

If neither set has anything, the diff is ordinary and `classify_dangerous_diff` returns `None`. Otherwise `risk` is assigned by which sets are non-empty: both → `other`, deletions only → `deletion`, dependencies only → `dependency`. The returned `DangerousDiff` carries the sorted **union** as `paths`, plus the two sorted subsets `deleted_paths` and `dependency_paths`.

Note that a single renamed manifest (e.g. `requirements.txt` → `requirements-dev.txt`) lands in **both** sets — the previous name is a deletion and a dependency match, the new name is a dependency match — so its risk is `other`, confirmed in [test_hitl.py:143-154](../../../tests/core/test_hitl.py#L143).

### Policy resolution (`evaluate_diff_gate`)

`evaluate_diff_gate` is what the guard actually calls; it applies the operator policy in two layers:

- **`protected_paths` (the floor), checked first.** `_protected_hits` collects every changed path whose new path _or_ a rename's previous path matches one of the `security.protected_paths` globs. Any hit requires approval at **any** `trust_level` — a create, edit, delete, or rename all count (unlike the base rule, which only flags deletions/dependencies).
- **`trust_level` (the threshold).** `strict` additionally folds in the `classify_dangerous_diff` base rule; `auto` gates nothing beyond the floor.

If there are no protected hits and (under `auto`) no base result, it returns `None`. Otherwise it returns a `DangerousDiff` whose `paths` is the sorted **union** of the deleted, dependency, and protected sets; `protected_paths` carries just the floor hits; and `risk` is `protected` when only protected paths are present, `other` for a mix of categories, else the base `deletion`/`dependency`. Because the result is a stable `DangerousDiff`, the durable resume/pre-approval matching (`risk` + sorted `paths`) works unchanged.

### Dependency-manifest matching

`_is_dependency_path` normalizes Windows separators (`\\` → `/`), takes the trailing path segment (the basename), and returns true if that basename matches any pattern via case-sensitive `fnmatch.fnmatchcase` ([dangerous_diff.py:112-114](../../../src/wastech_orchestrator/core/dangerous_diff.py#L112)). Matching is on the basename only, so a manifest in any directory is detected, and most patterns are exact filenames while a few are globs (`requirements*.txt`, `*.csproj`, `*.fsproj`, `*.vbproj`).

`_DEPENDENCY_PATTERNS` holds **58** patterns ([dangerous_diff.py:10-69](../../../src/wastech_orchestrator/core/dangerous_diff.py#L10)) spanning many ecosystems, covering: Python (`pyproject.toml`, `uv.lock`, `poetry.lock`, `Pipfile`/`Pipfile.lock`, `requirements*.txt`, `setup.py`, `setup.cfg`), JS/TS (`package.json`, `package-lock.json`, `npm-shrinkwrap.json`, `pnpm-lock.yaml`, `yarn.lock`, `bun.lock`, `bun.lockb`), Rust (`Cargo.toml`, `Cargo.lock`), Go (`go.mod`, `go.sum`), Ruby (`Gemfile`, `Gemfile.lock`), PHP (`composer.json`, `composer.lock`), JVM (`pom.xml`, `build.gradle`, `build.gradle.kts`, `gradle.lockfile`, `libs.versions.toml`), Swift/CocoaPods/Carthage (`Package.swift`, `Package.resolved`, `Podfile`, `Podfile.lock`, `Cartfile`, `Cartfile.resolved`), Dart (`pubspec.yaml`, `pubspec.lock`), Elixir (`mix.exs`, `mix.lock`), .NET (`*.csproj`, `*.fsproj`, `*.vbproj`, `Directory.Packages.props`, `packages.lock.json`, `packages.config`, `paket.dependencies`, `paket.lock`), Clojure (`deps.edn`, `project.clj`), Scala (`build.sbt`), C/C++ (`conanfile.py`, `conanfile.txt`, `conan.lock`, `vcpkg.json`), Bazel (`MODULE.bazel`, `WORKSPACE`), Nix (`flake.nix`, `flake.lock`), and Terraform (`.terraform.lock.hcl`).

### How the verdict is consumed (cross-link, not re-documented)

The classifier is called by the agent node runner's post-edit guard after a workspace-write edit, on `git.changed_code_entries()` ([agent.py:251](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L251)). A non-`None` result drives a durable HITL approval; on denial the stage reconsiders once and re-classifies, failing closed to manual review if the diff is still dangerous ([agent.py:251-279](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L251), [agent.py:324-336](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L324)). A matching planning pre-approval can bypass the request ([agent.py:264-265](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L264), [agent.py:338-343](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L338)). Approval matching compares `risk` and the sorted `paths` of the persisted request against a fresh `DangerousDiff` ([agent.py:506-516](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L506)); the `risk`/`paths` are surfaced to the operator in the approval signal ([agent.py:491-503](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L491)). See [B30](B30-flow-node-runners.md) for the full guard, [B12](B12-hitl-and-typed-output.md) for the durable approval.

## Invariants & guarantees

- **Pure and deterministic.** No I/O, no globals mutated, no clock; output depends only on the input tuple ([dangerous_diff.py:82-109](../../../src/wastech_orchestrator/core/dangerous_diff.py#L82)). `DangerousDiff` is a frozen dataclass ([dangerous_diff.py:72](../../../src/wastech_orchestrator/core/dangerous_diff.py#L72)).
- **Stable ordering.** All three path tuples are sorted, so equal change-sets produce byte-identical `DangerousDiff` values — this is what lets the guard match a persisted request against a re-classification ([dangerous_diff.py:106-108](../../../src/wastech_orchestrator/core/dangerous_diff.py#L106)).
- **`paths` is the union** of `deleted_paths` and `dependency_paths`; a single path can appear in both subsets (renamed manifest) ([dangerous_diff.py:106-108](../../../src/wastech_orchestrator/core/dangerous_diff.py#L106)).
- **Only deletions and dependency changes are dangerous under the base rule.** Plain modifications and additions (status `M`, `A`, `??`) are never flagged by `classify_dangerous_diff` on their own; an ordinary modified file returns `None`, confirmed in [test_hitl.py:126-128](../../../tests/core/test_hitl.py#L126). (A `protected_paths` match flags **any** change, including a plain edit — that is the floor, resolved in `evaluate_diff_gate`.)
- **`auto` gates only the floor.** Under `trust_level: auto` a deletion/dependency diff returns `None` unless a path also matches `protected_paths`; `strict` reproduces the pre-knob behavior (every deletion/dependency gates). A raised gate is fail-closed regardless of level — the level changes _which_ diffs gate, never whether an unanswered gate proceeds.
- **The floor is a superset check, never a hard-deny.** `protected_paths` can only _add_ approvals (it is checked before the level and cannot be lowered by any level); it never suppresses a change and does not affect the hard ceiling.
- **Basename-scoped, case-sensitive dependency matching.** Directory does not matter; case does (`fnmatchcase`).

## Dependencies

- **Uses:** [B22](B22-git-manager.md) (`ChangedPath` shape; `changed_code_entries` supplies the input tuple).
- **Used by:** [B30](B30-flow-node-runners.md) (the agent node runner's post-edit guard applies the classifier), surfacing into [B12](B12-hitl-and-typed-output.md) (the durable HITL approval).

## Tests

- `tests/core/test_hitl.py` — the only **direct** unit tests of the classifier: ordinary modify → `None` ([test_hitl.py:126-128](../../../tests/core/test_hitl.py#L126)), deletion + dependency → `other` with sorted union ([test_hitl.py:131-140](../../../tests/core/test_hitl.py#L131)), renamed manifest → `other` ([test_hitl.py:143-154](../../../tests/core/test_hitl.py#L143)).
- `tests/core/test_flow_node_runners.py` — exercise the classifier indirectly through the agent-node guard: dangerous diff with no approval → manual ([test_flow_node_runners.py:370-381](../../../tests/core/test_flow_node_runners.py#L370)), approved → proceeds ([test_flow_node_runners.py:404-415](../../../tests/core/test_flow_node_runners.py#L404)), denied then clean ([test_flow_node_runners.py:418-429](../../../tests/core/test_flow_node_runners.py#L418)), denied and still dangerous → manual ([test_flow_node_runners.py:432-443](../../../tests/core/test_flow_node_runners.py#L432)).
- `tests/core/test_orchestrator.py` — end-to-end: an implementation edit that deletes a file or touches `pyproject.toml` triggers exactly one `approval` ask ([test_orchestrator.py:1455-1489](../../../tests/core/test_orchestrator.py#L1455)).
- `tests/core/test_dangerous_diff.py` — the `trust_level` × diff-shape × `protected_paths` matrix: `strict` gates deletions/dependencies; `auto` gates neither unless a `protected_paths` match is present; a protected path flags a plain edit too; the union `paths`, `protected_paths` subset, and `risk` (`protected` / mixed → `other`) are correct.
- `tests/test_globmatch.py` — the shared repo-relative glob matcher (`**/*.md`, `docs/**`, single-segment `*.md`, `path_matches_any`) used by both this block and [B23](B23-check-discovery.md).
- Guard integration in `tests/core/test_flow_node_runners.py`: under `auto` a deletion diff proceeds with **no** approval ask; a `protected_paths` match still asks; a raised gate still fails closed on deny/timeout.
