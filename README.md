# claude-mv

Rename or move a project directory without losing its Claude Code history.

## The problem

Claude Code files each project's sessions under a name derived from the project's full path.
Move or rename the directory and Claude looks under the new name, finds nothing, and starts fresh.
Your past conversations and per-project settings are still on disk, just filed under the old path.

`claude-mv` refiles them under the new one.

## Quick start

```bash
# You renamed ~/code/old to ~/code/new. Now point Claude's records at the new path:
uv run claude_mv.py ~/code/old ~/code/new
```

`claude_mv.py` is a single self-contained file.
It declares its own dependencies, so `uv run` fetches them the first time and there is nothing to install.
Add `-n` / `--dry-run` to see the plan without changing anything.

Haven't renamed the folder yet? Let the tool do that too:

```bash
uv run claude_mv.py ~/code/old ~/code/new --move-dir
```

## What it updates

Everything Claude keys by the project path:

- the session transcripts, and the working directory recorded inside them
- the project's line in `~/.claude/history.jsonl`
- the project's entry in `~/.claude.json`: allowed tools, MCP servers, trust, usage stats

Your actual project directory is left alone unless you pass `--move-dir`.

## Options

| Option | |
| --- | --- |
| `-n`, `--dry-run` | Show the plan and change nothing. |
| `--move-dir` | Move the real project folder too, not just the records. |
| `--rewrite-content` | Also replace the old path where it is mentioned inside logged commands and captured output, not only in the fields Claude reads. |
| `--on-conflict merge\|clean` | If the destination already has history: `merge` the two, or `clean` (back it up and replace it). Defaults to stopping. |
| `--heal` | Finish a half-done migration (see below). |
| `-y`, `--yes` | Skip the confirmation prompt (required when there's no terminal). |

Both paths accept `~`, relative paths, and `..`. The old folder does not need to still exist.

## Re-running is safe

Run it again with the same arguments and it resumes from wherever it left off, so a two-step flow just works:

```bash
uv run claude_mv.py ~/code/old ~/code/new              # records only
uv run claude_mv.py ~/code/old ~/code/new --move-dir   # ...then move the folder
```

When there is nothing left to do, it says so and stops.

## What makes this fiddly (and why the tool exists)

Doing it by hand is trickier than renaming a folder:

- **The stored name is not your path.** Claude replaces every non-alphanumeric character with `-`, and the mapping is lossy, so you cannot reliably reverse it. The tool computes the name exactly the way Claude does.
- **The path is written in several places.** Rename just the folder and the working directory inside every transcript still points at the old path, as do `history.jsonl` and `~/.claude.json`. Miss one and the history looks half-broken.
- **Prefixes are a trap.** Renaming `/proj` must not touch `/proj-2` or `/proj/sub` by accident. Every rewrite is anchored to a real path boundary.
- **Half-finished states are ambiguous.** If a project ends up with some records pointing at the old path and some at the new one, the tool refuses to guess and asks you to re-run with `--heal`.

Before it changes anything it writes a backup under `~/.claude/claude-mv-backups/`, and if a step fails partway it rolls the whole thing back.

## Notes

- `--rewrite-content` does a best-effort literal replacement of the old path inside free text (commands you ran, tool output). It is off by default because that text is a record of what actually happened, and the default run leaves it untouched.
- Backups are never cleaned up automatically. Delete old ones from `~/.claude/claude-mv-backups/` when you no longer need them.
- Needs Python 3.12+ (uv will fetch one if needed).
