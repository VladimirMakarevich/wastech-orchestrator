"""Provider-agnostic check discovery and environment resolution (automatic check discovery).

This package sits between configuration/install and the Check Runner. It discovers, validates,
probes, and persists the repository's quality-gate profile so most repositories resolve a working
set of checks without hand-written, technology-specific commands:

```
RepositoryInspector -> CheckCandidateDetector -> [AgentCheckDiscovery (read-only fallback)]
    -> CheckCandidateValidator -> CheckProbeRunner -> ResolvedCheckProfileStore -> CheckRunner
```

Nothing here knows any provider's CLI syntax (architecture invariant): discovery proposes
``ResolvedCheck`` argv lists and the orchestrator-owned Check Runner remains the sole quality-gate
authority. See ``docs/implementation_stages/09_automatic_check_discovery.md``.
"""

from __future__ import annotations
