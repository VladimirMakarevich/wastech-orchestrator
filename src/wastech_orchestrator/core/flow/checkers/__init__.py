"""Core-owned ``checks``-node checkers beyond the command profile.

Two deterministic, non-LLM checkers that make the ``checks`` node usable for research / audit flows:

* :mod:`~.citation` — validates a synthesis ``sources.json`` manifest against the repository it
  cites (hallucinated citation → ``broken`` → the check fails, gating the synthesis loop).
* :mod:`~.dependency_scan` — runs the core-owned argv advisory scanners and structures their output
  as evidence (it never gates; it always emits ``pass`` so the ``checks`` node stays uniformly
  pass/fail and the engine needs no "this checker doesn't gate" special case).

Which checker a ``checks`` node runs is the node's ``checker`` field; the node runner
(``core/flow/nodes/checks.py``) dispatches on it. The flow never supplies commands/scanners
— the scanner set is core-owned here.
"""
