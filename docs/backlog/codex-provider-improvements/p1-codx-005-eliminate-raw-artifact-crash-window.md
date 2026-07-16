# CODX-005 — Eliminate the raw-artifact crash window

**Status:** postponed
**Priority:** P1
**Source finding:** CXP-05
**Dependencies:** CODX-004
**Officially supported CLI versions:** `codex` **≥ 0.144.4**

## Problem

The process runner streams raw stdout directly into the durable attempt directory. Codex also writes
the raw last message there. Redaction happens only after the child exits. A daemon crash, hard kill
or exception between those steps can leave secrets and session identifiers in durable artifacts.

## Required outcome

No unredacted provider output may ever be written to a registered or durable artifact path. Every
terminal path, including cancellation and process failure, must either publish a redacted artifact
atomically or publish no artifact.

## In scope

- Route raw stdout and Codex last-message output to private per-attempt scratch storage.
- Apply redaction and session-ID normalization before publishing durable files.
- Publish stdout, events, stderr and last-message artifacts atomically.
- Clean scratch files after success, timeout, cancellation, parse failure and daemon-managed stop.
- Keep output processing bounded for large stdout streams.
- Cover the shared process spine where behavior affects both providers.

## Acceptance criteria

- [ ] The child process never receives a durable artifact path for raw stdout or last-message.
- [ ] Durable stdout/events/stderr/last-message files contain only redacted content.
- [ ] SIGINT/CTRL_BREAK, timeout, hard kill and injected exceptions leave no raw durable file.
- [ ] Private scratch files are permission-restricted where the platform supports it.
- [ ] Scratch cleanup is best effort and cannot hide the original provider error.
- [ ] A cleanup failure is observable without copying secret content into logs.
- [ ] Session identifiers remain available only in the authoritative state location after
      normalization.
- [ ] Existing artifact retention modes and paths remain compatible.
- [ ] Fresh and resume runs behave identically.

## Verification

- Fault-injection tests at spawn, streaming, process exit, parsing, redaction and atomic publish.
- Cancellation/timeout tests that inspect both durable and scratch directories.
- Unique-secret scan over the entire temporary run root after every injected failure.
- Large-output test confirming bounded memory behavior.
- Windows and POSIX process-path tests.
- Full project quality gates.

## Out of scope

- Redacting structured_output after parsing; CODX-004.
- Changing artifact retention policy names.
- Encrypting all orchestrator artifacts at rest.
- Live remote log streaming.

## Likely implementation areas

- src/wastech_orchestrator/providers/process.py
- src/wastech_orchestrator/providers/_adapter_base.py
- src/wastech_orchestrator/providers/codex.py
- src/wastech_orchestrator/providers/artifacts.py
- tests/providers and tests/process-control paths
