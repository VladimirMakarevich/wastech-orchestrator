# The Markdown gate's second half: the config that exists only on `main`

Status: **applied** — the overlay exists on the documentation branch and the dead link in `operations.md` is gone Date: 2026-08-11 Owner: Vladimir Makarevich

## Why this is a separate item

The Markdown gate is one shared config, [../../wastech-mdlint.config.json](../../wastech-mdlint.config.json), authored on `dev` and carried to `main` by the ordinary merge. It covers both branch states without a branch switch, because a glob that selects nothing is an empty set: `docs/**` means the task queue here and the queue plus the derived documentation there, and a rule scoped at `docs/*.md` is simply inert on `dev`.

Exactly one class of rule cannot work that way — a rule that asserts the derived documentation **exists**. On `dev` it would fail by design. It therefore lives in a second, additive config that is present only on `main`, and creating that file is work on `main`, which is why it is written down here instead of being committed on `dev`.

The file's absence from `dev` is the whole mechanism: a merge cannot conflict on a path the incoming branch does not carry, so `dev → main` never touches it. The same file with different contents on both branches would conflict on every merge, forever — which is why the shared config must never be edited on `main` either.

## What to create on `main`

`wastech-mdlint.docs.config.json`, at the repository root, beside the shared config. Verified against the current `main` tree: it reports nothing, and moving any one of the eleven pages away makes it report that page as missing.

```jsonc
{
  "$schema": "./wastech-mdlint.schema.json",

  // Additive, not a second opinion: the shared config already lints every file here, so this one adds
  // only what cannot be expressed on the development branch. Nothing is reported twice.
  "include": ["docs/*.md"],

  "rules": [
    // The published documentation set, page for page. This is what turns "the derived docs were
    // reconstructed from the merged diff" from a promise into a check: a page deleted, renamed, or
    // never written fails the build on the branch that owns it. The list is the same set the site
    // publishes, so it stays in step with the navigation rather than describing a wish.
    {
      "rule": "STR-001",
      "options": {
        "files": [
          "docs/index.md",
          "docs/how-it-works.md",
          "docs/how-to.md",
          "docs/operations.md",
          "docs/configuration.md",
          "docs/task-authoring.md",
          "docs/flow-authoring.md",
          "docs/cookbook.md",
          "docs/telegram.md",
          "docs/glossary.md",
          "docs/worc_architecture.md",
        ],
      },
    },
  ],
}
```

Nothing else has to change: [../../tools/mdlint.py](../../tools/mdlint.py) already runs the shared config plus every overlay it finds, so the same command and the same pre-commit hook cover the new file the moment it is committed.

## One dead link to remove in the same change

`docs/operations.md` (line 124 at the time of writing) links to `backlog/install-and-upgrade-flow.md`. That document was retired along with the rest of the backlog archive, so the target does not exist on either branch — drop the link and keep the sentence, the way the queue documents on `dev` were cleaned. It is the **only** finding the shared config reports on `main` that a `dev → main` merge will not clear on its own; every other one there is a stale copy of a shared file that is already clean on `dev`. The page is [operations.md on main](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/operations.md).

That merge is also where the archive itself leaves `main`: `dev` deleted `docs/backlog/archive/**` and `docs/backlog/deep-research-postmortem/**`, so the merge proposes those deletions and they must be **accepted**. Keeping them would restore documents nothing links to any more, and the shared config no longer excuses their internal links.

## Verifying it

In a `main` checkout, with the linter available:

```bash
python tools/mdlint.py          # runs the shared config, then this overlay
```

Both passes must print `No problems found.` The gate is only worth reading while that is true — a rule joins one of the two configs once it measures zero, never before.

## Candidates for the same file later

Neither is written yet, and both are documentation-branch work rather than something `dev` can prepare:

- **CTX-003** (content uses canonical glossary terms) needs `glossary.md` to expose a canonical-term column and an alias column it can read. That is a change to the glossary's shape, so it is a decision for whoever owns that page, not a config line.
- **SEC-003** (sections conform to a template) needs one of the guide pages nominated as the template the others follow. The pages differ on purpose today, so nominating one is an editorial decision first and a rule second.
