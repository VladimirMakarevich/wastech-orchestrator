# CODX-007 — Refresh model defaults and establish one source of truth

**Status:** open
**Priority:** P1
**Source finding:** CXP-07
**Dependencies:** CODX-006
**Officially supported CLI versions:** `codex` **≥ 0.144.4**

## Problem

The provider can pass current model IDs, but the installer, packaged config, flow comments, tests and
documentation still pin or mention different GPT-5.4/GPT-5.5 defaults. New installations therefore
start with a stale model and documentation does not provide one authoritative policy.

## Required outcome

Use one canonical Codex model-default policy across code, generated configuration, packaged flows,
tests and documentation. Keep arbitrary valid future model IDs pass-through compatible.

## In scope

- Make an empty model string, meaning Codex CLI/account default, the canonical shipped default.
- List current public model IDs as examples rather than a hard allowlist.
- Remove stale GPT-5.4/GPT-5.5 default assertions and comments.
- Centralize installer/config example defaults so they cannot drift.
- Keep model/provider vendor validation compatible with future GPT model IDs.
- Document entitlement and model-unavailable failure behavior.

## Acceptance criteria

- [ ] Fresh install writes the same canonical Codex model default as packaged config.
- [ ] The canonical default is empty and delegates selection to the CLI/account.
- [ ] Configuration docs explain how to pin gpt-5.6-sol, gpt-5.6-terra and gpt-5.6-luna.
- [ ] GPT-5.4/GPT-5.5 remain only in historical analysis where historically accurate.
- [ ] Flow comments and live examples no longer present stale IDs as current defaults.
- [ ] Future/non-public model IDs are not rejected solely because they are absent from a static list.
- [ ] Model/reasoning compatibility failures are explicit and do not cause infrastructure fallback.
- [ ] Round-trip and installer tests enforce one source of truth.
- [ ] Documentation states that private GPT-6 availability is not equivalent to a tested public
      provider contract.

## Verification

- Search-based regression test or canonical constant test preventing default drift.
- Installer/config round-trip tests.
- Provider command test for each current public exemplar and an unknown future ID.
- Documentation link check.
- Full project quality gates.

## Out of scope

- Maintaining a complete hardcoded model catalog.
- Automatically changing an existing operator's explicit model pin.
- Purchasing or provisioning model entitlement.
- Implementing reasoning modes; CODX-006.

## Likely implementation areas

- src/wastech_orchestrator/install/config_writer.py
- src/wastech_orchestrator/packaged/config.example.yaml
- src/wastech_orchestrator/packaged/flows
- src/wastech_orchestrator/config and tests/install
- docs/configuration.md, docs/task-authoring.md and functional docs
