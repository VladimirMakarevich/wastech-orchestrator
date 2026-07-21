# WRI-002 — Enforce a Claude read-deny of the private home

**Status:** open **Phase:** 1 (hygiene) **Source:** [decision record](README.md) **Dependencies:** WRI-001

## Problem

Claude can read the entire private home (`.worc/`) today. The existing `security.denied_read_paths` mechanism (`--disallowedTools Read(<glob>)`) is the right shape but must **not** be reused for `.worc/**`: it is `REPLACES`-not-extends and is overloaded with two other jobs — the redaction pass globs its patterns and reads matched files as "secrets", and the skill scanner skips files matching it. Pointing it at `.worc/**` would make the orchestrator read `state.db` and the logs as secrets and would drift as new subdirs appear.

## Required outcome

Claude attempts cannot read the private home through the `Read` tool. Enforcement is a **dedicated, internal, non-weakenable** deny appended to `--disallowedTools`, computed from the private-home path — separate from `denied_read_paths`, and not feeding redaction or the skill scanner. The exchange (`.worc-io/`) stays readable because it is a sibling path the `.worc` glob does not match.

## In scope

- Compute the private-home deny internally and append a deny of the private home to Claude's `--disallowedTools`, alongside the existing secret and native-memory denies.
- **Anchor the glob to the resolved absolute home, not a bare `.worc/`.** `_deny_read_tools_for` forwards `denied_read_paths` patterns verbatim with no anchoring, but the paths Claude is handed in the context footer are `<repo.local_path>/.worc/logs/…` — never a leading `.worc/`. A relative `Read(.worc/**)` glob would not match the path the tool is invoked with. Follow the pattern `_native_memory_deny_tools` already uses ([claude.py:195-213](../../../src/wastech_orchestrator/providers/claude.py#L195-L213)): emit a `//`-anchored absolute POSIX glob (`"//" + private_home.resolve().as_posix().lstrip("/") + "/**"`) plus the bare-directory form.
- **Cover dotfiles under the home.** Confirm the deny actually matches `.worc/.env` (and any other hidden entry) — `**` skips leading-dot entries in many matchers. If it does not, add an explicit dotfile-covering deny; do not rely on the separate root-level `.env` deny, which is a different path than `<home>/.env`.
- Guarantee the exchange root is not denied.
- Make the deny non-weakenable: task, `extra_args`, and flow nodes cannot remove or supersede it; the validator rejects any attempt to.
- Document honestly the Phase-1 residual: the `Read`-tool deny does not stop `Bash`-based reads (`cat`, interpreters) under `workspace-write`; that hole closes in Phase 2 (WRI-005/WRI-006).

## Acceptance criteria

- [ ] A Claude run has an absolute-anchored deny of the private home (a `//`-anchored glob, e.g. `Read(//abs/repo/.worc/**)`) in `--disallowedTools`; the exchange root is absent from the deny.
- [ ] A `Read` of the invoked footer path for a private-home file (e.g. `<repo>/.worc/state.db`) is denied — proving the anchoring matches the path the tool is actually called with, not a bare `.worc/…`.
- [ ] A `Read` of `<repo>/.worc/.env` is denied — proving dotfile coverage under the home.
- [ ] The deny is present for both the read-only and workspace-write profiles, and for fresh and resume attempts.
- [ ] Task prompt / `extra_args` / flow node cannot remove or override the deny; the validator rejects unsafe attempts.
- [ ] The private-home deny is not added to `denied_read_paths` and does not trigger any redaction-harvest read of the home.
- [ ] Claude can still read the exchange and repo; a `Read` of `.worc/.env` is denied.
- [ ] Docs (security.md rule, configuration/guide) state the guarantee and the Phase-1 `Bash` residual explicitly; no claim of full enforcement.

## Verification

- Table-driven Claude argv tests: deny present, exchange absent from the deny, both profiles, fresh + resume.
- Real-workspace test: a `Read` of a private-home file is denied, a `Read` of an exchange file is allowed.
- Non-weakening tests: task/`extra_args` cannot remove the deny.
- Full security/provider suites and project gates.

## Out of scope

- Codex enforcement (WRI-003 hygiene now, WRI-006 real enforcement).
- Closing the `Bash` read hole (Phase 2 topology WRI-005 + sandbox WRI-006).

## Likely implementation areas

- src/wastech_orchestrator/providers/claude.py
- src/wastech_orchestrator/security
- src/wastech_orchestrator/config/schema.py and validation.py
- tests/providers/test_claude_command.py, tests/providers/test_claude_run.py
- .agents/rules/security.md, docs/configuration.md, src/wastech_orchestrator/packaged/guide
