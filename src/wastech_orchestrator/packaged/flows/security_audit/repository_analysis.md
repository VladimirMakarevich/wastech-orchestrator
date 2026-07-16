Analyze the repository at `{repo}` for security-relevant surface, within the agreed scope.{?scope_path} Work within the scope fixed at {scope_path} — cover every component and issue class it lists, and honor anything it marked out of scope.{/scope_path}

## Where The Security-Relevant Surface Lives

Trace, for each component the scope names: entry points and how input reaches them, authentication/authorization (if any), secret handling, any exposed interface's argument validation, filesystem and parsing paths, anywhere a child process is spawned, install-time scripts, and how errors/diagnostics are rendered. Follow untrusted input from where it enters to where it reaches a sink (a file read, a process spawn, a query, a rendered response) rather than looking at files in isolation.

## Delivery Evidence

Git history is always present and authoritative: `git log` / `git show` reveal what each change actually did and can surface a risky pattern introduced in a specific commit. If prior orchestrator run logs happen to be present in the working tree (e.g. under `.worc/logs/`), treat them as a supplement only — they are typically gitignored, so their absence is normal, never a finding.

## What To Record

Record the exact `path:line` for every security-relevant observation — these anchor the later threat findings and must point at text that is really there. For each surface note: the untrusted input, where it enters, what validation or normalization it passes through, and where it reaches a sink. Note also the **absence** of an expected control — input used without normalization, a user-supplied value with no bound, an error path that could echo a filesystem path or environment value, an argument that skips its declared validation.

Do not assess exploitability yet — that is the threat step's job; here you map the surface and the evidence for it. Read only; do not edit code or write files. Return the typed structured result required by the output schema.
