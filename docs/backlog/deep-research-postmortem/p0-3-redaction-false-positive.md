# P0.3 — the secret redactor corrupts benign identifiers, breaks the audit log, and pollutes the handoff channel

Priority: **P0** Status: **implemented** (2026-07-26) Date: 2026-07-25 Source: [postmortem.md](postmortem.md) DR-10

## Implemented

Both changes as specified. `_ASSIGNMENT` keeps its cheap prefilter but the substitution decides by `is_sensitive_key`, so one policy serves text, mappings and env vars. Harm 2 is fixed by the option this document asked for: a new `redact_jsonl` decodes each sink line, walks it with the existing `_redact_node`, and re-serializes — an escape cannot be half-consumed because it is gone before any pattern applies. Line endings and key order are preserved (CRLF survives on Windows; no `sort_keys`, so the sink stays diffable).

Deliberate narrowing to record: plurals and glued compounds (`TOKENS=`, `api_keys:`, `MYTOKEN=`) also stop matching, since the segment set contains `token`, not `tokens`. That is the stated policy and already how env-var harvesting behaves, and the token-shape patterns plus the harvested-literal path still catch a credential-shaped value under any name.

Two adjacent gaps found while pinning this, both fixed in the same change and both in the **widening** direction:

- **`"access_token": "…"` was never redacted at all.** The name group cannot cross a quote, so a JSON key's closing quote ended the match — the `"NAME": "VALUE"` form this module's own comment claims to handle was unprotected. The acceptance criterion above lists it as a must-still-redact case; it was in fact a must-start-redacting case.
- **`_scrub_raw_session` used a bare `str.replace`** for the raw session id — the F45 defect on a path F45 did not cover. Harmless for a UUID, but a short id rewrites those characters inside other words and shreds the JSON of the very sinks it rewrites. Now word-bounded, like the literal path in `redact_text`.

## Problem

`_ASSIGNMENT` matches a **substring**, so the ordinary identifier `tokens` is treated as a secret-bearing name. This contradicts the documented policy in the same module, produces invalid JSON in `events.jsonl`, and — because the same function redacts node outputs — has already corrupted the inter-node handoff channel and leaked the corruption into a published PR body.

This is **not** F45 (short harvested literals rewriting ordinary words, fixed by adding word boundaries to the literal path). It is a separate defect on the assignment path.

## Evidence

[`providers/redaction.py:74-76`](../../../src/wastech_orchestrator/providers/redaction.py):

```python
_SENSITIVE_WORD = r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|AUTHORIZATION|CREDENTIALS?|PRIVATE[_-]?KEY)"
_ASSIGNMENT = re.compile(
    rf"(?i)([A-Za-z0-9_]*{_SENSITIVE_WORD}[A-Za-z0-9_]*)(\s*[:=]\s*\"?)([^\s\"]+)"
)
```

Reproduced directly against the real function:

```
'tokens: thresholdSchema.optional(),' -> 'tokens: [REDACTED]'
'input_tokens: 4447658,'              -> 'input_tokens: [REDACTED]'
'let apiKeyword = 1'                  -> 'let apiKeyword = [REDACTED]'
'secretName: foo'                     -> 'secretName: [REDACTED]'
```

The contradiction is internal: `redaction.py:78-80` states that matching is by whole segment "so `access_token` / `API_KEY` match while a usage counter like `input_tokens` does not", and `is_sensitive_key("tokens")` returns `False`. Two matchers, opposite policies, one module.

### Harm 1 — invalid JSON in the audit log

Redaction is applied to the already-serialized stream at [`providers/_adapter_base.py:483`](../../../src/wastech_orchestrator/providers/_adapter_base.py), and the value group `[^\s\"]+` eats the escape backslash of `\"`:

```
in : {"text":"  tokens: \"tokens\","}
out: {"text":"  tokens: [REDACTED]"tokens\","}
```

On `p9-09`, 2 of 14 `events.jsonl` files have an unparsable line (`repository_analysis` line 146, `critical_review` line 73), and the corrupted payload is the read of `size.ts` — the file behind the run's own SC-2 finding. Runtime behavior is unaffected (`_adapter_base.py:480` notes parsing uses the in-memory raw stream), but any `jq`-based tooling over `events.jsonl` silently loses those lines.

### Harm 2 — corrupted handoff, already observed in production

The same function redacts node output at [`core/flow/postprocess.py:155`](../../../src/wastech_orchestrator/core/flow/postprocess.py), and **that redacted copy is what the downstream `{<node_id>_path}` channel resolves** (WRI-001, `postprocess.py:158-168`). It did not fire on `p9-09`'s outputs, but it did on `p10-05-test-depth`, whose published PR body carries the evidence:

> "A transient artifact in the plan draft (a malformed `tokens: [REDACTED]` fragment in the SIZE-001 test sketch) was flagged as a risk to verify; a spot-check of the committed `rules-size.test.ts` confirmed it resolved to valid `tokens: { warn: … }` syntax."

A downstream node spent effort disproving a phantom defect, and the explanation shipped to GitHub. `p7-05-integration-tests-docs-3/stages/fixing/run-000200/fixing.out.md` carries the same corruption.

## Change

1. Align `_ASSIGNMENT`'s name matcher with the segment policy `is_sensitive_key` already implements, so `tokens`, `input_tokens`, `apiKeyword` and `secretName` stop matching while `access_token`, `API_KEY` and `GITHUB_TOKEN` still do. One policy, one helper, used by both paths.
2. Apply redaction to **decoded string values**, not to the serialized line, so an escape sequence can never be partially consumed. The mapping path (`redact_mapping` / `_redact_node`) already does this correctly; the stream path at `_adapter_base.py:483` does not.

## Acceptance

- `redact_text` leaves `tokens:`, `input_tokens:`, `apiKeyword`, `secretName` untouched, and still redacts `GITHUB_TOKEN=…`, `api_key: …`, `"access_token": "…"`.
- Every line of a written `events.jsonl` parses as JSON after redaction, for any input containing escaped quotes next to a sensitive-looking name.
- A node output containing `tokens: { warn: 5 }` survives the handoff byte-identical.
- No regression on F45's word-boundary literal behavior.

## Test

Property-style: for a corpus of source snippets containing sensitive and benign identifiers, `json.loads` succeeds on every redacted serialized line, and no benign identifier's value is replaced. Regression cases pinned from the two real corrupted lines in `p9-09` and the `p10-05` plan draft.

## Scope / risk

Orchestrator default. Risk is the direction that matters: narrowing a redactor must not un-redact a real secret. Keep the token-shape patterns (`_TOKEN_PATTERNS`) and the harvested-literal path untouched — this change only narrows the **name**-based assignment matcher, whose intended policy is already written down and stricter than what the regex implements.

## Depends on

Nothing. Independent of every other item, and it is the only one that is currently corrupting data rather than merely losing signal.
