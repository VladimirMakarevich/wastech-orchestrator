# Acceptance criteria

Status: **draft — to refine** Date: 2026-06-28 — [task hub](index.md)

Testable criteria for V1, grouped by area. Each maps to one or more requirements in [requirements.md](requirements.md). Numeric targets marked **[refine]** are locked once the eval baseline exists.

## Storage & config

- **AC-S1** With memory enabled, a completed task leaves a populated `.worc/memory/` tree (long-term / short-term / entities / audit), task-independent and gitignored.
- **AC-S2** `.worc/memory/` is never committed and never appears in a PR diff; install seeds the gitignore entry.
- **AC-S3** Nothing memory-related is written into `state.db`.
- **AC-S4** With memory **disabled** in config, no `.worc/memory/` writes occur and task behavior is byte-for-byte today's (regression-tested).
- **AC-S5** `CONFIG_SCHEMA_VERSION` is bumped; an older config without the memory block loads with safe defaults (no fatal error).

## Write path

- **AC-W1** Memory is written exactly once per task, at finalization, with **zero additional LLM calls** beyond the supervisor's existing summary turn (asserted in tests).
- **AC-W2** A candidate delta with missing/invalid evidence is rejected or quarantined, never silently promoted to long-term.
- **AC-W3** A failed/manual task writes short-term/failure memory but does not promote to long-term (except by explicit operator signal).
- **AC-W4** Tasks with external (web/MCP) context default to quarantine-unless-code-validated.

## Read path

- **AC-R1** Each of planning / implementation / review / fixing receives a packet file by `memory_path`; the agent is never handed the memory root.
- **AC-R2** Every packet respects the hard caps (lines/bullets/lessons/entities/episodic). **[refine]** exact caps.
- **AC-R3** Packet selection is deterministic: same inputs → same packet (reproducible in tests).
- **AC-R4** A node with no relevant memory gets an empty/minimal packet, not a fabricated one.

## Safety

- **AC-SF1** A secret-like string present in task artifacts never lands in any `.worc/memory/` file (redaction test with planted secrets) — leak count **0**.
- **AC-SF2** An `external-untrusted` / `agent-inferred` candidate never auto-promotes to durable long-term (poisoning drill).
- **AC-SF3** Every mutation produces an audit row with pre/post hashes and rationale; the log is append-only.
- **AC-SF4** A batch cleanup is preceded by a snapshot, and `restore` returns memory to the pre-cleanup state (rollback test).
- **AC-SF5** Trust level is assigned to every record and enforced at promotion (low-trust cannot behave as high-trust).

## Curation

- **AC-C1** `worc memory show | validate | compact | restore` exist and operate with a `--dry-run` plan before execution. **[refine]** final verbs.
- **AC-C2** The background cleanup runs only when no task is active, within its configured budget, and never delays the next task pickup.
- **AC-C3** Cleanup may demote/expire/quarantine/merge but **never** creates a new long-term lesson and **never** edits code/docs/skills.
- **AC-C4** A stale entity (referenced path/symbol removed) is detected and marked/quarantined by cleanup or validate.

## Cross-platform

- **AC-X1** Stored/compared path strings use POSIX form (`as_posix()`); records round-trip identically on Windows and POSIX.
- **AC-X2** The idle/cleanup control uses no `os.kill`/`signal` assumptions; the suite is green on Windows and POSIX.

## Outcome (gated by the eval baseline — blueprint §10.3)

- **AC-O1** ≥10% reduction in tokens or wall-clock for repeated-repo tasks. **[refine]**
- **AC-O2** ≥10% improvement in first-pass review/test success on repeated hotspots. **[refine]**
- **AC-O3** Stale-contradiction rate <5%; secret-leak rate 0; external-only long-term promotions 0.
- **AC-O4** No vector/graph infra is added without a measured recall/quality lift.
