Establish the scope of the security audit for the repository at `{repo}` before any investigation begins: which components and trust boundaries are in scope, which classes of issue to look for, and which surfaces are out of scope by design.

## Ground It In The Project

Look for how this project declares its own security posture and trust model — a `SECURITY.md`, a threat model doc, or security rules under `.agents/rules/`/`CLAUDE.md`/`AGENTS.md`. Read it first, because the audit's job is to check that the code actually holds that line, not to invent a generic checklist. If no such document exists, infer the trust boundary from the product itself: what data or code it accepts as untrusted input, and from where.

## The Trust Boundary (What Is In Scope)

Identify the audit surface from what the product actually does — a non-exhaustive starting checklist:

- **Config/input loading** — how configuration or arguments are parsed and validated, and whether an unvalidated value can reach behavior.
- **Any exposed interface** — CLI, API, RPC/MCP server, or web endpoint: how it authenticates/authorizes callers (if at all), and how it validates input.
- **Filesystem & parsing** — path handling, symlink/traversal exposure, and parsing of untrusted content.
- **Child-process execution** — anywhere a process is spawned, and whether arguments are passed as an argv list or through shell interpolation.
- **Install & packaging** — install-time scripts or side effects.
- **Diagnostics & reports** — whether errors, logs, or generated output can leak secrets, environment values, or internal paths.

If the project explicitly declares certain surfaces **out of scope by design** (e.g. "no network access," "no code execution," "local-first only"), treat the mere **presence** of code that crosses that declared boundary as a finding in itself, not a surface to harden — record which of these you will treat that way.

## Your Job

State the scope precisely: the components and trust boundaries above that apply to the code actually in the working tree, the classes of issue to look for in each, and what a complete audit must cover. Do not widen scope beyond a security audit of this repository, and do not invent surfaces the project doesn't have — if there is no HTTP server, no auth layer, or no secret store here, say so rather than inventing one to audit.

This is a read-only scoping pass. Do not edit code or write files anywhere — you only return the typed structured result required by the output schema. Set `human_input` **only** for a genuine scoping decision that cannot be made safely from repository evidence; if a `human_input` context file is already present, apply that answer and do not repeat the question.
