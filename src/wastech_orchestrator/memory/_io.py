"""Atomic, deterministic on-disk primitives for the memory store.

Mirrors the existing atomic-write discipline (``core.hitl._atomic_json``) rather than inventing a
new one: write to a temp sibling, then ``replace`` it into place so a crash never leaves a partially
written file. Two cross-cutting guarantees this module is responsible for:

* **Deterministic bytes.** Every write fixes ``newline="\\n"`` (no platform translation) and emits
  JSON with ``sort_keys=True``/``ensure_ascii=False``. The bytes on disk are therefore identical on
  Windows and POSIX for the same input — required so content hashes (audit) and snapshot/restore
  comparisons are byte-stable across platforms.
* **No interpretation.** These helpers do not redact, validate, or audit. Redaction happens in
  :class:`~wastech_orchestrator.memory.service.MemoryService` *before* a row reaches here, so the
  "no write bypasses redaction" invariant is enforced at the service boundary, not duplicated here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


def _dumps(payload: Mapping[str, Any], *, indent: int | None) -> str:
    return json.dumps(payload, indent=indent, ensure_ascii=False, sort_keys=True)


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` atomically (temp-file-then-rename), creating parent dirs as needed.

    ``newline="\\n"`` disables platform newline translation so the on-disk bytes are exactly the
    UTF-8 encoding of ``text`` on every OS (deterministic hashing / byte-identical restore).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    temp.replace(path)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically write ``payload`` as pretty-printed, key-sorted JSON (one trailing newline)."""
    atomic_write_text(path, _dumps(payload, indent=2) + "\n")


def append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    """Append ``row`` as one compact JSON line (the append-only record/audit primitive).

    The whole line is written in a single ``write`` call so a crash cannot tear it mid-object — the
    discipline the audit hash chain later backstops. Append mode keeps the file genuinely
    append-only (no in-place rewrite), which the audit log requires.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = _dumps(row, indent=None) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)


def atomic_write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    """Atomically (re)write a whole JSONL file to exactly ``rows`` — the record *update* primitive.

    Temp-file-then-rename, so a crash never leaves a half-rewritten file. Callers pass the complete
    desired contents (read-all → edit → write-all-back); this is **not** append. Never use it on the
    append-only audit log.
    """
    text = "".join(_dumps(row, indent=None) + "\n" for row in rows)
    atomic_write_text(path, text)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file into a list of dict rows (``[]`` if absent; blank lines skipped)."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line:
            rows.append(json.loads(line))
    return rows
