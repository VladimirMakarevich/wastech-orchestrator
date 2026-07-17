From the repository analysis and the dependency-scan advisories, identify the concrete security threats to this project.{?repository_analysis_path} Build on the surface mapped at {repository_analysis_path}.{/repository_analysis_path}{?scope_path} Stay inside the scope fixed at {scope_path}.{/scope_path} Fold any dependency-scan advisories into your analysis — for each advisory, judge whether the vulnerable code path is actually reached by this product rather than treating the advisory as a finding on its own.

## Threat Model

Frame each threat against the trust boundary and declared posture from the scope step. If the project declares a surface out of scope by design (e.g. no network, no code execution, no remote config), a threat inside that declared boundary is a **presence finding** — the code itself being there is the issue, not a hardening gap. For everything else, consider the standard classes as they apply here: injection/deserialization risk in input parsing, missing or weak authorization on an exposed interface, path traversal or symlink escape in filesystem access, command injection through unsafe process spawning, information disclosure in errors/diagnostics/logs, and install-time side effects.

## For Each Threat

Give: the attack, the affected location as `path:line` (or the dependency + advisory id), the impact, and a severity. **Distinguish exploitable from theoretical** — state the concrete precondition and input that trigger the issue and whether an attacker actually controls them given this project's trust model, versus a defense-in-depth concern with no reachable trigger in the code today. Mark the latter explicitly so verification and the report rank it below a reachable issue, and do not inflate a theoretical concern into a confirmed vulnerability.

Use only the network access the flow grants, and only to confirm an **upstream advisory or CVE detail** for a flagged dependency — not to fetch anything about this local repository. If network is unavailable, say so and fall back to the advisory text. Read only; do not edit code or write files. Return the typed structured result required by the output schema.
