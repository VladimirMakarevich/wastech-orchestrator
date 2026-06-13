# CLAUDE.md — instructions for Claude Code in this repository

You are working on **wastech-orchestrator** — an orchestrator that launches coding agents (Codex / Claude Code) to carry out development tasks and publish the result to Git.

## Before writing code

1. Read **[docs/implementation_stages/00_orchestrator_final_plan.md](docs/implementation_stages/00_orchestrator_final_plan.md)** — this is the canonical spec. In case of any discrepancy, it takes precedence over architecture.md.
2. Check against the rules in **[docs/rules/](docs/rules/)** — they are mandatory:
   - [architecture.md](docs/rules/architecture.md) — invariants that must not be violated
   - [coding-style.md](docs/rules/coding-style.md) — Python style
   - [security.md](docs/rules/security.md) — security policy
   - [git-workflow.md](docs/rules/git-workflow.md) — branches, commits, PRs
   - [testing.md](docs/rules/testing.md) — what to test and how

## Hard invariants (must not be violated)

- **The core does not know the CLI syntax.** All provider-specific logic lives only in `src/wastech_orchestrator/providers/`. The core only ever calls the `AgentProvider` interface.
- **Only the orchestrator does commit / push / PR**, not the agent provider. Providers do not perform fallback and do not change the state machine.
- **Fallback is only for infrastructure errors** (`binary_not_found`, `timeout`, `rate_limited`, …). Test/review errors go to the `fixing` stage, not to another provider.
- **The security policy cannot be weakened** through a task or `extra_args`. No flags that bypass sandbox/approvals.
- **No secrets** in logs, in SQLite, or in artifacts. Pass only allowlisted env variables to processes.
- **Launch the CLI without shell interpolation** of user strings (an argument list, not a string).

## Canonical names (do not invent your own)

- Providers: `codex`, `claude`.
- Stages: `refinement`, `planning`, `implementation`, `testing`, `review`, `fixing`, `summary`, `publishing`.
- Branch prefix: `agent/<task-id>-<slug>`.
- State machine statuses: see [docs/implementation_stages/00_orchestrator_final_plan.md §8](docs/implementation_stages/00_orchestrator_final_plan.md).

## Commands

```bash
pip install -e ".[dev]"   # install
ruff check .              # lint
mypy src                  # types
pytest                    # tests
```

There is a skill for running all checks: `/run-checks`.

## Working style

- Make minimal, focused changes; follow the style of the surrounding code.
- For new components, check against the contracts in [docs/implementation_stages/00_orchestrator_final_plan.md](docs/implementation_stages/00_orchestrator_final_plan.md) (§4, §7, §8).
- When adding/changing behavior — add or update tests (see docs/rules/testing.md).
- When you change behavior/CLI/config/architecture — update the affected docs and a `CHANGELOG.md` `[Unreleased]` entry **in the same change** (use `/sync-docs`), and record deferred work in [docs/backlog/follow_ups.md](docs/backlog/follow_ups.md). The Stop docs-sync gate enforces this.
- Before committing, run `ruff`, `mypy`, `pytest`.
- The MVP build (the six phases under [docs/implementation_stages/](docs/implementation_stages/)) is **complete**; those phase docs are now a historical record. Track ongoing work in [docs/backlog/follow_ups.md](docs/backlog/follow_ups.md) and the product backlog.

<!-- rtk-instructions v2 -->
# RTK (Rust Token Killer) - Token-Optimized Commands

## Golden Rule

**Always prefix commands with `rtk`**. If RTK has a dedicated filter, it uses it. If not, it passes through unchanged. This means RTK is always safe to use.

**Important**: Even in command chains with `&&`, use `rtk`:
```bash
# ❌ Wrong
git add . && git commit -m "msg" && git push

# ✅ Correct
rtk git add . && rtk git commit -m "msg" && rtk git push
```

## RTK Commands by Workflow

### Build & Compile (80-90% savings)
```bash
rtk cargo build         # Cargo build output
rtk cargo check         # Cargo check output
rtk cargo clippy        # Clippy warnings grouped by file (80%)
rtk tsc                 # TypeScript errors grouped by file/code (83%)
rtk lint                # ESLint/Biome violations grouped (84%)
rtk prettier --check    # Files needing format only (70%)
rtk next build          # Next.js build with route metrics (87%)
```

