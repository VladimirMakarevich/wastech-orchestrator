# WRI-004 — Introduce a typed runtime/control/exchange layout

**Status:** implemented **Milestone:** 0 (foundation) **Source:** [decision record](README.md), [follow_ups.md](../follow_ups.md) **Dependencies:** —

## Problem

The code uses one `artifacts_root` and reconstructs `<repo>/.worc` independently in the CLI, Core, memory, Git, output-policy, validation, install, and process-control paths. Treating every `.worc` literal as one replaceable value is also wrong: some consumers need the discoverable operator control plane, while others need private runtime state or the agent-facing exchange.

The previous dependency direction was backwards: splitting writers first and introducing a layout seam later would force the same call graph to be threaded twice.

## Required outcome

Introduce one provider-neutral immutable layout object, constructed in the composition/CLI boundary and passed to consumers by dependency injection:

- `repo_root` — target working tree.
- `control_home` — `<repo>/.worc`, holding `config.yaml`, guide, flows, tools, and install metadata.
- `private_home` — initially the same `<repo>/.worc`, holding state, audit, secrets, memory, reports, rejected runtime tasks, and process-control files.
- `exchange_root` — `<repo>/.worc-io`, reserved for WRI-001.
- `internal_denied_paths` (or an equivalent typed policy input) — `control_home`, `private_home`, frozen control bundles, and resolved operator/runtime secret sources outside those roots, notably an explicit `--env-file` and provider-owned auth/config homes. It is internal provider policy, not the overloaded public `security.denied_read_paths` list.

This task changes no on-disk behavior. It makes each consumer declare which surface it owns so WRI-001 and WRI-005 can change destinations without another global literal hunt.

## Gap handed over by WRI-001 (decoupled build) — consolidated

WRI-001 shipped **before** this task and computed the exchange root inline. WRI-004 has absorbed that shim:

- `providers/artifacts.EXCHANGE_HOME` was removed; the canonical `.worc-io` name now lives in `runtime_layout.EXCHANGE_HOME_DIRNAME`, and `core/orchestrator.py.__init__` reads `layout.exchange_root` instead of joining `Path(config.repo.local_path) / EXCHANGE_HOME`. The duplicated `.worc-io` literals in `git_manager.py` (`RUNTIME_EXCLUDED_DIRS`, `_RUNTIME_IGNORE_ROOTS`, `RUNTIME_GITIGNORE_LINES`) are now built from the shared constants; the per-root ignore logic stays explicit.
- The exchange builders'/publisher's `exchange_root` **parameter** convention is unchanged — `exchange_task_dir(exchange_root, …)`, `exchange_node_run_dir(exchange_root, …)`, `exchange_latest_run_file(exchange_root, …)`, and the `providers/exchange.py` publisher still take the root as an argument (mirroring `task_artifact_dir(artifacts_root, …)`). WRI-004 changed only where the value comes from.
- The pre-launch `assert_exchange_current_task_only(self._exchange_root, …)` and interim `clear_exchange_task_dir(self._exchange_root, …)` calls now flow from `layout.exchange_root` via `self._exchange_root`.

**Frozen-bundle shim (handed forward to WRI-010):** the `internal_denied_paths` policy names the control/private homes, the resolved env-file, and provider auth/config homes today. The live control plane's **frozen control bundle** is a further deny target owned by WRI-010 and is intentionally absent from `InternalDenyPolicy` until then (see wri-010).

## In scope

- Move the canonical directory names into a leaf module importable by CLI, composition, Core interfaces, Git Manager, and memory without cycles.
- Add a typed layout factory whose default reproduces today's paths exactly.
- Thread `control_home` to config discovery/install/upgrade, flow registry, tool registry, and shipped guide operations.
- Thread `private_home` to state DB, logs/artifacts, ledger, memory, security reports, HITL, rejected runtime tasks, pid/stop/children files, and default `.env` resolution.
- Resolve the actual default/explicit env-file path before provider construction and thread it through the internal deny policy; do not assume it is a child of `private_home`.
- Thread `exchange_root` without writing to it yet.
- Keep Git ignore/exclude logic explicit: `.worc/` is a control-home repo footprint, not an arbitrary private-home path.
- Replace `MemoryLayout.for_repo(repo_root)` with a constructor that receives the resolved private home.
- Update the existing duplicate-home follow-up only when the implementation is complete.

## Acceptance criteria

- [x] Composition constructs one layout and consumers receive the correct field; Core does not import CLI (`composition.build_orchestrator`/`build_providers` take `layout`; `runtime_layout` is a stdlib-only leaf — `lint-imports` green).
- [x] Config/flows/tools/guide use `control_home`; DB/logs/memory/reports/HITL/process control use `private_home` (via `worc_home_for` → `layout.private_home`); exchange consumers have an explicit but unused `exchange_root`.
- [x] Control/private roots, an explicit `--env-file`, and provider credential/config homes are represented as internal deny targets (`InternalDenyPolicy`) without being added to redaction/skill-scanning config globs. **Frozen control bundles are deferred to WRI-010** (documented shim above).
- [x] The default resolved paths and all observable behavior are byte-for-byte/path-for-path unchanged (`RuntimeLayout.default` reproduces `<repo>/.worc` and `<repo>/.worc-io`; covered by `tests/core/test_composition_layout.py` and `tests/test_runtime_layout.py`).
- [x] No consumer reconstructs a private runtime path from `repo_root / ".worc"`; legitimate control-home config-default/validation literals remain allowed (guarded by `tests/test_worc_home_call_sites.py`, an AST call-site check — not a text ban).
- [x] Persisted/displayed path strings use `Path.as_posix()`; filesystem operations keep `Path` values rather than round-tripping through display strings.
- [x] Windows drive/UNC, macOS, Linux, and relative configured repo paths are covered through the injected `RuntimeLayout.default` seam; the layout performs **no** path resolution, so symlinked repo roots and linked Git worktrees pass through unchanged (nothing to canonicalize here — alias/symlink identity checks belong to the exchange/provider tasks).
- [x] The header `follow_ups.md` link resolves (`../follow_ups.md`) and the real follow-up (2026-06-22 `_WORC_HOME` dedup) is closed now that the code has landed.

## Verification

- Unit tests for layout resolution on POSIX and Windows path fixtures.
- Wiring tests proving every named consumer receives the intended field.
- A guard test over path-construction call sites, not a brittle repository-wide ban on the `.worc` text.
- Full project gates with no behavior/documentation drift.

## Out of scope

- Moving an artifact to the exchange (WRI-001).
- Portable task/node identity validation and path-builder containment (WRI-008).
- Freezing and protecting live control inputs (WRI-010).
- Provider enforcement (WRI-002/003).
- Changing the default private-home location (WRI-005).

## Likely implementation areas

- src/wastech_orchestrator/runtime_layout.py (new leaf module or equivalent)
- src/wastech_orchestrator/cli.py and cli_shell.py
- src/wastech_orchestrator/composition.py
- src/wastech_orchestrator/core/orchestrator.py and flow wiring
- src/wastech_orchestrator/git_manager.py
- src/wastech_orchestrator/memory/paths.py
- src/wastech_orchestrator/core/flow/output_policy.py
- src/wastech_orchestrator/config and install
- src/wastech_orchestrator/process_control.py
- tests/ and docs/backlog/follow_ups.md
