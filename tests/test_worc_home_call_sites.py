"""Guard: no module rebuilds a ``.worc`` / ``.worc-io`` path from a bare string literal (WRI-004).

Path construction for the runtime homes must go through :mod:`wastech_orchestrator.runtime_layout`
(the ``RuntimeLayout`` factory / the ``*_HOME_DIRNAME`` constants), never a hand-joined literal such
as ``repo_root / ".worc"``. This is a targeted check over **path-construction call sites** (a ``/``
division whose operand is a ``.worc`` string literal) — deliberately *not* a brittle ban on the
``.worc`` text, so legitimate docstrings, help strings, config-default value strings, and the
``config/validation`` guard literal remain allowed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "wastech_orchestrator"

# A pure path literal naming one of the runtime homes (``.worc``, ``.worc-io``, ``./.worc/...``),
# distinct from prose that merely mentions ``.worc`` (prose has spaces / is multi-line).
_WORC_PATH_LITERAL = re.compile(r"^\.?/?\.worc(-io)?(/[\w./*-]*)?$")

# Files allowed to construct a ``.worc`` path from a literal division. Empty by design: everything
# routes through ``runtime_layout``. Add an entry (with a reason) only for a genuinely unavoidable
# literal — never to silence an accidental reintroduction.
_ALLOWED_LITERAL_DIVISION: frozenset[str] = frozenset()


def _literal_worc_divisions(tree: ast.AST) -> list[tuple[int, str]]:
    """Line/value pairs for every ``<expr> / "<.worc...>"`` (or reversed) division in ``tree``."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            for operand in (node.left, node.right):
                if (
                    isinstance(operand, ast.Constant)
                    and isinstance(operand.value, str)
                    and _WORC_PATH_LITERAL.match(operand.value)
                ):
                    hits.append((node.lineno, operand.value))
    return hits


def test_no_literal_worc_path_construction_outside_runtime_layout() -> None:
    offenders: dict[str, list[tuple[int, str]]] = {}
    for path in SRC_ROOT.rglob("*.py"):
        rel = path.relative_to(SRC_ROOT).as_posix()
        if rel in _ALLOWED_LITERAL_DIVISION:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = _literal_worc_divisions(tree)
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "these modules build a .worc/.worc-io path from a bare literal instead of going through "
        f"runtime_layout (RuntimeLayout / *_HOME_DIRNAME): {offenders}"
    )


def test_runtime_layout_defines_the_canonical_names() -> None:
    from wastech_orchestrator import runtime_layout

    assert runtime_layout.CONTROL_HOME_DIRNAME == ".worc"
    assert runtime_layout.PRIVATE_HOME_DIRNAME == ".worc"
    assert runtime_layout.EXCHANGE_HOME_DIRNAME == ".worc-io"
