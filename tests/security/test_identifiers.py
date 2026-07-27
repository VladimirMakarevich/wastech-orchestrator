"""Portable artifact path-identity validators.

These validators are pure and **host-independent** by construction — there is no platform seam to
inject, so the same string is accepted/rejected identically on Windows, macOS, and Linux. That is
the point: a task/flow that loads on one OS must be able to write its artifacts on every OS. The
filesystem-touching containment belt is covered in tests/providers/test_artifacts.py.
"""

from __future__ import annotations

import pytest

from wastech_orchestrator.core.prompts import referenced_variables
from wastech_orchestrator.security.identifiers import (
    is_portable_path_segment,
    is_valid_node_id,
    is_valid_task_id,
    is_windows_reserved_name,
)

# Every Windows reserved device stem. Each is reserved case-insensitively and even with an
# extension, so ``con``, ``CON``, ``con.txt`` all resolve to the device.
_DEVICE_STEMS = [
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{d}" for d in "123456789"),
    *(f"lpt{d}" for d in "123456789"),
]


# --- Windows reserved names ----------------------------------------------------------------------


@pytest.mark.parametrize("stem", _DEVICE_STEMS)
def test_device_names_rejected_case_insensitively_and_with_extension(stem: str) -> None:
    for name in (stem, stem.upper(), stem.capitalize(), f"{stem}.txt", f"{stem.upper()}.TAR.GZ"):
        assert is_windows_reserved_name(name), name
        assert not is_valid_node_id(name), name
        assert not is_valid_task_id(name), name


@pytest.mark.parametrize(
    "name",
    ["console", "com0", "com10", "lpt0", "comx", "connie", "aux1", "prnt", "nulled", "com"],
)
def test_near_device_names_are_not_reserved(name: str) -> None:
    # Only the exact stems are devices; ``com0``/``com10``/``console`` are ordinary names.
    assert not is_windows_reserved_name(name)


# --- task id -------------------------------------------------------------------------------------


@pytest.mark.parametrize("task_id", ["task-001", "a", "0", "task.1_2-3", "a" * 64, "con2", "com"])
def test_valid_task_ids(task_id: str) -> None:
    assert is_valid_task_id(task_id)


@pytest.mark.parametrize(
    "task_id",
    [
        "",  # empty
        "Task-001",  # uppercase
        "-task",  # leading separator
        ".task",  # leading dot
        "_task",  # leading underscore
        "task 001",  # whitespace
        "task/01",  # separator
        "task\\01",  # backslash
        "tÉst",  # non-ascii / normalization drift
        "a" * 65,  # too long
        "task.",  # trailing dot (Windows strips it → a different on-disk name)
        "..",  # traversal
        "con",  # device name
        "nul.txt",  # device stem + extension
        "com1",  # serial-port device
        "lpt9",  # printer device
    ],
)
def test_invalid_task_ids(task_id: str) -> None:
    assert not is_valid_task_id(task_id)


# --- node id -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "node_id",
    ["a", "planning", "test_fix", "static-scan", "pass2", "node-1", "a" * 64, "con2", "review"],
)
def test_valid_node_ids(node_id: str) -> None:
    assert is_valid_node_id(node_id)


@pytest.mark.parametrize(
    "node_id",
    [
        "",  # empty
        "A",  # uppercase
        "Planning",  # uppercase
        "-node",  # leading separator
        "_node",  # leading underscore
        "node.",  # trailing dot
        "a.b",  # dot is not a node-id char (breaks the {id_path} token)
        "a/b",  # separator
        "a\\b",  # backslash
        "..",  # traversal
        "node id",  # whitespace
        "a" * 65,  # too long
        "tÉst",  # non-ascii
        "con",  # device name
        "nul",  # device name
        "com1",  # device name
        "lpt3",  # device name
        "aux",  # device name
    ],
)
def test_invalid_node_ids(node_id: str) -> None:
    assert not is_valid_node_id(node_id)


@pytest.mark.parametrize(
    "node_id", ["a", "planning", "test_fix", "static-scan", "pass2", "node-1", "a" * 64]
)
def test_valid_node_id_is_a_substitutable_prompt_token(node_id: str) -> None:
    # Every accepted node id forms a token the renderer actually substitutes as
    # {<node-id>_path} — verified against the real renderer token grammar, not a re-derived pattern.
    token = f"{node_id}_path"
    assert referenced_variables("before {" + token + "} after") == {token}


# --- portable path segment (the exchange relpath grammar) ----------------------------------------


@pytest.mark.parametrize(
    "segment",
    [
        "plan.md",
        "current.diff",
        "findings.json",
        "history.jsonl",
        "run-000001",
        "1-claude",
        "sub-02",
        "SKILL.md",  # mixed case is fine for a fixed artifact name (case collisions are dir-level)
        "task.enriched.md",
        "con2",
        "a",
    ],
)
def test_valid_portable_segments(segment: str) -> None:
    assert is_portable_path_segment(segment)


@pytest.mark.parametrize(
    "segment",
    [
        "",  # empty
        ".",  # current dir
        "..",  # traversal
        "a/b",  # separator
        "a\\b",  # backslash
        "a:b",  # drive/stream separator
        "C:",  # drive-relative form
        "plan.",  # trailing dot
        "plan ",  # trailing space
        "na<me",  # Windows-forbidden char
        'q"x',  # Windows-forbidden char
        "a|b",  # Windows-forbidden char
        "a?b",  # Windows-forbidden char
        "a*b",  # Windows-forbidden char
        "con",  # device name
        "nul.txt",  # device stem + extension
        "COM1",  # device name
        "lpt9.log",  # device stem + extension
        "a\tb",  # control char
        "a\x00b",  # NUL
    ],
)
def test_invalid_portable_segments(segment: str) -> None:
    assert not is_portable_path_segment(segment)
