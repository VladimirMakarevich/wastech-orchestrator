# Releasing

How to cut a release or pre-release of **wastech-orchestrator**. Releases are **tag-driven**: the
package version comes from the Git tag (hatch-vcs), and pushing the tag triggers the
[release workflow](../.github/workflows/release.yml) which builds the artifacts and creates the
GitHub (pre)release. You do **not** edit a version field by hand.

## Version scheme (PEP 440)

Python pre-releases use `aN` / `bN` / `rcN` suffixes (not `-alpha.N`). Start with alpha:

| Stage | Version (from tag) | Tag |
|---|---|---|
| alpha | `0.1.0a1`, `0.1.0a2`, … | `v0.1.0a1` |
| beta | `0.1.0b1` | `v0.1.0b1` |
| release candidate | `0.1.0rc1` | `v0.1.0rc1` |
| final | `0.1.0` | `v0.1.0` |

The workflow marks any tag ending in `aN`/`bN`/`rcN` as a GitHub **pre-release** automatically; a
plain `vX.Y.Z` tag becomes a normal release.

## Steps

1. **Green main.** `ruff check .`, `ruff format --check .`, `mypy src`, `pytest` all pass (`/run-checks`). Docs are in sync (`/sync-docs`).
2. **CHANGELOG.** Move the `[Unreleased]` items in [CHANGELOG.md](../CHANGELOG.md) under a new heading, e.g. `## [0.1.0a1] - YYYY-MM-DD`, and commit.
3. **Tag and push** (this is the release trigger):
   ```bash
   git tag v0.1.0a1
   git push origin v0.1.0a1
   ```
4. **CI does the rest.** The `release` workflow re-runs the checks, builds the wheel + sdist, and
   creates the GitHub release (pre-release for an alpha/beta/rc tag), attaching the artifacts.
5. **Verify the install** from the tag (no PyPI needed):
   ```bash
   pipx install "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git@v0.1.0a1"
   wastech-orchestrator --version      # -> 0.1.0a1
   ```

Recommended first tag: **`v0.1.0a1`**.

## Manual fallback (no CI)

If you need to release without Actions:

```bash
pip install build
python -m build                                   # dist/*.whl + *.tar.gz (version from the current tag)
gh release create v0.1.0a1 dist/* --prerelease --title "v0.1.0a1" --generate-notes
```

## Notes

- **Version source.** Between tags, local/CI builds produce a dev version like
  `0.1.0a1.dev3+g<sha>` (and a `.dDATE` suffix when the tree is dirty). Only a clean, tagged checkout
  produces a bare `0.1.0a1` — so always release from a clean tag.
- **hatch-vcs needs full history.** CI checks out with `fetch-depth: 0`; locally, a shallow clone
  would mis-resolve the version.
- **Not yet on PyPI.** Distribution is via `pipx`/`pip` from the Git tag. Publishing to (Test)PyPI
  with trusted publishing is tracked in [docs/backlog/follow_ups.md](backlog/follow_ups.md).
