# Phase 06 — Adopt the worc contract

- **Status:** ☐
- **Depends on:** 01, 02, 05
- **Delivers:** the connector emits `references: ["Fixes #<n>"]` so GitHub can close the issue on merge by itself, reads `pr_url` from `worc list --format json` instead of only discovering the PR by branch, and `write_back.close_on_merge: false` becomes a real choice.

## Goal

Replace the two places where v1 worked around worc — closing the issue itself and finding the PR by branch — with the contract worc now offers, and pin the minimum worc version that has it.

## Steps

1. `core/builder.py` — when the installed worc is at least the version that ships `references:`, emit `references` with the adapter-supplied closing line (`TrackerAdapter.closing_reference(item) -> str | None`; GitHub returns `Fixes #<n>`, an adapter with no keyword returns `None`). Below that version, omit the key — an unknown front-matter key is a hard reject in worc.
2. `core/reconcile.py` — prefer `pr_url` from `worc list --format json` when present; keep the by-branch lookup as the fallback and for `mergedAt`.
3. Configuration: `close_on_merge` documented as "off lets GitHub close via `Fixes #n`"; `init` keeps the default on.
4. Version detection: `worc --version` parsed once per process; the connector's README states the minimum.

## Files touched

- connector: `src/worc_connect/core/{builder,reconcile}.py`, `src/worc_connect/trackers/base.py`, `src/worc_connect/trackers/github/`, `tests/`, README.

## Invariants in play

- worc still interprets nothing: the adapter authors the closing line, worc appends it.
- The connector never emits a key the installed worc does not accept.

## Tests

- Builder with/without a capable worc version; adapter without a closing keyword; reconcile prefers `pr_url` and falls back by branch; real run with `close_on_merge: false` where GitHub closes the issue on merge.

## Docs to sync in this phase

- Connector README and configuration reference.

## Acceptance for this phase

- [ ] With `close_on_merge: false`, a real run ends with the issue closed by GitHub on merge and the PR body ending in `## References` / `Fixes #<n>`.
- [ ] Both CI families green.
