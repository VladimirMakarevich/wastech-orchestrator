"""Flow engine.

The universal flow-execution layer that replaces the hardcoded pipeline: a declarative graph
(YAML+MD) executed by a thin engine over a fixed core. The provider-neutral execution vocabulary
lives in :mod:`.contracts`, alongside the schema, the validator, and the engine itself.
"""

from __future__ import annotations
