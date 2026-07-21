# WRI-006 — Generated Codex OS-sandbox read-deny of the private home (fail-closed on Windows)

**Status:** open **Phase:** 2 (hard isolation) **Source:** [decision record](README.md); aligns with / subsumes [CODX-003](../codex-provider-improvements/p0-codx-003-enforce-deny-policy.md) for the runtime home **Dependencies:** WRI-005; coordinate with CODX-001, CODX-002, CODX-003

## Problem

Even after relocation (WRI-005), a Codex agent with broad filesystem reads could read a **known** absolute path; a real guarantee needs kernel-level read denial. Codex enforcement of denied reads is the open P0 [CODX-003](../codex-provider-improvements/p0-codx-003-enforce-deny-policy.md), whose bar is explicit: "redaction or prompt instructions do not count as enforcement," and it must cover indirect reads through interpreters, scripts and tools.

## Required outcome

Codex attempts are OS-sandbox-prevented from reading the private home (and the configured `denied_read_paths`) on macOS (Seatbelt) and Linux (Landlock). On Windows, where no Codex sandbox exists, the attempt **fails preflight under `strict_isolation`** rather than silently degrading. The Claude `Read`-deny (WRI-002) stays as defense in depth.

## In scope

- Generate the Codex sandbox policy that denies reads of the private home (plus existing `denied_read_paths`) via Seatbelt (macOS) and Landlock (Linux).
- Fail-closed on Windows under `strict_isolation`; an explicit, honest warning when `strict_isolation` is off.
- Make the policy non-weakenable by task / `extra_args` / flow, and cover indirect reads (interpreters, shell wrappers) per the CODX-003 bar.
- A blocked read is reported as a policy denial, not an infrastructure fallback.

## Acceptance criteria

- [ ] On macOS and Linux, a Codex attempt cannot read the private home by any means — direct shell read or indirect read through an interpreter/script.
- [ ] On Windows without a sandbox, the attempt fails preflight under `strict_isolation`; no false "isolated" status is reported.
- [ ] A denied read surfaces as a policy denial and does not trigger provider fallback.
- [ ] Task / `extra_args` / flow cannot remove or supersede the generated policy; the generated policy artifacts contain no secret file contents.
- [ ] The `denied_read_paths` defaults (`.env`, `secrets/**`) are also enforced for Codex, closing the runtime-home portion of CODX-003 (cross-reference and update CODX-003).
- [ ] Claude behavior and orchestrator-owned publish operations are unchanged.

## Verification

- Real-workspace tests for direct and indirect reads of the private home and every denied-path pattern, on POSIX.
- Host-matrix / opt-in smokes on Windows and POSIX; a Windows `strict_isolation` preflight-failure test.
- Fresh / resume parity tests; negative tests that allowed reads (repo, exchange) still work.
- Full security / isolation / provider suites and project gates.

## Out of scope

- Relocation itself (WRI-005).
- Hardening Claude's `Bash` beyond the existing `Read`-deny + the WRI-005 topology (a full OS sandbox / container for Claude is a separate, larger direction).
- Denied-command enforcement for Codex beyond read paths (the command half of CODX-003).

## Likely implementation areas

- src/wastech_orchestrator/providers/codex.py
- src/wastech_orchestrator/security/isolation.py and forbidden_args.py
- src/wastech_orchestrator/config/schema.py and validation.py
- tests/providers/test_codex_command.py, tests/providers/test_codex_run.py
- docs/configuration.md, docs/operations.md; cross-reference docs/backlog/codex-provider-improvements/p0-codx-003-enforce-deny-policy.md
