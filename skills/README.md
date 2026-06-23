# Task-authoring skills

Copy-ready [Claude Code skills](https://docs.claude.com/en/docs/claude-code/skills) that help a user turn raw, free-form work into task files **wastech-orchestrator** will accept. They encode the task contract (the same rules the validation gate enforces) so the output passes on the first try. Each skill is self-contained — the rules are embedded, so a skill keeps working after you copy it into another repository.

| Skill | Use it to |
| --- | --- |
| [worc-task](worc-task/SKILL.md) | Convert one raw task (a paragraph, a ticket) into a single valid task file ready for `tasks/pending/`. |
| [worc-deco-task](worc-deco-task/SKILL.md) | Convert one coherent change with ordered steps into operator-authored decomposition — a root task plus per-subtask spec files (one branch, one PR). |

## How to use them now

These are reference templates. To use one, copy its directory into a skills location Claude Code reads, then invoke it:

```bash
# Global (available in every project):
cp -r skills/worc-task ~/.claude/skills/

# Or per-project (available in that repo only):
cp -r skills/worc-task <your-repo>/.claude/skills/
```

Then run `/worc-task` (or `/worc-deco-task`) in Claude Code and paste your raw task.

A dedicated command to install these skills directly is planned; until then, copy them as above.

## See also

The full operator references for the task format live in the orchestrator's own docs: [docs/task-authoring.md](../docs/task-authoring.md) and the compact agent-facing guide in [docs/worc/](../docs/worc/README.md). The skills here distill those into an interactive authoring helper; you do not need to read the docs to use a skill.
