# B21 — Secret Redaction

> Reconstructed from code (`src/wastech_orchestrator/providers/redaction.py`) and tests (`tests/providers/test_redaction.py`, `tests/providers/test_redaction_sinks.py`, `tests/security/test_denied_reads.py`). The code is the only source of truth; this document was rebuilt from the implementation, not from prose or comments. Significant claims carry a `file:line` reference.

**Status:** documented · **Source modules:** `src/wastech_orchestrator/providers/redaction.py`

## Responsibility

A leaf module of pure functions that scrub known-secret values out of text and out of structured request/result representations **before** the caller writes them to an artifact, a log, or SQLite. It owns the redaction mechanism only; it never decides which literals a given process should redact and never performs any write itself — those are the callers' jobs ([redaction.py:1](../../../src/wastech_orchestrator/providers/redaction.py#L1)). It also mints the non-secret outward form of a provider session id ([redaction.py:29](../../../src/wastech_orchestrator/providers/redaction.py#L29)).

There are two distinct redaction inputs: **literal secrets** the caller already knows (exact-value match, ≥ a minimum length), and **token-shaped patterns** plus sensitive `NAME=VALUE` assignments (structural match) ([redaction.py:106](../../../src/wastech_orchestrator/providers/redaction.py#L106)).

## Public surface

- `REDACTED` ([redaction.py:26](../../../src/wastech_orchestrator/providers/redaction.py#L26)) — the placeholder string `[REDACTED]` substituted for every secret.
- `redact_text(text, *, extra_secrets=())` ([redaction.py:106](../../../src/wastech_orchestrator/providers/redaction.py#L106)) — returns `text` with literals, sensitive assignments, and token patterns masked. Pure.
- `redact_mapping(obj, *, extra_secrets=())` ([redaction.py:120](../../../src/wastech_orchestrator/providers/redaction.py#L120)) — returns a deep copy of a mapping with secrets scrubbed; recurses into nested dicts/lists/tuples. Pure (input not mutated).
- `read_denied_secrets(workspace, denied_read_paths, *, max_bytes=65536)` ([redaction.py:146](../../../src/wastech_orchestrator/providers/redaction.py#L146)) — read-only harvest of secret tokens from the workspace's denied-read files, returned as a deduplicated tuple of literals to feed back into `redact_text` / `redact_mapping`.
- `is_sensitive_key(name)` ([redaction.py:97](../../../src/wastech_orchestrator/providers/redaction.py#L97)) — segment-based test for a secret-bearing key/env-var name.
- `normalized_session_id(raw_session_id)` ([redaction.py:29](../../../src/wastech_orchestrator/providers/redaction.py#L29)) — `session:<sha256-prefix>`, the non-secret form of a resumable session id.

## Behavior

### `redact_text` — three ordered passes

`redact_text` applies, in order ([redaction.py:108](../../../src/wastech_orchestrator/providers/redaction.py#L108)):

1. **Literal substitution.** Each value in `extra_secrets` of length ≥ `_MIN_LITERAL_LEN` (4) is `str.replace`d with `REDACTED`. Literals are de-duplicated and sorted **longest-first** so a longer secret that contains a shorter one is masked before the substring would be ([redaction.py:109](../../../src/wastech_orchestrator/providers/redaction.py#L109)). The length floor exists because a 1–3 char "secret" would mangle ordinary text without protecting anything ([redaction.py:39](../../../src/wastech_orchestrator/providers/redaction.py#L39)).
2. **Sensitive assignments.** The `_ASSIGNMENT` regex matches `NAME=VALUE` / `NAME: VALUE` / `"NAME": "VALUE"` where the name contains a sensitive word (`TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|AUTHORIZATION|CREDENTIALS?|PRIVATE_KEY`, case-insensitive). The **name is kept and the value redacted** ([redaction.py:65](../../../src/wastech_orchestrator/providers/redaction.py#L65)).
3. **Token shapes.** Each `_TOKEN_PATTERNS` regex is substituted: GitHub PAT/OAuth/server/refresh/user (`gh[opsur]_…`), GitHub fine-grained PAT (`github_pat_…`), OpenAI-style key (`sk-…`, incl. `sk-proj-`), Slack (`xox[baprs]-…`), AWS access-key id (`AKIA…`), `Bearer <token>`, and JWT (`eyJ…`.`…`.`…`) ([redaction.py:53](../../../src/wastech_orchestrator/providers/redaction.py#L53)).

The function never mutates its input — it rebinds a local copy and returns it ([redaction.py:108](../../../src/wastech_orchestrator/providers/redaction.py#L108)). Ordinary prose with no secret shape passes through byte-identical (`test_ordinary_text_passes_through_unchanged`, [test_redaction.py:41](../../../tests/providers/test_redaction.py#L41)).

### `redact_mapping` / `is_sensitive_key` — structured redaction

`redact_mapping` walks the mapping recursively ([redaction.py:120](../../../src/wastech_orchestrator/providers/redaction.py#L120)). For each key/value: if the key is sensitive, the value is replaced wholesale with `REDACTED` regardless of shape ([redaction.py:130](../../../src/wastech_orchestrator/providers/redaction.py#L130)); otherwise a string is sent through `redact_text`, a nested mapping/list/tuple is recursed, and any other scalar (int/bool/None/float) is returned unchanged ([redaction.py:136](../../../src/wastech_orchestrator/providers/redaction.py#L136)). Non-string scalars survive (`test_non_string_scalars_are_preserved`, [test_redaction.py:77](../../../tests/providers/test_redaction.py#L77)).

`is_sensitive_key` lower-cases the name, splits it on non-alphanumerics (`_SEGMENT_SPLIT`), and matches **whole segments** against `_SENSITIVE_SEGMENTS` (`token`, `secret`, `password`, `key`, `credential`, `apikey`, `accesskey`, `privatekey`, …) ([redaction.py:97](../../../src/wastech_orchestrator/providers/redaction.py#L97)). Segment matching is deliberate: `access_token` / `API_KEY` are sensitive, but a usage counter like `input_tokens` (segment `tokens`, not `token`) is not ([redaction.py:74](../../../src/wastech_orchestrator/providers/redaction.py#L74), `test_usage_counter_keys_are_not_redacted` [test_redaction.py:82](../../../tests/providers/test_redaction.py#L82)).

### `read_denied_secrets` — harvesting leaked file contents

So a leaked secret is matched even when the caller does not know its value, this harvests the contents of the `security.denied_read_paths` files (`.env`, `secrets/**`) present in the agent's workspace ([redaction.py:146](../../../src/wastech_orchestrator/providers/redaction.py#L146)):

1. Each pattern is `Path.glob`'d relative to the workspace; a matched file is added directly, a matched directory is walked with `rglob("*")`. Glob errors (`OSError`, `ValueError`) are swallowed and the pattern skipped ([redaction.py:162](../../../src/wastech_orchestrator/providers/redaction.py#L162)).
2. Each file is read **bounded** to `max_bytes` (default 64 KiB); read errors are swallowed ([redaction.py:178](../../../src/wastech_orchestrator/providers/redaction.py#L178)). The cap really bounds the read (`test_size_cap_bounds_the_read`, [test_denied_reads.py:38](../../../tests/security/test_denied_reads.py#L38)).
3. `_extract_secret_tokens` pulls candidates per non-blank, non-`#` line: the value after the first `=`, every contiguous non-separator run (`_DENIED_TOKEN_RE`, catching the bare value inside `"key": "value"`), and the whole stripped line — each kept only if, after stripping quotes, its length ≥ `_MIN_DENIED_SECRET_LEN` (8) ([redaction.py:190](../../../src/wastech_orchestrator/providers/redaction.py#L190)).
4. A de-duplicated tuple is returned ([redaction.py:176](../../../src/wastech_orchestrator/providers/redaction.py#L176)). The returned values are only ever passed back as redaction literals and are never themselves written anywhere.

A harvested value is dropped if short (`true` is not collected — `test_reads_env_values_excluding_short_ones`, [test_denied_reads.py:16](../../../tests/security/test_denied_reads.py#L16)), pulled out of a JSON line, and from a recursed `secrets/**` subtree (`test_reads_secrets_dir_recursively`, [test_denied_reads.py:23](../../../tests/security/test_denied_reads.py#L23)). The full loop — harvest a `.env` value, feed it to `redact_text`, confirm it is scrubbed from leaked stdout — is `test_harvested_secret_feeds_redaction` ([test_denied_reads.py:45](../../../tests/security/test_denied_reads.py#L45)).

### How callers wire it (redact-before-write)

The adapters assemble the `extra_secrets` literal set as secret-named parent-env values **+** `read_denied_secrets(...)` **+** the raw resume session id ([codex.py:585](../../../src/wastech_orchestrator/providers/codex.py#L585), [claude.py](../../../src/wastech_orchestrator/providers/claude.py#L674)). The env-value half is filtered to `len(value) >= 8 and is_sensitive_key(key)` ([codex.py:602](../../../src/wastech_orchestrator/providers/codex.py#L602)). Every adapter sink — `stdout.log`, `stderr.log`, `events.jsonl` final message, `usage`, and the `request.json` representation — is passed through `redact_text` / `redact_mapping` before the artifact writer (B20) touches disk ([codex.py:433](../../../src/wastech_orchestrator/providers/codex.py#L433)), and the result's session id is swapped for the normalized form via `normalized_session_id` before `result.json` is written ([codex.py:613](../../../src/wastech_orchestrator/providers/codex.py#L613)). `test_redaction_sinks.py` seeds both a token-shaped secret and a denied-file-only secret and asserts neither lands in any sink ([test_redaction_sinks.py:47](../../../tests/providers/test_redaction_sinks.py#L47)).

Other callers: B27's `RedactionFilter` runs every log record's message/args/fields through `redact_text` ([logging.py:109](../../../src/wastech_orchestrator/observability/logging.py#L109)); B27's flow observability redacts the rendered prompt before persisting it ([observability.py:118](../../../src/wastech_orchestrator/core/flow/observability.py#L118)); B22 redacts git stderr and diffs ([git_manager.py:222](../../../src/wastech_orchestrator/git_manager.py#L222)); B26 redacts outbound Telegram text ([telegram.py:291](../../../src/wastech_orchestrator/notify/telegram.py#L291)); B06 redacts rendered prompt/skill text ([orchestrator.py:1456](../../../src/wastech_orchestrator/core/orchestrator.py#L1456)).

## Invariants & guarantees

- **Pure / non-mutating.** None of the functions mutate inputs; `redact_mapping` returns a deep copy (`test_redact_mapping_does_not_mutate_input`, [test_redaction.py:70](../../../tests/providers/test_redaction.py#L70)).
- **Redaction runs before any write.** Content reaches B20/B22/B27/B26 already redacted; this module imports nothing that writes, and the artifact writer imports neither this module nor any provider syntax ([artifacts.py:7](../../../src/wastech_orchestrator/providers/artifacts.py#L7)). The "no secret ever lands on disk" guarantee is the composition of _caller redacts_ + _writer is dumb_.
- **Session id never leaves `state.db`.** The raw resume id is in `extra_secrets` (so it is scrubbed from argv/stdout/stderr/events/result) and the artifact carries only `normalized_session_id` ([redaction.py:29](../../../src/wastech_orchestrator/providers/redaction.py#L29), [codex.py:613](../../../src/wastech_orchestrator/providers/codex.py#L613)).
- **Read-only filesystem access.** `read_denied_secrets` only reads, bounded by `max_bytes`, and silently skips missing/unreadable paths ([redaction.py:160](../../../src/wastech_orchestrator/providers/redaction.py#L160)).

## Dependencies

- **Uses:** standard library only (`hashlib`, `re`, `pathlib`) — no internal block.
- **Used by:** B18 (Agent Providers — redact every sink + harvest denied secrets), B20 (Run Artifact Layout — receives already-redacted content), B22 (Git Manager), B26 (Telegram), B27 (Observability — `RedactionFilter`, prompt persistence), B06 (Orchestrator Pipeline — prompt/skill text). See B25 (Security Policy) for the env allowlist and denied-read-path source.

## Tests

- `tests/providers/test_redaction.py` — token-shape masking, literal `extra_secrets`, the short-literal guard, name-kept/value-redacted assignments, deep recursion, non-mutation, scalar pass-through, and segment-based key sensitivity (incl. `input_tokens` negative).
- `tests/security/test_denied_reads.py` — `read_denied_secrets`: env-value harvest with the short-value exclusion, recursive `secrets/**`, missing-path skip, `max_bytes` bound, and the harvest→`redact_text` round-trip.
- `tests/providers/test_redaction_sinks.py` — end-to-end: a token-shaped secret and a denied-file-only secret seeded into the workspace are scrubbed from every Claude/Codex sink (`stdout.log`, `events.jsonl`, `stderr.log`, `request.json`).
