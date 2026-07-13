# claude-mv

Repoint Claude Code's stored history when you rename or move a project directory.

## The problem

Claude Code keeps each project's sessions under `~/.claude/projects/<encoded-path>/`.
The directory name is the project's absolute path with every non-alphanumeric character replaced by `-`.
So `/Users/me/my_proj` is stored as `-Users-me-my-proj`.

Rename the project on disk and that encoded name stops matching.
Claude Code then starts a fresh, empty history and your old sessions look lost.
They are not gone, they are just filed under the old name.

`claude-mv` refiles them under the new name.

## Usage

```bash
# You already renamed ~/code/old-name to ~/code/new-name; fix Claude's bookkeeping:
uv run claude_mv.py ~/code/old-name ~/code/new-name

# Preview without changing anything:
uv run claude_mv.py ~/code/old-name ~/code/new-name --dry-run

# Also move the real project directory (default: leave the filesystem alone):
uv run claude_mv.py ~/code/old-name ~/code/new-name --move-dir
```

`uv run` reads the inline dependency block at the top of the script and fetches `cyclopts` for you.
If `cyclopts` is already installed you can run `python claude_mv.py ...` directly.

Both paths are resolved (`~`, relative paths, and `..` all work).
The old directory does not need to still exist, since the tool works from Claude's stored copy of it.

### Options

| Flag | Meaning |
| --- | --- |
| `--dry-run`, `-n` | Show the plan and touch nothing. |
| `--move-dir` | Also move the real project directory, not just `~/.claude`. |
| `--rewrite-content` | Also rewrite incidental path mentions inside session and history files, not just the pointer fields (see below). |
| `--on-conflict abort\|merge\|clean` | What to do if the destination already has history (default `abort`). |
| `--yes`, `-y` | Skip the confirmation prompt. Required in a non-interactive shell. |
| `--force` | Override the destination-identity safety check (see below). |
| `--claude-dir PATH` | Point at a different data directory (default `~/.claude`, handy for testing). |

## What it changes

1. Renames `~/.claude/projects/<encoded-old>` to `<encoded-new>` (this carries the session files and the `memory/` subdir along with it).
2. Rewrites the `cwd` field inside each session `*.jsonl`.
3. Rewrites the `project` field in `~/.claude/history.jsonl`.
4. Remaps the project's entry in `~/.claude.json` (the `projects` map keyed by absolute path, plus `githubRepoPaths`).
   This is the per-project config: allowed tools, MCP servers, trust acceptance, and stats.
   Both community shell scripts miss this, so a rename silently drops those settings.
5. Defensively renames any `<encoded-old>` entry under the sibling dirs `todos/`, `file-history/`, `shell-snapshots/`, and `debug/`.
   On current Claude Code these are keyed by session id, not project path, so there is usually nothing to move.
   Some versions key them by path, and this keeps the tool correct for them.

By default it does not move your actual project directory.
Pass `--move-dir` if you want it to.

## Re-running (resumable)

Running the same `OLD NEW` again is safe and resumable.
If the default migration already happened, the tool notices that the project now lives at the new encoded name and only does the work that is left, rather than reporting "nothing found".

So a common flow is to run it once, then re-run with an added flag:

```bash
uv run claude_mv.py ~/code/old ~/code/new                 # metadata only
uv run claude_mv.py ~/code/old ~/code/new --rewrite-content # now also fix incidental mentions
uv run claude_mv.py ~/code/old ~/code/new --move-dir        # now also move the real folder
```

Each of these steps is idempotent: once its work is done, re-running it just prints "Already migrated; nothing to do".

The state is verified rather than guessed at.
If the project's sessions mix old and new `cwd` references (a half-finished migration), the tool refuses and asks you to re-run with `--heal` to finish it.
If `--move-dir` is asked for but the real directory is in an in-between state (both the old and new paths exist, or neither does), it refuses too.

## Why it is careful where the reference scripts are not

This is a from-scratch reimplementation of the shell `claude-mv` scripts, written to avoid their data-loss modes.

- **JSON-aware, field-scoped rewrites.**
  By default it parses each line and edits only the location-pointer fields: `cwd` in sessions, `project` in history, and the `projects`/`githubRepoPaths` entries in `~/.claude.json`.
  It never does a blind text substitution, so an old path that appears incidentally inside a logged shell command or captured tool output is left untouched.
  Rewriting that incidental text would corrupt the historical record and can silently mangle unrelated data.
  If you do want the incidental mentions rewritten too, `--rewrite-content` opts in, and even then the replacement is path-boundary-aware so `/proj` inside `/proj-2` is still safe.

- **Correct, verified encoding.**
  The encoding replaces every non-alphanumeric character, not just `/` and `.`.
  This was checked against every local project directory.
  The common shell version only substitutes `/` and `.`, so it silently fails on any path containing `_`, a space, `@`, `+`, and so on.

- **Prefix-boundary matching.**
  A path is only rewritten when it equals the old path or sits under it with a real separator boundary.
  So renaming `/proj` never disturbs `/proj-2`.

- **Encoding-collision guard.**
  The encoding is lossy, so two different real paths can map to the same directory name (`/Users/a.b` and `/Users/a/b` both become `-Users-a-b`).
  If the destination already holds history that belongs to a genuinely different project, `merge` and `clean` refuse rather than blend or delete an unrelated project's data.
  Use `--force` only when you are sure.

- **Backup and rollback.**
  Before any change it copies the affected files into `~/.claude/claude-mv-backups/<timestamp>/`.
  If anything fails midway, it restores the original state.
  Writes are atomic (temp file plus rename).
  These backups are never pruned, so delete old ones yourself once you no longer need them.

## Scope note

On current Claude Code, `todos/`, `file-history/`, `session-env/`, `shell-snapshots/`, and `debug/` are keyed by session id or content hash, not by the project path.
A project rename does not change those keys, so their contents are left alone.
The path can still appear inside them (a debug log line, a snapshot of a file that mentions its own path), but that is historical content, not a location pointer, so rewriting it would be wrong.
The one exception is a `<encoded>` directory keyed by the project path, which this tool does move (see item 5 above).
