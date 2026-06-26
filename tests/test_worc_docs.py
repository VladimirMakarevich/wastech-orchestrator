"""Packaged installable guide docs: availability and shipped task validity.

The guide ships as package data under ``wastech_orchestrator/packaged/guide/`` (the single
aggregated home for everything shipped/seeded). The shipped sample tasks must pass the validation
gate.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from wastech_orchestrator.cli import _worc_root
from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.task.parser import ParsedSource
from wastech_orchestrator.task.validation_gate import ValidationGate


def test_worc_packaged_data_discoverable() -> None:
    # Packaged via importlib.resources, so `init`/`install` work from an installed wheel too.
    assert _worc_root().joinpath("README.md").is_file()
    assert _worc_root().joinpath("tasks", "task-minimal.md").is_file()
    assert _worc_root().joinpath("tasks", "task-rich.md").is_file()
    assert _worc_root().joinpath("tasks", "skills", "worc-task", "SKILL.md").is_file()
    assert _worc_root().joinpath("tasks", "skills", "worc-deco-task", "SKILL.md").is_file()
    assert _worc_root().joinpath("config", "README.md").is_file()
    assert _worc_root().joinpath("config", "skills", "worc-config", "SKILL.md").is_file()


def test_shipped_task_samples_pass_validation(packaged_config_text: str) -> None:
    config = loads_config(packaged_config_text).config
    gate = ValidationGate(
        config,
        store_has_task_id=lambda _i: False,
        ledger_has_task_id=lambda _i: False,
        is_recovery_rerun=lambda _i: False,
    )
    with resources.as_file(_worc_root()) as wroot:
        samples = sorted((Path(wroot) / "tasks").glob("*.md"))
        assert samples, "no task samples shipped under worc/tasks/"
        for path in samples:
            source = ParsedSource(path=str(path), suffix=path.suffix, raw_bytes=path.read_bytes())
            result = gate.validate(source)
            assert result.passed is True, f"{path.name} failed validation: {result.reason}"
            assert result.normalized is not None
