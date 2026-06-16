"""Secret redaction (spec §12.6, .agents/rules/security.md).

Pure functions that scrub known-secret shapes from text and from the request representation
**before** anything is written to an artifact, log, or SQLite. There are two inputs:

* ``extra_secrets`` — exact secret values the caller already knows (e.g. the values of
  non-allowlisted, secret-named environment variables); these are matched literally.
* token-shaped patterns (GitHub/OpenAI/Slack/AWS keys, Bearer tokens, JWTs) and sensitive
  ``NAME=VALUE`` assignments — matched structurally.

The functions never mutate their inputs. :func:`read_denied_secrets` harvests the values of the
``security.denied_read_paths`` files (``.env``, ``secrets/**``) present in the agent's workspace so
that, even if the agent leaks their content to stdout/stderr, those values are redacted before any
artifact/log is written (§12.4/§12.6). The guarantee these support: **no secret ever lands in an
artifact**.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"

# Literal secrets shorter than this are ignored: redacting a 1-3 char value would mangle ordinary
# text without protecting anything meaningful (real tokens are long).
_MIN_LITERAL_LEN = 4

# Threshold for a token harvested from a denied_read_paths file. Higher than _MIN_LITERAL_LEN so
# scanning a ``.env`` does not turn common short values (``true``, ``1234``) into redaction literals
# that would mangle unrelated output. Mirrors the >= 8 heuristic the adapters use for env secrets.
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
# redact the value.
_SENSITIVE_WORD = (
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|AUTHORIZATION|CREDENTIALS?"
    r"|PRIVATE[_-]?KEY)"
)
_ASSIGNMENT = re.compile(
    rf"(?i)([A-Za-z0-9_]*{_SENSITIVE_WORD}[A-Za-z0-9_]*)(\s*[:=]\s*\"?)([^\s\"]+)",
)

# Whole-segment names whose value is redacted in a structured mapping / env var. Matching is by
# segment (the name is split on non-alphanumerics) so ``access_token`` / ``API_KEY`` match while a
# usage counter like ``input_tokens`` does not (its segment is ``tokens``, not ``token``).
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


def redact_text(text: str, *, extra_secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with known secrets replaced by :data:`REDACTED`. Pure."""
    redacted = text
    literals = sorted(
        {s for s in extra_secrets if len(s) >= _MIN_LITERAL_LEN}, key=len, reverse=True
    )
    for secret in literals:
        redacted = redacted.replace(secret, REDACTED)
    redacted = _ASSIGNMENT.sub(rf"\1\2{REDACTED}", redacted)
    for pattern in _TOKEN_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


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
    """Harvest secret values from the ``denied_read_paths`` files under ``workspace`` (§12.4).

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
