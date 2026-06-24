"""Packaged agent task-authoring docs: availability, source sync, and example validity.

The authored docs live in ``docs/worc/`` and a byte-identical copy ships as package data under
``wastech_orchestrator/packaged/guide/`` (the aggregated package-data home). The shipped example
tasks must pass the validation gate.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from wastech_orchestrator.cli import _iter_template_files, _worc_root
from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.task.parser import ParsedSource
from wastech_orchestrator.task.validation_gate import ValidationGate

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCS_WORC = _REPO_ROOT / "docs" / "worc"


def test_worc_packaged_data_discoverable() -> None:
    # Packaged via importlib.resources, so `init`/`install` work from an installed wheel too.
    assert _worc_root().joinpath("README.md").is_file()
    assert _worc_root().joinpath("examples", "task-minimal.md").is_file()


def test_docs_worc_in_sync_with_packaged() -> None:
    # Single source of truth: the authored docs/worc/ and the packaged copy must not drift. Update
    # both (copy docs/worc/* into src/wastech_orchestrator/packaged/guide/) when editing either.
    docs_files = {p.relative_to(_DOCS_WORC) for p in _DOCS_WORC.rglob("*") if p.is_file()}
    with resources.as_file(_worc_root()) as wroot:
        packaged_files = set(_iter_template_files(Path(wroot)))
        assert docs_files == packaged_files, "docs/worc and packaged worc/ have different files"
        for rel in sorted(docs_files):
            assert (_DOCS_WORC / rel).read_bytes() == (Path(wroot) / rel).read_bytes(), (
                f"docs/worc/{rel} differs from the packaged copy"
            )


def test_shipped_example_tasks_pass_validation(packaged_config_text: str) -> None:
    config = loads_config(packaged_config_text).config
    gate = ValidationGate(
        config,
        store_has_task_id=lambda _i: False,
        ledger_has_task_id=lambda _i: False,
        is_recovery_rerun=lambda _i: False,
    )
    with resources.as_file(_worc_root()) as wroot:
        examples = sorted((Path(wroot) / "examples").glob("*.md"))
        assert examples, "no example tasks shipped under worc/examples/"
        for path in examples:
            source = ParsedSource(path=str(path), suffix=path.suffix, raw_bytes=path.read_bytes())
            result = gate.validate(source)
            assert result.passed is True, f"{path.name} failed validation: {result.reason}"
            assert result.normalized is not None
