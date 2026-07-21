# WRI-006 — Add the cross-platform isolation verification gate

**Status:** open **Milestone:** 1 **Source:** [decision record](README.md) **Dependencies:** WRI-002, WRI-003, WRI-007

## Problem

Cross-platform behavior is a hard repository invariant, but the current CI workflow runs only on Ubuntu. The previous WRI-006 duplicated Codex policy ownership, proposed custom macOS/Linux policy generation, and treated native Windows as unsupported. It also mixed fake-CLI wiring evidence with proof of host sandbox enforcement.

## Required outcome

Windows, macOS, and Linux all run deterministic isolation coverage in CI. A separate no-model Codex host smoke proves the permission profile on the actual runner whenever Codex is available. Claude's supported-host Bash sandbox receives real-host coverage in a protected integration lane if the CLI exposes no credential-free runner; deterministic CI must label generated settings/fake-CLI evidence as wiring only. No ordinary unit-test job authenticates to a model provider or spends model tokens.

## CI matrix

- Run the isolation-relevant pytest suites on `ubuntu-latest`, `macos-latest`, and `windows-latest` with the project's supported Python version(s). Keep static gates on Ubuntu if runtime parity is proven elsewhere.
- Exercise provider command generation, typed layout, exchange publication, lifecycle sealing/restoration, redaction, and security validation on all three OSes.
- Inject platform/path behavior for deterministic edge cases, but also run filesystem tests natively on each runner.
- Treat WSL2 as the Linux policy branch and cover it in a documented scheduled/manual smoke when hosted CI cannot supply WSL. Native Windows remains its own required branch.

## Codex capability smoke

When a compatible Codex CLI is installed, use `codex sandbox` (or its supported no-model equivalent), never `codex exec`, to test the exact generated profile:

1. Record OS, architecture, Codex version, the native Windows sandbox mode surface actually exposed by the CLI (re-verify on a Windows host: 0.144.4 marks the older elevated/experimental Windows sandbox feature flags removed), the resolved `CODEX_HOME` identity, and generated-profile/rules digests.
2. Confirm a direct read of the private fixture is denied.
3. Confirm a shell/interpreter-mediated read of the same fixture is denied.
4. Confirm repository and exchange reads are allowed.
5. Confirm exchange writes are denied.
6. Confirm repository writes are denied for `read-only` and allowed for `workspace-write`.
7. Confirm network remains disabled for an offline profile without making an external service a flaky dependency.
8. Confirm project config is untrusted, no unapproved external allow-rule layer is active, hooks/custom subagents are disabled, and the effective MCP/app/plugin/computer-use inventory contains no unresolved local-filesystem surface.

The smoke is a capability probe, not an exact-version gate. If the profile surface changes or enforcement cannot be demonstrated, strict isolation must fail before model execution. Fake CLIs may prove argv, config, and error routing but cannot satisfy these host-enforcement checks.

## Claude capability matrix

- On macOS/Linux/WSL2, test the exact adapter-owned settings with sandbox availability required, unsandboxed escape disabled, no excluded commands, and controlled settings/MCP/tool surfaces. Prove direct and shell/interpreter-mediated private reads plus exchange/Git writes fail while permitted repository operations succeed.
- On native Windows, prove strict workspace-write omits Bash and still permits the intended Edit/Write tools. Separately prove the operator-only unsafe branch is labeled unisolated.
- If the Claude CLI has no credential-free sandbox runner, keep normal CI deterministic and run the smallest authenticated host smoke in a secret-protected required integration lane. Do not report fake CLI or settings serialization as OS-enforcement evidence.
- Record Claude version, settings/tool inventory, managed-policy trust result, OS/WSL branch, sandbox dependency result, and policy digest. Any unresolved extension surface or weaker managed setting fails strict isolation.

## Cross-platform edge cases

- POSIX paths, Windows drive paths, UNC paths, spaces, non-ASCII, case variation, and long paths within supported runner limits.
- Symlinks on POSIX; symlinks, junctions, and reparse points on Windows, with tests skipped only when the runner truly cannot create the fixture and the missing capability is reported.
- Hard links/file-identity aliases and special-file rejection across workspace/exchange/private/control roots; Windows device-name/trailing-dot task ids, case-fold collisions, and NTFS alternate data streams.
- Locked and read-only exchange files, cross-volume copy seam, interrupted seal/restore, and atomic replacement semantics.
- CRLF/LF content and manifest hashing independent of text newline translation.
- Bounded deny-glob expansion on Linux/WSL/native Windows and exact subtree behavior on every OS.
- Fresh/resume parity and both provider permission profiles.
- Native-Windows standalone package helper discovery/PATH augmentation, elevated/unelevated selection, helper-launch failure, and the existing exit-0 false-success signature guard.
- Strict-mode migration/rejection of legacy Codex sandbox config and the deliberately unisolated operator-only full-access branch under `strict_isolation: false`.
- Claude sandbox availability/failure on macOS/Linux/WSL2, native-Windows no-Bash behavior, safe/config-isolation and resume parity, settings-array merge hazards, and operator-only unsafe mode.
- Provider root exit with background/reparented descendants; Windows Job Object (or equivalent), Linux/WSL containment, macOS process-group/descendant tracking, PID reuse, and the quiescence-before-manifest ordering.

## Acceptance criteria

- [ ] The CI workflow has required Windows, macOS, and Linux isolation jobs.
- [ ] All deterministic tests run without provider credentials or real model calls; any separate authenticated Claude host lane is isolated, minimal, and clearly identified.
- [ ] Codex host smokes combine no-model sandbox execution with effective config/rules/tool-surface inspection and distinguish `unsupported`, `policy failed`, and `passed` without silently downgrading strict isolation; `unsupported` maps to the pre-model `CAPABILITY_UNAVAILABLE` classification and `policy failed` to the non-fallback security result.
- [ ] Native Windows tests the supported Codex sandbox rather than expecting a preflight failure merely because of the OS.
- [ ] Claude evidence distinguishes built-in tool policy from Bash OS enforcement. Supported-host enforcement is exercised on a real sandbox; native Windows proves strict no-Bash behavior.
- [ ] Fake-CLI, generated-policy, and real-host evidence are labeled separately in test/docs output.
- [ ] WSL coverage and any hosted-runner limitation are explicitly documented.

## Verification

- Validate the workflow matrix and required-job configuration.
- Run the isolation test selection on all three hosted OSes.
- Run the no-model Codex capability smoke on each supported host image where the CLI can be installed.
- Run the Claude real-host smoke on macOS/Linux/WSL2 through a credential-free runner if available; otherwise through the protected authenticated lane defined above.
- Run the project gates required by `testing.md`; no platform-specific skip may hide an untested mandatory branch.

## Out of scope

- Owning the Codex profile implementation (WRI-003).
- Authenticated end-to-end model calls in ordinary deterministic CI jobs.
- Inventing a Claude sandbox for native Windows or claiming the built-in sandbox covers non-Bash tools.

## Likely implementation areas

- .github/workflows/ci.yml
- tests/providers/
- tests/security/
- tests/core/
- scripts/ or a test helper for the no-model Codex capability smoke
- docs/operations.md and packaged guide
