"""Secret redaction (.agents/rules/security.md).

Pure functions that scrub known-secret shapes from text and from the request representation
**before** anything is written to an artifact, log, or SQLite. There are two inputs:

* ``extra_secrets`` — exact secret values the caller already knows (e.g. the values of
  non-allowlisted, secret-named environment variables); these are matched literally.
* token-shaped patterns (GitHub/OpenAI/Slack/AWS keys, Bearer tokens, JWTs) and sensitive
  ``NAME=VALUE`` assignments — matched structurally. Whether a *name* is sensitive is decided in
  exactly one place, :func:`is_sensitive_key`, for text, mappings and env vars alike.

Pick the entry point by what the payload is: :func:`redact_text` for prose and source text,
:func:`redact_mapping` for a structured request, and :func:`redact_jsonl` for a stream of JSON lines
(a provider event log), which redacts decoded values so a redacted line never stops parsing.

The functions never mutate their inputs. :func:`read_denied_secrets` harvests the values of the
``security.denied_read_paths`` files (``.env``, ``secrets/**``) present in the agent's workspace so
that, even if the agent leaks their content to stdout/stderr, those values are redacted before any
artifact/log is written. The guarantee these support: **no secret ever lands in an
artifact**.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"


def normalized_session_id(raw_session_id: str) -> str:
    """A stable, non-secret outward form of a provider session id (durable sessions, P2.2).

    The raw session id lives **only** in ``state.db`` (the ``editing_lineage`` table); everywhere
    else (artifacts, logs, the ``result.json`` audit) the session is referred to by this normalized
    token — a short SHA-256 prefix — so runs can be correlated without exposing the resumable id.
    """
    digest = hashlib.sha256(raw_session_id.encode("utf-8")).hexdigest()[:12]
    return f"session:{digest}"


# Literal secrets shorter than this are ignored: redacting a very short value would mangle ordinary
# text without protecting anything meaningful (real tokens are long). Aligned with
# ``_MIN_DENIED_SECRET_LEN`` below — every harvest source already floors literals at 8, so this
# loses no real secret while adding defense-in-depth against a short literal slipping in (F45).
_MIN_LITERAL_LEN = 8

# Threshold for a token harvested from a denied_read_paths file. Higher than _MIN_LITERAL_LEN so
# scanning a ``.env`` does not turn common short values (``true``, ``1234``) into redaction literals
# that would mangle unrelated output. The adapter base imports this constant for the same heuristic
# when harvesting secret-named env values, so the threshold has a single source of truth.
_MIN_DENIED_SECRET_LEN = 8

# A run of non-separator characters on a line — used to harvest individual secret tokens (e.g. the
# value inside ``"api_key": "SECRET"``) so a leaked bare value is matched, not just the whole line.
_DENIED_TOKEN_RE = re.compile(r"[^\s\"'=:,;]+")

# Token-shaped secrets. Conservative — each pattern targets a recognizable credential format.
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"gh[opsur]_[A-Za-z0-9]{16,}"),  # GitHub PAT / OAuth / server / refresh / user
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),  # GitHub fine-grained PAT
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),  # OpenAI-style secret key (incl. sk-proj-)
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack token
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*"),  # Bearer <token>
    re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"),  # JWT
)

# Sensitive ``NAME=VALUE`` / ``NAME: VALUE`` / ``"NAME": "VALUE"`` assignments — keep the name,
# redact the value. This regex is only a cheap PREFILTER: it finds assignment-shaped text whose name
# contains a sensitive word anywhere, and :func:`_redact_assignment` then decides by the one name
# policy (:func:`is_sensitive_key`). Trusting the substring match instead was a defect: it rewrote
# the ordinary identifier ``tokens`` (``[A-Za-z0-9_]*TOKEN[A-Za-z0-9_]*`` matches it), which
# corrupted benign source text in artifacts, logs and the inter-node handoff channel.
_SENSITIVE_WORD = (
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|AUTHORIZATION|CREDENTIALS?"
    r"|PRIVATE[_-]?KEY)"
)
#
# The optional quote BEFORE the separator is what makes the ``"NAME": "VALUE"`` form in the comment
# above real: the name group cannot cross a quote, so a JSON key's closing quote used to end the
# match and ``"access_token": "…"`` was never redacted at all.
_ASSIGNMENT = re.compile(
    rf"(?i)([A-Za-z0-9_]*{_SENSITIVE_WORD}[A-Za-z0-9_]*)(\"?\s*[:=]\s*\"?)([^\s\"]+)",
)

# Whole-segment names whose value is redacted. THE single name policy — used by the assignment
# matcher above, by structured mappings, and by env-var harvesting, so all three agree. Matching is
# by segment (the name is split on non-alphanumerics) so ``access_token`` / ``API_KEY`` match while
# a usage counter like ``input_tokens`` does not (its segment is ``tokens``, not ``token``).
_SENSITIVE_SEGMENTS: frozenset[str] = frozenset(
    {
        "token",
        "secret",
        "password",
        "passwd",
        "pwd",
        "passphrase",
        "authorization",
        "credential",
        "credentials",
        "key",
        "apikey",
        "accesskey",
        "privatekey",
        "secretkey",
    }
)
_SEGMENT_SPLIT = re.compile(r"[^a-z0-9]+")


def is_sensitive_key(name: str) -> bool:
    """Return whether ``name`` looks like a secret-bearing key/env-var name (e.g. ``API_KEY``).

    Matches on whole segments, so ``GITHUB_TOKEN`` / ``api_key`` are sensitive but a usage counter
    like ``input_tokens`` (segment ``tokens``) is not.
    """
    return any(seg in _SENSITIVE_SEGMENTS for seg in _SEGMENT_SPLIT.split(name.lower()) if seg)


def secret_env_values(allowed_environment: Iterable[str]) -> tuple[str, ...]:
    """Values of non-allowlisted, secret-named parent env vars, as defensive redaction literals.

    The single source of truth for env-secret harvesting: a value is collected only when its env-var
    name looks secret-bearing (:func:`is_sensitive_key`), the name is **not** on the process
    allowlist (allowlisted vars are deliberately exported, not secrets to scrub), and the value is
    at least :data:`_MIN_DENIED_SECRET_LEN` chars (so short values like ``true`` are never turned
    into a redaction literal that would mangle unrelated output). Used by the provider adapters and
    the memory write path to scrub a known secret value that matches no structural token shape (C1).
    """
    allowed = set(allowed_environment)
    return tuple(
        value
        for key, value in os.environ.items()
        if key not in allowed and len(value) >= _MIN_DENIED_SECRET_LEN and is_sensitive_key(key)
    )


def _redact_assignment(match: re.Match[str]) -> str:
    """Redact one prefiltered assignment's value, but only when its NAME is sensitive by policy.

    The prefilter regex matches a sensitive word anywhere inside the name; :func:`is_sensitive_key`
    is the authority, so ``access_token`` / ``API_KEY`` / ``GITHUB_TOKEN`` are redacted while
    ``tokens``, ``input_tokens``, ``apiKeyword`` and ``secretName`` are left alone.
    """
    name, separator = match.group(1), match.group(2)
    if not is_sensitive_key(name):
        return match.group(0)
    return f"{name}{separator}{REDACTED}"


def redact_text(text: str, *, extra_secrets: Iterable[str] = ()) -> str:
    r"""Return ``text`` with known secrets replaced by :data:`REDACTED`. Pure.

    Literal ``extra_secrets`` are replaced only on word boundaries (``(?<!\w)…(?!\w)``), never as a
    substring inside a larger token. An unbounded substring replace corrupted benign text — F45: a
    short harvested value rewrote the middle of an ordinary word in a lesson ``subject``, which also
    broke the subject-derived dedup key. Deterministic (F36): the same input always redacts alike.

    A sensitive ``NAME=VALUE`` assignment keeps its name and loses its value, gated on the whole-
    segment name policy (:func:`is_sensitive_key`) so a benign identifier is never rewritten. When
    the text is a stream of JSON lines, prefer :func:`redact_jsonl`: this function works on raw
    characters and would consume the backslash of an escaped ``\"`` inside a JSON string.
    """
    redacted = text
    literals = sorted(
        {s for s in extra_secrets if len(s) >= _MIN_LITERAL_LEN}, key=len, reverse=True
    )
    for secret in literals:
        redacted = re.sub(rf"(?<!\w){re.escape(secret)}(?!\w)", REDACTED, redacted)
    redacted = _ASSIGNMENT.sub(_redact_assignment, redacted)
    for pattern in _TOKEN_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def redact_jsonl(text: str, *, extra_secrets: Iterable[str] = ()) -> str:
    r"""Redact a JSON-lines stream via DECODED values, so every output line stays valid JSON.

    :func:`redact_text` operates on raw characters, so a sensitive assignment inside a JSON string
    (``{"text":"  password: \"x\","}``) has its value group eat the backslash of the escaped quote
    and the line stops parsing — 2 of 14 ``events.jsonl`` files in one real run had an unrecoverable
    line, and the payload lost was a tool result the run's own findings rested on. Decoding first
    makes that structurally impossible: the escape is gone by the time any pattern is applied, and
    :func:`json.dumps` re-escapes whatever survives.

    Each line is decoded and walked with the same recursion as :func:`redact_mapping` (sensitive
    keys lose their whole value; string leaves go through :func:`redact_text`). A line that is not
    JSON — a provider preamble, a truncated tail — falls back to :func:`redact_text`, so nothing is
    written unscrubbed. Line endings are preserved verbatim, so a CRLF stream survives on Windows,
    and key order is preserved (no ``sort_keys``) so the sink stays diffable and deterministic
    (F36).
    """
    secrets = tuple(extra_secrets)
    out: list[str] = []
    for raw in text.splitlines(keepends=True):
        payload = raw.rstrip("\r\n")
        ending = raw[len(payload) :]
        if not payload.strip():
            out.append(raw)
            continue
        try:
            decoded = json.loads(payload)
        except ValueError:
            out.append(redact_text(payload, extra_secrets=secrets) + ending)
            continue
        out.append(json.dumps(_redact_node(decoded, secrets), ensure_ascii=False) + ending)
    return "".join(out)


def redact_mapping(obj: Mapping[str, Any], *, extra_secrets: Iterable[str] = ()) -> dict[str, Any]:
    """Return a deep copy of ``obj`` with secrets scrubbed. Pure (input is not mutated).

    String values are passed through :func:`redact_text`; a value under a sensitive key name (e.g.
    ``api_key``) is fully redacted regardless of shape; lists/dicts are walked recursively.
    """
    secrets = tuple(extra_secrets)
    return {key: _redact_value(key, value, secrets) for key, value in obj.items()}


def _redact_value(key: str, value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(key, str) and is_sensitive_key(key):
        return REDACTED
    return _redact_node(value, secrets)


def _redact_node(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return redact_text(value, extra_secrets=secrets)
    if isinstance(value, Mapping):
        return {k: _redact_value(k, v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_node(item, secrets) for item in value]
    return value


def read_denied_secrets(
    workspace: str | Path,
    denied_read_paths: Iterable[str],
    *,
    max_bytes: int = 65536,
) -> tuple[str, ...]:
    """Harvest secret values from the ``denied_read_paths`` files under ``workspace``.

    Each pattern is globbed relative to the workspace (``.env`` matches a file, ``secrets/**`` a
    whole subtree); matched files are read up to ``max_bytes`` and their non-trivial value tokens
    returned as literal secrets for :func:`redact_text` / :func:`redact_mapping`. Missing paths are
    silently skipped. Pure with respect to the filesystem (read-only, no mutation); the returned
    values are only ever used as redaction literals and are never themselves written anywhere.
    """
    root = Path(workspace)
    files: set[Path] = set()
    for pattern in denied_read_paths:
        cleaned = pattern.strip()
        if not cleaned:
            continue
        try:
            matches = list(root.glob(cleaned))
        except (OSError, ValueError):
            continue
        for match in matches:
            if match.is_file():
                files.add(match)
            elif match.is_dir():
                files.update(p for p in match.rglob("*") if p.is_file())

    secrets: list[str] = []
    seen: set[str] = set()
    for path in sorted(files):
        try:
            data = path.read_bytes()[:max_bytes]
        except OSError:
            continue
        for token in _extract_secret_tokens(data.decode("utf-8", errors="replace")):
            if token not in seen:
                seen.add(token)
                secrets.append(token)
    return tuple(secrets)


def _extract_secret_tokens(text: str) -> list[str]:
    """Pull candidate secret strings out of a denied file's text (env values, quoted/JSON values).

    Yields, per non-comment line: the value after the first ``=`` (env style), every contiguous
    non-separator run (catches the bare value inside ``"key": "value"``), and the whole stripped
    line (opaque single-token secret files) — each filtered to ``_MIN_DENIED_SECRET_LEN`` so common
    short values are not turned into redaction literals.
    """
    tokens: list[str] = []

    def keep(candidate: str) -> None:
        cleaned = candidate.strip().strip("\"'")
        if len(cleaned) >= _MIN_DENIED_SECRET_LEN:
            tokens.append(cleaned)

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            keep(line.split("=", 1)[1])
        for run in _DENIED_TOKEN_RE.findall(line):
            keep(run)
        keep(line)
    return tokens
