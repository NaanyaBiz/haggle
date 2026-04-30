# /wt

Manage sibling git worktrees for parallel feature development.

## Usage

```
/wt new <branch>    — create a new worktree at ../haggle.wt/<branch>/
/wt list            — list active worktrees
/wt rm <branch>     — remove a worktree (refuses if dirty)
```

## What this does

Runs `./scripts/wt <subcommand>` and prints the next step.

For `/wt new <branch>`:
1. Run `./scripts/wt new <branch>`
2. Print: "Worktree created at `../haggle.wt/<branch>/`. Open a new Claude Code session there to work on feature `<branch>` in isolation."

For `/wt rm <branch>`:
1. Run `./scripts/wt rm <branch>`
2. Confirm removal with a one-line summary.

## Convention

- Main worktree (`/Users/dave/projects/haggle/`) is always on `main`.
- Feature work happens in `../haggle.wt/<branch>/`.
- Each worktree shares `.venv` and `.claude/settings.local.json` via symlink.
- Never commit directly to `main` from a feature worktree — always PR.

## See also

`scripts/wt` — the actual implementation.
`AGENTS.md` — full worktree workflow documentation.
