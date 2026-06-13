"""Strict validation of agent discovery output (automatic check discovery §6, §12)."""

from __future__ import annotations

from wastech_orchestrator.checks.schema_validate import validate_discovery_output

_VALID = {
    "checks": [
        {
            "name": "tests",
            "argv": [".venv/bin/python", "-m", "pytest"],
            "evidence": ["pytest in pyproject.toml"],
            "confidence": "high",
        }
    ],
}


def test_valid_document_parses() -> None:
    doc = validate_discovery_output(_VALID)
    assert doc is not None
    assert doc.checks[0].name == "tests"
    assert doc.checks[0].argv == (".venv/bin/python", "-m", "pytest")


def test_non_dict_rejected() -> None:
    assert validate_discovery_output(None) is None
    assert validate_discovery_output("nope") is None


def test_unknown_top_level_key_rejected() -> None:
    assert validate_discovery_output({**_VALID, "extra": 1}) is None


def test_missing_or_empty_checks_rejected() -> None:
    assert validate_discovery_output({}) is None
    assert validate_discovery_output({"checks": []}) is None


def test_unknown_item_key_rejected() -> None:
    bad = {"checks": [{"name": "t", "argv": ["pytest"], "confidence": "high", "x": 1}]}
    assert validate_discovery_output(bad) is None


def test_bad_confidence_rejected() -> None:
    bad = {"checks": [{"name": "t", "argv": ["pytest"], "confidence": "certain"}]}
    assert validate_discovery_output(bad) is None


def test_missing_confidence_on_check_rejected() -> None:
    bad = {"checks": [{"name": "t", "argv": ["pytest"]}]}
    assert validate_discovery_output(bad) is None


def test_shell_metacharacter_argv_rejected() -> None:
    bad = {"checks": [{"name": "t", "argv": ["pytest;", "rm"], "confidence": "high"}]}
    assert validate_discovery_output(bad) is None


def test_absolute_path_rejected() -> None:
    bad = {"checks": [{"name": "t", "argv": ["/usr/bin/pytest"], "confidence": "high"}]}
    assert validate_discovery_output(bad) is None


def test_empty_argv_rejected() -> None:
    bad = {"checks": [{"name": "t", "argv": [], "confidence": "high"}]}
    assert validate_discovery_output(bad) is None


def test_too_many_checks_rejected() -> None:
    items = [{"name": f"c{i}", "argv": ["x"], "confidence": "low"} for i in range(13)]
    assert validate_discovery_output({"checks": items}) is None


def test_unknown_top_level_setup_key_rejected() -> None:
    assert validate_discovery_output({**_VALID, "setup": []}) is None
