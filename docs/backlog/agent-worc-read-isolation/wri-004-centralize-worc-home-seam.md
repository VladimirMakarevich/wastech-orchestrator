# WRI-004 — Introduce a typed runtime/control/exchange layout

**Status:** open **Milestone:** 0 (foundation) **Source:** [decision record](README.md), [follow_ups.md](../follow_ups.md) **Dependencies:** —

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

## Gap handed over by WRI-001 (decoupled build)

WRI-001 shipped **before** this task, so it computes the exchange root inline instead of reading a typed layout field. When WRI-004 lands it must absorb that shim:

- `providers/artifacts.py` defines the constant `EXCHANGE_HOME = ".worc-io"`, and `core/orchestrator.py.__init__` sets `self._exchange_root = Path(config.repo.local_path) / EXCHANGE_HOME`. Replace that inline construction with `layout.exchange_root` and remove the ad-hoc join. The same `.worc-io` literal is duplicated in `git_manager.py` (`RUNTIME_EXCLUDED_DIRS`, `RUNTIME_GITIGNORE_LINES`, `_IGNORE_PROBE_PATHS`); fold those onto the typed layout too.
- **Do not** change the exchange builders'/publisher's `exchange_root` **parameter** convention — `exchange_task_dir(exchange_root, …)`, `exchange_node_run_dir(exchange_root, …)`, `exchange_latest_run_file(exchange_root, …)`, and the `providers/exchange.py` publisher all take the root as an argument, mirroring the existing `task_artifact_dir(artifacts_root, …)`. WRI-004 only changes where the value comes from, not the signatures.
- The pre-launch invariant `assert_exchange_current_task_only(self._exchange_root, …)` and the interim `clear_exchange_task_dir(self._exchange_root, …)` calls in the orchestrator should switch to `layout.exchange_root` as part of the same sweep.

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

- [ ] Composition constructs one layout and consumers receive the correct field; Core does not import CLI.
- [ ] Config/flows/tools/guide use `control_home`; DB/logs/memory/reports/HITL/process control use `private_home`; exchange consumers have an explicit but unused `exchange_root`.
- [ ] Control/private roots, frozen control bundles, an explicit `--env-file`, and provider credential/config homes are represented as internal deny targets without being added to redaction/skill-scanning config globs.
- [ ] The default resolved paths and all observable behavior are byte-for-byte/path-for-path unchanged.
- [ ] No consumer reconstructs a private runtime path from `repo_root / ".worc"`; legitimate control-home and gitignore literals remain allowed.
- [ ] Persisted/displayed path strings use `Path.as_posix()`; filesystem operations keep `Path` values rather than round-tripping through display strings.
- [ ] Windows drive/UNC, macOS, Linux, relative configured repo paths, symlinked repo roots, and linked Git worktrees are covered through injected path/platform seams.
- [ ] The broken sibling link to `follow_ups.md` is fixed and the real follow-up is closed only after the code lands.

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