### Test (60-99% savings)
```bash
rtk cargo test          # Cargo test failures only (90%)
rtk go test             # Go test failures only (90%)
rtk jest                # Jest failures only (99.5%)
rtk vitest              # Vitest failures only (99.5%)
rtk playwright test     # Playwright failures only (94%)
rtk pytest              # Python test failures only (90%)
rtk rake test           # Ruby test failures only (90%)
rtk rspec               # RSpec test failures only (60%)
rtk test <cmd>          # Generic test wrapper - failures only
```

### Git (59-80% savings)
```bash
rtk git status          # Compact status
rtk git log             # Compact log (works with all git flags)
rtk git diff            # Compact diff (80%)
rtk git show            # Compact show (80%)
rtk git add             # Ultra-compact confirmations (59%)
rtk git commit          # Ultra-compact confirmations (59%)
rtk git push            # Ultra-compact confirmations
rtk git pull            # Ultra-compact confirmations
rtk git branch          # Compact branch list
rtk git fetch           # Compact fetch
rtk git stash           # Compact stash
rtk git worktree        # Compact worktree
```

Note: Git passthrough works for ALL subcommands, even those not explicitly listed.

### GitHub (26-87% savings)
```bash
rtk gh pr view <num>    # Compact PR view (87%)
rtk gh pr checks        # Compact PR checks (79%)
rtk gh run list         # Compact workflow runs (82%)
rtk gh issue list       # Compact issue list (80%)
rtk gh api              # Compact API responses (26%)
```

### JavaScript/TypeScript Tooling (70-90% savings)
```bash
rtk pnpm list           # Compact dependency tree (70%)
rtk pnpm outdated       # Compact outdated packages (80%)
rtk pnpm install        # Compact install output (90%)
rtk npm run <script>    # Compact npm script output
rtk npx <cmd>           # Compact npx command output
rtk prisma              # Prisma without ASCII art (88%)
```

### Files & Search (60-75% savings)
```bash
rtk ls <path>           # Tree format, compact (65%)
rtk read <file>         # Code reading with filtering (60%)
rtk grep <pattern>      # Search grouped by file (75%). Format flags (-c, -l, -L, -o, -Z) run raw.
rtk find <pattern>      # Find grouped by directory (70%)
```

### Analysis & Debug (70-90% savings)
```bash
rtk err <cmd>           # Filter errors only from any command
rtk log <file>          # Deduplicated logs with counts
rtk json <file>         # JSON structure without values
rtk deps                # Dependency overview
rtk env                 # Environment variables compact
rtk summary <cmd>       # Smart summary of command output
rtk diff                # Ultra-compact diffs
```

### Infrastructure (85% savings)
```bash
rtk docker ps           # Compact container list
rtk docker images       # Compact image list
rtk docker logs <c>     # Deduplicated logs
rtk kubectl get         # Compact resource list
rtk kubectl logs        # Deduplicated pod logs
```

### Network (65-70% savings)
```bash
rtk curl <url>          # Compact HTTP responses (70%)
rtk wget <url>          # Compact download output (65%)
```

### Meta Commands
```bash
rtk gain                # View token savings statistics
rtk gain --history      # View command history with savings
rtk discover            # Analyze Claude Code sessions for missed RTK usage
rtk proxy <cmd>         # Run command without filtering (for debugging)
rtk init                # Add RTK instructions to CLAUDE.md
rtk init --global       # Add RTK to ~/.claude/CLAUDE.md
```

## Token Savings Overview

| Category | Commands | Typical Savings |
|----------|----------|-----------------|
| Tests | vitest, playwright, cargo test | 90-99% |
| Build | next, tsc, lint, prettier | 70-87% |
| Git | status, log, diff, add, commit | 59-80% |
| GitHub | gh pr, gh run, gh issue | 26-87% |
| Package Managers | pnpm, npm, npx | 70-90% |
| Files | ls, read, grep, find | 60-75% |
| Infrastructure | docker, kubectl | 85% |
| Network | curl, wget | 65-70% |

Overall average: **60-90% token reduction** on common development operations.
<!-- /rtk-instructions -->
