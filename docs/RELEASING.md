# Releasing

How to cut a release or pre-release of **wastech-orchestrator**. Releases are **tag-driven**: the
package version comes from the Git tag (hatch-vcs), and pushing the tag triggers the
[release workflow](../.github/workflows/release.yml) which builds the artifacts and creates the
GitHub (pre)release. You do **not** edit a version field by hand.

## Version scheme (PEP 440)

Python pre-releases use `aN` / `bN` / `rcN` suffixes (not `-alpha.N`). Start with alpha:

| Stage | Version (from tag) | Tag |
|---|---|---|
| alpha | `0.1.0a1`, `0.1.0a2`, … | `v0.1.1a1` |
| beta | `0.1.0b1` | `v0.1.0b1` |
| release candidate | `0.1.0rc1` | `v0.1.0rc1` |
| final | `0.1.0` | `v0.1.0` |

The workflow marks any tag ending in `aN`/`bN`/`rcN` as a GitHub **pre-release** automatically; a
plain `vX.Y.Z` tag becomes a normal release.

## Steps

1. **Green main.** `ruff check .`, `ruff format --check .`, `mypy src`, `pytest` all pass (`/run-checks`). Docs are in sync (`/sync-docs`).
2. **CHANGELOG = the release notes.** Move the `[Unreleased]` items in [CHANGELOG.md](../CHANGELOG.md) under a new heading `## [0.1.0a1] - YYYY-MM-DD` and write the human-facing comments there (Keep-a-Changelog: Added / Changed / Fixed / Removed). The workflow copies this exact section into the GitHub release body, so this is where your release comments live. Commit it.
3. **Tag and push** (this is the release trigger). Use an **annotated** tag with a short message — it records the intent in git itself (`git show v0.1.1a1`):
   ```bash
   git tag -a v0.1.1a1 -m "alpha 1 — interactive installer, tag-driven releases, versioning gates"
   git push origin v0.1.1a1
   ```
4. **CI does the rest.** The `release` workflow re-runs the checks, builds the wheel + sdist, and creates the GitHub release (pre-release for an alpha/beta/rc tag), attaching the artifacts. The release body is taken from this version's CHANGELOG section; if that section is missing it falls back to GitHub's auto-generated "What's Changed".
5. **Verify the install** from the tag (no PyPI needed):
   ```bash
   pipx install "git+https://github.com/VladimirMakarevich/wastech-orchestrator.git@v0.1.1a1"
   wastech-orchestrator --version      # -> 0.1.0a1
   ```

Recommended first tag: **`v0.1.1a1`**.

## Release notes & comments

The release body is your curated changelog for that version — that's where the "comments" go.

- **Source of truth = CHANGELOG.** Write the notes under `## [X.Y.Z] - DATE` in
  [CHANGELOG.md](../CHANGELOG.md); the workflow copies that section verbatim into the GitHub release.
  Keep them human-facing (what changed and why), grouped Added / Changed / Fixed / Removed.
- **Annotated tag message** (`git tag -a -m "…"`) records the release intent in git history. To use
  the *tag message* as the release body instead of the CHANGELOG, swap the workflow's notes step for
  `gh release create … --notes-from-tag`.
- **Override at creation** (manual releases): `--notes "free text"`, `--notes-file NOTES.md`, or
  `--generate-notes` (auto-built from merged PR titles — so write good PR titles).
- **Edit after publishing**: `gh release edit v0.1.1a1 --notes-file NOTES.md` (or GitHub UI → the
  release → *Edit*). The title and the pre-release flag are editable there too.
- **Optional**: add a `.github/release.yml` to group auto-generated notes by PR label
  (Features / Fixes / …) for when you lean on `--generate-notes`.

## Manual fallback (no CI)

If you need to release without Actions (notes from the annotated tag message; or use
`--notes-file` / `--generate-notes`):

```bash
pip install build
python -m build                                   # dist/*.whl + *.tar.gz (version from the current tag)
gh release create v0.1.1a1 dist/* --prerelease --title "v0.1.1a1" --notes-from-tag
```

## Notes

- **Version source.** Between tags, local/CI builds produce a dev version like
  `0.1.0a1.dev3+g<sha>` (and a `.dDATE` suffix when the tree is dirty). Only a clean, tagged checkout
  produces a bare `0.1.0a1` — so always release from a clean tag.
- **hatch-vcs needs full history.** CI checks out with `fetch-depth: 0`; locally, a shallow clone
  would mis-resolve the version.
- **Not yet on PyPI.** Distribution is via `pipx`/`pip` from the Git tag. Publishing to (Test)PyPI
  with trusted publishing is tracked in [docs/backlog/follow_ups.md](backlog/follow_ups.md).
