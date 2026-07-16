# CODX-009 — Add typed and path-safe image inputs

**Status:** postponed
**Priority:** P1
**Source finding:** CXP-08
**Dependencies:** CODX-008
**Officially supported CLI versions:** `codex` **≥ 0.144.4**

## Problem

Codex CLI supports image attachments for fresh and resumed prompts, but AgentRunRequest has no typed
image input. Using extra_args would expose arbitrary host paths and bypass the orchestrator's path
and capability policies.

## Required outcome

Flows and tasks can attach approved local images through a typed request field. Every path is
validated against configured roots and projected to Codex without exposing additional filesystem
authority.

## In scope

- Add typed image/attachment paths to flow/node request models.
- Require an explicit image-input capability grant.
- Resolve paths canonically and reject traversal, symlink escape and disallowed roots.
- Validate existence, file type, count and configured size limits before provider invocation.
- Generate correct fresh and resume image arguments.
- Persist only redacted metadata needed for audit.
- Document supported formats, limits and failure behavior.

## Acceptance criteria

- [ ] One or more allowed images reach fresh Codex invocation through documented image flags.
- [ ] Images can be attached to resume prompts with the correct resume grammar.
- [ ] Relative paths resolve only inside explicitly allowed roots.
- [ ] Absolute outside-root paths, traversal and symlink escapes are rejected before spawn.
- [ ] Directories, missing files, unsupported formats, excessive counts and oversized files fail
      validation clearly.
- [ ] The task cannot use image input to add a writable directory or broader read permission.
- [ ] Request/result artifacts do not copy image bytes or leak sensitive absolute paths beyond the
      existing path-redaction policy.
- [ ] Claude and flows without images remain unchanged.
- [ ] Cross-provider fallback handles attachments explicitly instead of silently dropping them.

## Verification

- Path-security tests for Windows drives/UNC paths and POSIX roots/symlinks.
- Fresh/resume argv tests with one and multiple images.
- Limit and unsupported-format tests.
- Capability-denied tests proving no provider process starts.
- Opt-in visual-input smoke against a supported model.
- Full project quality gates.

## Out of scope

- Generating or editing images.
- Downloading remote image URLs.
- OCR or image preprocessing.
- Persisting image binaries as orchestrator artifacts.
- General arbitrary-file attachments.

## Likely implementation areas

- src/wastech_orchestrator/providers/base.py and codex.py
- src/wastech_orchestrator/config/schema.py
- src/wastech_orchestrator/core/flow request construction
- src/wastech_orchestrator/routing/router.py
- tests/providers, tests/core and tests/security
- docs/configuration.md and docs/task-authoring.md
