"""Repo-relative glob matching shared across the orchestrator.

A single dependency-free implementation of the glob dialect used wherever an operator lists
repo-relative path patterns (``checks.command_sets[].paths``, the dangerous-diff deletion
allowlist). Anchored ``fullmatch`` against the whole path; ``**`` crosses path separators while a
single ``*``/``?`` stay within one segment. Pure and deterministic — no I/O, no clock.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache


@lru_cache(maxsize=256)
def compile_glob(pattern: str) -> re.Pattern[str]:
    """Translate a repo-relative glob into an anchored regex.

    ``**`` crosses path separators (``**/`` = zero or more leading directories, so ``**/*.md``
    matches both ``README.md`` and ``docs/a/b.md``); a single ``*`` and ``?`` stay within one
    segment. Dependency-free, so it works on the ``requires-python`` floor (3.12, before
    ``PurePath.full_match``).
    """
    norm = pattern.replace("\\", "/")
    out: list[str] = []
    i, n = 0, len(norm)
    while i < n:
        c = norm[i]
        if c == "*":
            if i + 1 < n and norm[i + 1] == "*":
                if i + 2 < n and norm[i + 2] == "/":
                    out.append("(?:.*/)?")  # **/  → zero or more leading directories
                    i += 3
                else:
                    out.append(".*")  # ** → anything, including separators
                    i += 2
                continue
            out.append("[^/]*")  # * → within one path segment
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
        elif c == "/":
            out.append("/")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out))


def path_matches_any(path: str, patterns: Iterable[str]) -> bool:
    """True iff ``path`` (repo-relative) fully matches at least one glob in ``patterns``."""
    norm = path.replace("\\", "/")
    return any(compile_glob(pattern).fullmatch(norm) is not None for pattern in patterns)
