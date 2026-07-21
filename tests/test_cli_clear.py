"""`worc clear`: emits the screen-wipe ANSI to stdout and deletes nothing."""

from __future__ import annotations

import pytest

from wastech_orchestrator import cli


def test_clear_writes_ansi_screen_wipe(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["clear"]) == 0
    assert capsys.readouterr().out == cli._CLEAR_SCREEN
