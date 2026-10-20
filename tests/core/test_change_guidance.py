"""The where-to-start sentence a node is handed about the task's change (``change_guidance``)."""

from __future__ import annotations

import re

import pytest

from wastech_orchestrator.core.flow.change_guidance import change_guidance
from wastech_orchestrator.diff_overview import LAST_COMMIT_TITLE

_SHA = "3f9c2a7e1d4b5a6c8e0f1a2b3c4d5e6f7a8b9c0d"


def test_a_node_with_a_shell_starts_from_the_overview() -> None:
    sentence = change_guidance(base_sha=_SHA, has_shell=True, decomposed=False)
    assert sentence.startswith("Start from the change overview")
    assert f"`git diff {_SHA} -- <path>`" in sentence
    # An untracked added file is not in `git diff` at all, so it is sent to be read instead.
    assert "read an added (A) file directly" in sentence
    assert "quote a path that contains spaces" in sentence
    assert "Do not inspect a path marked [denied]." in sentence


def test_a_node_without_a_shell_reads_the_full_diff() -> None:
    sentence = change_guidance(base_sha=_SHA, has_shell=False, decomposed=False)
    assert sentence.startswith("Read the full diff")
    assert "change overview" in sentence
    # Never told to run anything it may not be able to run.
    assert "git" not in sentence


@pytest.mark.parametrize("has_shell", [True, False])
def test_a_subtask_is_told_which_section_is_its_own(has_shell: bool) -> None:
    plain = change_guidance(base_sha=_SHA, has_shell=has_shell, decomposed=False)
    decomposed = change_guidance(base_sha=_SHA, has_shell=has_shell, decomposed=True)
    assert decomposed.startswith(plain)
    assert f'"{LAST_COMMIT_TITLE}"' in decomposed and "this subtask's own change" in decomposed


def test_the_guidance_names_only_git_commands() -> None:
    # The core owns git through the Git Manager; it names no provider tool or flag.
    for has_shell in (True, False):
        for decomposed in (True, False):
            sentence = change_guidance(base_sha=_SHA, has_shell=has_shell, decomposed=decomposed)
            commands = re.findall(r"`([^`]+)`", sentence)
            assert all(command.startswith("git ") for command in commands)
            # No provider tool name and no CLI flag outside the git command itself.
            outside = re.sub(r"`[^`]+`", "", sentence)
            assert not re.search(r"\b(Bash|Grep|Glob|apply_patch)\b|Read\(|--\w", outside)
