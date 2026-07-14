# claude-mv

[![Tests][tests-badge]][tests-link]
[![uv][uv-badge]][uv-link]
[![Ruff][ruff-badge]][ruff-link]
[![codecov][codecov-badge]][codecov-link]
\
[![Made Using tsvikas/python-template][template-badge]][template-link]
[![GitHub Discussion][github-discussions-badge]][github-discussions-link]
[![PRs Welcome][prs-welcome-badge]][prs-welcome-link]

## Overview

Rename or move a project directory without losing its Claude Code history.

Claude Code files each project's sessions under a name derived from the project's full path.
Move or rename the directory and Claude looks under the new name, finds nothing, and starts fresh.
Your past conversations and per-project settings are still on disk, just filed under the old path.

`claude-mv` refiles them under the new one.

## Install

Install this tool using uv (or pipx):

```bash
uv tool install git+https://github.com/tsvikas/claude-mv.git
```

## Quick start

```bash
# You renamed ~/code/old to ~/code/new. Now point Claude's records at the new path:
claude-mv ~/code/old ~/code/new
```

Add `-n` / `--dry-run` to see the plan without changing anything.

Haven't renamed the folder yet? Let the tool do that too:

```bash
claude-mv ~/code/old ~/code/new --move-dir
```

## What it updates

Everything Claude keys by the project path:

- the session transcripts, and the working directory recorded inside them
- the project's line in `~/.claude/history.jsonl`
- the project's entry in `~/.claude.json`: allowed tools, MCP servers, trust, usage stats

Your actual project directory is left alone unless you pass `--move-dir`.

## Options

| Option                       |                                                                                                                                  |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `-n`, `--dry-run`            | Show the plan and change nothing.                                                                                                |
| `--move-dir`                 | Move the real project folder too, not just the records.                                                                          |
| `--rewrite-content`          | Also replace the old path where it is mentioned inside logged commands and captured output, not only in the fields Claude reads. |
| `--on-conflict merge\|clean` | If the destination already has history: `merge` the two, or `clean` (back it up and replace it). Defaults to stopping.           |
| `--heal`                     | Finish a half-done migration (see below).                                                                                        |
| `-y`, `--yes`                | Skip the confirmation prompt (required when there's no terminal).                                                                |

Both paths accept `~`, relative paths, and `..`. The old folder does not need to still exist.

## Re-running is safe

Run it again with the same arguments and it resumes from wherever it left off, so a two-step flow just works:

```bash
claude-mv ~/code/old ~/code/new              # records only
claude-mv ~/code/old ~/code/new --move-dir   # ...then move the folder
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
- Requires Python 3.12+.
- Tested on Linux and macOS. It is not tested on Windows yet, so treat Windows as unsupported for now.

## Contributing

Interested in contributing?
See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guideline.

[codecov-badge]: https://codecov.io/gh/tsvikas/claude-mv/graph/badge.svg
[codecov-link]: https://codecov.io/gh/tsvikas/claude-mv
[github-discussions-badge]: https://img.shields.io/static/v1?label=Discussions&message=Ask&color=blue&logo=github
[github-discussions-link]: https://github.com/tsvikas/claude-mv/discussions
[prs-welcome-badge]: https://img.shields.io/badge/PRs-welcome-brightgreen.svg
[prs-welcome-link]: https://opensource.guide/how-to-contribute/
[ruff-badge]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json
[ruff-link]: https://github.com/astral-sh/ruff
[template-badge]: https://img.shields.io/badge/%F0%9F%9A%80_Made_Using-tsvikas%2Fpython--template-gold
[template-link]: https://github.com/tsvikas/python-template
[tests-badge]: https://github.com/tsvikas/claude-mv/actions/workflows/ci.yml/badge.svg
[tests-link]: https://github.com/tsvikas/claude-mv/actions/workflows/ci.yml
[uv-badge]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json
[uv-link]: https://github.com/astral-sh/uv
