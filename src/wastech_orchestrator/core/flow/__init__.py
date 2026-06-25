"""Flow engine (backlog, P0).

The universal flow-execution layer that replaces the hardcoded pipeline: a declarative graph
(YAML+MD) executed by a thin engine over a fixed core. P0.1 ships only the provider-neutral
execution vocabulary (:mod:`.contracts`); the schema/validator/engine arrive in later slices.
"""

from __future__ import annotations
