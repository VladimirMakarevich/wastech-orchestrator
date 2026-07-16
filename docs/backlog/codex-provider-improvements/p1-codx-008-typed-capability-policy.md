# CODX-008 — Add a typed Codex capability policy

**Status:** postponed
**Priority:** P1
**Source finding:** CXP-08
**Dependencies:** CODX-001, CODX-002
**Officially supported CLI versions:** `codex` **≥ 0.144.4**

## Problem

CODX-002 now places every Codex attempt behind a controlled boundary and disables ungranted
external features. The orchestrator still has no typed way to request and globally cap apps, MCP,
browser/computer use, plugins, hooks, image generation, fast mode, personality, verbosity, or
native multi-agent capabilities; they can only remain at the safe baseline rather than be granted
selectively.

## Required outcome

Introduce a typed capability request and effective-capability policy. Flow/node configuration may
request supported features, global configuration defines the maximum authority/cost ceiling, and
CodexProvider projects only the resulting effective set into controlled CLI/config state.

## In scope

- Define typed capability fields for the currently supported Codex surface.
- Separate external-I/O/security capabilities from presentation and cost/performance settings.
- Add global ceilings and safe defaults.
- Resolve requested versus allowed capabilities deterministically before provider invocation.
- Expose provider capability support through preflight.
- Persist a redacted effective-capability manifest per attempt.
- Keep provider-specific syntax inside CodexProvider.
- Update schema/version, loader, validation, flows and operator documentation.

## Acceptance criteria

- [ ] Apps, MCP, browser, computer-use, plugins, hooks, image generation and multi-agent are disabled
      unless explicitly granted.
- [ ] A task cannot raise any global security, network, concurrency, token or cost ceiling.
- [ ] Unsupported requested capabilities fail before the model run with a typed non-infrastructure
      error.
- [ ] Effective capabilities are identical for fresh and resume invocations.
- [ ] The manifest records requested, allowed and effective values without secrets.
- [ ] Capability resolution is deterministic and unit-testable without invoking Codex.
- [ ] Codex CLI/config syntax remains absent from core and flow layers.
- [ ] Existing flows with no capability block preserve their safe behavior.
- [ ] Cross-provider fallback drops or remaps provider-specific capabilities explicitly.
- [ ] Free-form extra_args are not used as the public API for any capability.

## Verification

- Resolution matrix tests: unset, requested, globally denied, unsupported and granted.
- Security tests proving node/task values cannot raise ceilings.
- Fresh/resume command/config snapshots.
- Preflight tests against missing and present feature support.
- Artifact redaction tests for capability manifests.
- Full architecture/import, config, provider and routing test suites.

## Out of scope

- Granting all modern features by default.
- Building provider-agnostic implementations of Codex-native tools.
- Image path transport, covered by CODX-009.
- Custom Ultra fan-out, excluded by CODX-006.
- Capacity-aware scheduling across tasks.

## Likely implementation areas

- src/wastech_orchestrator/config/schema.py, loader.py and validation.py
- src/wastech_orchestrator/providers/base.py and codex.py
- src/wastech_orchestrator/core/flow schema/resolution
- src/wastech_orchestrator/routing/router.py
- tests/config, tests/core, tests/providers and tests/routing
- docs/configuration.md, docs/task-authoring.md and docs/operations.md
