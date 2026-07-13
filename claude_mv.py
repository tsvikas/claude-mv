#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["cyclopts>=3"]
# ///
"""Move a project's Claude Code bookkeeping when its directory is renamed or moved.

Claude Code stores each project's sessions under `~/.claude/projects/<encoded-path>/`,
where the directory name is the project's absolute path with every non-alphanumeric
character replaced by `-`.
Rename the project on disk and that encoded name no longer matches, so Claude Code
starts a fresh, empty history and the old sessions look lost.

This tool repoints the bookkeeping at the new path:
it renames the `projects/<encoded>` directory, rewrites the `cwd` field inside the
session `*.jsonl` files, and rewrites the `project` field in `~/.claude/history.jsonl`.
By default it touches nothing outside `~/.claude`; pass `--move-dir` to also move the
real project directory.

Run with `uv run claude_mv.py OLD NEW`, or install cyclopts and run directly.
"""

import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from cyclopts import App, Parameter

DEFAULT_CLAUDE_DIR = Path.home() / ".claude"
DEFAULT_CLAUDE_JSON = Path.home() / ".claude.json"

# Directories under ~/.claude that are (or on some versions were) keyed by the encoded
# project path. On current Claude Code only `projects` is path-keyed and carries the
# session files; the rest are keyed by session id or content hash, so their `<encoded>`
# entry simply won't exist and is skipped. Renaming them anyway keeps the tool correct
# across versions that do key them by path (see the reference scripts).
PATH_KEYED_DIRS = ("projects", "todos", "file-history", "shell-snapshots", "debug")

app = App(
    name="claude-mv",
    help="Move a project's Claude Code history when its directory is renamed.",
)


# Claude Code's path encoding: every non-alphanumeric character becomes '-'.
# Verified against 62/62 local project dirs, including paths with '_', spaces, '@',
# and '+'. The mapping is lossy (many characters collapse to '-') and therefore NOT
# invertible: never try to recover a real path from an encoded directory name.
def encode_path(abs_path: str) -> str:
    """Encode an absolute path the way Claude Code names its `projects/` subdir."""
    return re.sub(r"[^a-zA-Z0-9]", "-", abs_path)


def to_abs(p: Path | str) -> str:
    """Expand `~` and normalize to an absolute path string, without touching disk.

    Symlinks are left unresolved so the result matches the path the user refers to
    (and, for the destination, the path they will `cd` into).
    """
    return os.path.abspath(Path(p).expanduser())


def read_root_cwd(project_dir: Path, enc_name: str) -> str | None:
    """Return the project's root path as Claude Code recorded it, or None.

    A project dir can hold sessions whose `cwd` is a subdirectory (e.g. a worktree),
    so we don't just take the first one. The root is the recorded `cwd` that encodes
    back to the directory's own name `enc_name`; a subdirectory encodes to a longer,
    different name. This authoritative stored string is preferred over re-deriving the
    path from the user's argument, which handles symlinks and alternate spellings.
    """
    for jsonl in sorted(project_dir.glob("*.jsonl")):
        try:
            with jsonl.open(encoding="utf-8") as fh:
                for line in fh:
                    if '"cwd"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = obj.get("cwd")
                    if isinstance(cwd, str) and cwd and encode_path(cwd) == enc_name:
                        return cwd
        except OSError:
            continue
    return None


def find_project_dir(projects_dir: Path, old_abs: str) -> tuple[Path | None, str]:
    """Locate `projects/<encoded>` for `old_abs`.

    Returns (dir_or_None, encoded_name). Falls back to the symlink-resolved path, then
    to scanning every project's recorded `cwd`, so a slightly different spelling of the
    old path (trailing slash, symlink, ..) still finds the right directory.
    """
    enc = encode_path(old_abs)
    direct = projects_dir / enc
    if direct.is_dir():
        return direct, enc

    real = os.path.realpath(old_abs)
    if real != old_abs:
        enc_real = encode_path(real)
        cand = projects_dir / enc_real
        if cand.is_dir():
            return cand, enc_real

    if projects_dir.is_dir():
        for sub in projects_dir.iterdir():
            if not sub.is_dir():
                continue
            cwd = read_root_cwd(sub, sub.name)
            if cwd and to_abs(cwd) == old_abs:
                return sub, sub.name
    return None, enc


def remap(value: str, old: str, new: str) -> str | None:
    """Return `value` with an `old` path prefix swapped for `new`, or None if unchanged.

    The `old + os.sep` boundary check keeps `/proj` from matching `/proj-2`.
    """
    if value == old:
        return new
    if value.startswith(old + os.sep):
        return new + value[len(old) :]
    return None


def _atomic_write(path: Path, lines: list[str]) -> None:
    """Replace `path` with `lines` via a temp file in the same dir, then os.replace."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _rewrite_field(path: Path, field: str, old: str, new: str, *, apply: bool) -> int:
    """Rewrite one top-level JSON `field` per line where it holds the old path.

    Only lines that literally contain `old` are parsed, and only the target `field` is
    changed. A line that needs no edit keeps its exact original bytes. A line that does
    get edited is re-serialized, but only `field` changes value, so an incidental path
    mention elsewhere on that line (a logged shell command, captured tool output) keeps
    its text. Returns the number of lines that changed (or would change).
    """
    if not path.exists():
        return 0
    changed = 0
    out: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if old not in line:
                out.append(line)
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                out.append(line)
                continue
            value = obj.get(field)
            new_value = remap(value, old, new) if isinstance(value, str) else None
            if new_value is None:
                out.append(line)
                continue
            obj[field] = new_value
            changed += 1
            newline = "\n" if line.endswith("\n") else ""
            dumped = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            out.append(dumped + newline)
    if apply and changed:
        _atomic_write(path, out)
    return changed


def _replace_paths_in_text(text: str, old: str, new: str) -> str:
    """Literal, path-boundary-aware replacement of `old` with `new` inside free text.

    An occurrence is replaced only when the character right after it cannot continue a
    filename (a separator, a quote, whitespace, punctuation, or end of string). So the
    `old` path inside a longer sibling like `/proj-2` (next char `-`) is left alone,
    while `/proj` in `cd /proj && ls`, `"/proj/sub"`, or at end of line is replaced.
    """
    if old not in text:
        return text
    out: list[str] = []
    i = 0
    n = len(old)
    while True:
        j = text.find(old, i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        after = text[j + n] if j + n < len(text) else ""
        out.append(new if not (after.isalnum() or after in "._-") else old)
        i = j + n
    return "".join(out)


def _rewrite_content(path: Path, old: str, new: str, *, apply: bool) -> int:
    """Replace every path mention of `old` with `new` in a file's raw text.

    The opt-in counterpart to the field-scoped rewrite: this also changes incidental
    mentions in logged commands and captured output. Returns the number of lines that
    changed. Paths carry no JSON-special characters, so raw-text replacement keeps the
    JSONL valid.
    """
    if not path.exists():
        return 0
    changed = 0
    out: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            new_line = _replace_paths_in_text(line, old, new)
            if new_line != line:
                changed += 1
            out.append(new_line)
    if apply and changed:
        _atomic_write(path, out)
    return changed


def rewrite_session(
    path: Path, old: str, new: str, *, content: bool, apply: bool
) -> int:
    """Rewrite a session file: all path mentions if `content`, else just `cwd`."""
    if content:
        return _rewrite_content(path, old, new, apply=apply)
    return _rewrite_field(path, "cwd", old, new, apply=apply)


def rewrite_history(
    path: Path, old: str, new: str, *, content: bool, apply: bool
) -> int:
    """Rewrite history.jsonl: all path mentions if `content`, else just `project`."""
    if content:
        return _rewrite_content(path, old, new, apply=apply)
    return _rewrite_field(path, "project", old, new, apply=apply)


def _remap_json(obj: object, old: str, new: str) -> tuple[object, int]:
    """Recursively remap every dict key and string value that holds the old path.

    Returns (new_object, number_of_remaps). Used for `~/.claude.json`, whose every
    project-path occurrence is a location pointer (a `projects` key, a `githubRepoPaths`
    entry), never incidental prose, so a structural remap is both safe and complete.
    """
    if isinstance(obj, str):
        remapped = remap(obj, old, new)
        return (remapped, 1) if remapped is not None else (obj, 0)
    if isinstance(obj, dict):
        out: dict[object, object] = {}
        count = 0
        for key, value in obj.items():
            new_key = remap(key, old, new) if isinstance(key, str) else None
            if new_key is not None:
                key = new_key
                count += 1
            new_value, sub = _remap_json(value, old, new)
            count += sub
            out[key] = new_value
        return out, count
    if isinstance(obj, list):
        out_list: list[object] = []
        count = 0
        for value in obj:
            new_value, sub = _remap_json(value, old, new)
            count += sub
            out_list.append(new_value)
        return out_list, count
    return obj, 0


def _rewrite_claude_json(path: Path, old: str, new: str, *, apply: bool) -> int:
    """Remap project-path keys and values in `~/.claude.json` (per-project config).

    This holds `allowedTools`, MCP servers, trust acceptance, and stats keyed by the
    project's absolute path, so a rename orphans it unless remapped. Returns the number
    of remaps. Only the matching keys/values change; the file's 2-space formatting is
    preserved so the diff stays minimal.
    """
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    new_data, count = _remap_json(data, old, new)
    if apply and count:
        _atomic_write(path, [json.dumps(new_data, ensure_ascii=False, indent=2) + "\n"])
    return count


def _merge_move(src: Path, dst: Path, warnings: list[str]) -> None:
    """Move everything from `src` into `dst`, recursing into shared subdirectories.

    Session files are UUID-named and never collide. On a genuine file collision (e.g.
    two `memory/` entries) the incoming file is kept under a suffixed name rather than
    silently overwriting or dropping either side.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            if target.is_dir():
                _merge_move(item, target, warnings)
                item.rmdir()
            else:
                shutil.move(str(item), str(target))
        elif target.exists():
            kept = dst / f"{item.name}.merged-from-source"
            shutil.move(str(item), str(kept))
            warnings.append(f"collision: kept both, incoming saved as {kept.name}")
        else:
            shutil.move(str(item), str(target))


def _backup(items: list[Path], backup_root: Path) -> list[tuple[Path, Path]]:
    """Copy each existing item into `backup_root`; return (original, saved) pairs.

    Items are saved under an index-prefixed name so that same-named sources (every
    `<encoded>` dir across `projects/`, `todos/`, ... shares a name) never collide.
    """
    backup_root.mkdir(parents=True, exist_ok=True)
    pairs: list[tuple[Path, Path]] = []
    for i, item in enumerate(items):
        if not item.exists():
            continue
        saved = backup_root / f"{i:02d}-{item.name}"
        if item.is_dir():
            shutil.copytree(item, saved)
        else:
            shutil.copy2(item, saved)
        pairs.append((item, saved))
    return pairs


def _restore_pair(original: Path, saved: Path) -> None:
    """Replace whatever is now at `original` with the backed-up copy `saved`."""
    if original.is_dir():
        shutil.rmtree(original, ignore_errors=True)
    elif original.exists():
        original.unlink()
    if saved.is_dir():
        shutil.copytree(saved, original)
    else:
        shutil.copy2(saved, original)


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


@app.default
def main(
    old: str,
    new: str,
    *,
    on_conflict: Literal["abort", "merge", "clean"] = "abort",
    move_dir: bool = False,
    rewrite_content: bool = False,
    dry_run: Annotated[bool, Parameter(alias="-n")] = False,
    yes: Annotated[bool, Parameter(alias="-y")] = False,
    force: bool = False,
    claude_dir: Path = DEFAULT_CLAUDE_DIR,
    claude_json: Path = DEFAULT_CLAUDE_JSON,
) -> int:
    """Repoint Claude Code's bookkeeping from an old project path to a new one.

    Parameters
    ----------
    old
        The project's old absolute path (before the rename/move). `~`, relative paths,
        and `..` are resolved. The directory need not still exist.
    new
        The project's new absolute path (after the rename/move).
    on_conflict
        What to do if the destination already has Claude history: `abort` (default),
        `merge` old sessions into it, or `clean` (back up and replace it). `merge` and
        `clean` refuse unless the existing history actually belongs to this project.
    move_dir
        Also move the real project directory from `old` to `new` (default: leave the
        filesystem alone and only fix `~/.claude`).
    rewrite_content
        Also replace incidental path mentions inside session files and history.jsonl
        (logged shell commands, captured output), not just the `cwd`/`project` pointer
        fields. Off by default, since that text is a record of what actually happened.
    dry_run
        Show what would change and touch nothing.
    yes
        Skip the confirmation prompt. Required to proceed in a non-interactive shell.
    force
        Override the safety check that the destination history belongs to this project.
    claude_dir
        Location of the Claude data directory (default: `~/.claude`). For testing.
    claude_json
        Location of Claude's per-project config file (default: `~/.claude.json`).
        For testing.
    """
    old_abs = to_abs(old)
    new_abs = to_abs(new)
    if old_abs == new_abs:
        print("Old and new paths resolve to the same location; nothing to do.")
        return 1

    projects_dir = claude_dir / "projects"
    history_file = claude_dir / "history.jsonl"

    src_dir, enc_old = find_project_dir(projects_dir, old_abs)
    enc_new = encode_path(new_abs)
    dst_dir = projects_dir / enc_new

    # The authoritative old-path string is the project root as Claude actually stored
    # it, if we found it. Falling back to the user's argument covers the rare dir with
    # no root-level cwd on record.
    old_stored = (read_root_cwd(src_dir, enc_old) if src_dir else None) or old_abs

    n_sessions = len(list(src_dir.glob("*.jsonl"))) if src_dir else 0
    sess_hits = (
        sum(
            rewrite_session(
                f, old_stored, new_abs, content=rewrite_content, apply=False
            )
            for f in src_dir.glob("*.jsonl")
        )
        if src_dir
        else 0
    )
    hist_hits = rewrite_history(
        history_file, old_stored, new_abs, content=rewrite_content, apply=False
    )
    cjson_hits = _rewrite_claude_json(claude_json, old_stored, new_abs, apply=False)

    # Sibling dirs that some Claude Code versions key by the encoded path. Present only
    # if such a version created them; on current versions they are session-keyed, so the
    # `<encoded>` entry does not exist and the list is empty.
    extra_moves = [
        (name, claude_dir / name / enc_old, claude_dir / name / enc_new)
        for name in PATH_KEYED_DIRS
        if name != "projects" and (claude_dir / name / enc_old).exists()
    ]

    print("claude-mv plan")
    print(f"  old path : {old_stored}")
    print(f"  new path : {new_abs}")
    print(f"  projects/: {enc_old}  ->  {enc_new}")
    if src_dir is None:
        print("  (no projects/ directory found for the old path)")
    else:
        unit = "path mention(s)" if rewrite_content else "cwd reference(s)"
        print(f"  sessions : {n_sessions} file(s), {sess_hits} {unit} to rewrite")
    for name, _src, _dst in extra_moves:
        print(f"  {name}/: {enc_old}  ->  {enc_new}")
    print(f"  history  : {hist_hits} line(s) to rewrite in history.jsonl")
    print(f"  config   : {cjson_hits} entry(ies) to remap in .claude.json")
    if move_dir:
        print(f"  move dir : {old_abs}  ->  {new_abs}  (real directory)")

    if src_dir is None and hist_hits == 0 and cjson_hits == 0 and not extra_moves:
        print("Nothing to do.")
        return 0

    # Conflict handling. The encoding is lossy, so an existing destination dir may
    # belong to a *different* real project that happens to encode identically. Only
    # treat it as this project's history if its recorded cwd resolves to old or new.
    conflict = src_dir is not None and dst_dir.exists()
    if conflict:
        dst_cwd = read_root_cwd(dst_dir, enc_new)
        dst_resolved = to_abs(dst_cwd) if dst_cwd else None
        related = dst_resolved in {new_abs, old_abs, to_abs(old_stored)}
        print(
            f"  CONFLICT : destination {enc_new} already exists"
            f" (belongs to {dst_cwd or 'unknown'})"
        )
        if not related and not force:
            print(
                "Refusing: the existing destination history belongs to a different"
                " project (encoding collision)."
                " Re-run with --force only if you are sure."
            )
            return 2
        if on_conflict == "abort":
            print(
                "Destination exists. Re-run with --on-conflict merge|clean to proceed."
            )
            return 2

    if dry_run:
        print("Dry run: no changes made.")
        return 0

    if not yes:
        if not sys.stdin.isatty():
            print("Refusing to proceed without --yes in a non-interactive shell.")
            return 1
        if not _confirm("Proceed? [y/N] "):
            print("Aborted.")
            return 1

    # Back up everything we may modify or delete: the source dirs, any merge
    # destinations (restored wholesale on failure), history, and the config file.
    backup_items: list[Path] = [src_dir] if src_dir else []
    if conflict:
        backup_items.append(dst_dir)
    backup_items += [s for _n, s, _d in extra_moves]
    backup_items += [d for _n, _s, d in extra_moves if d.exists()]
    backup_items.append(history_file)
    if cjson_hits:
        backup_items.append(claude_json)

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    backup_root = claude_dir / "claude-mv-backups" / f"{stamp}-{enc_old}"
    backup_pairs = _backup(backup_items, backup_root)
    print(f"  backup   : {backup_root}")

    warnings: list[str] = []
    created: list[Path] = []  # paths a rename created; delete these on rollback
    moved_real = False
    try:
        if move_dir:
            if not Path(old_abs).exists():
                raise FileNotFoundError(
                    f"--move-dir: source directory not found: {old_abs}"
                )
            if Path(new_abs).exists():
                raise FileExistsError(
                    f"--move-dir: destination already exists: {new_abs}"
                )
            Path(new_abs).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(old_abs, new_abs)
            moved_real = True

        if src_dir is not None:
            if conflict and on_conflict == "clean":
                shutil.rmtree(dst_dir)
                src_dir.rename(dst_dir)
            elif conflict and on_conflict == "merge":
                _merge_move(src_dir, dst_dir, warnings)
                src_dir.rmdir()
            else:
                src_dir.rename(dst_dir)
                created.append(dst_dir)

            for jsonl in dst_dir.glob("*.jsonl"):
                rewrite_session(
                    jsonl, old_stored, new_abs, content=rewrite_content, apply=True
                )

        for _name, src, dst in extra_moves:
            if dst.exists():
                _merge_move(src, dst, warnings)
                if src.is_dir():
                    src.rmdir()
            else:
                src.rename(dst)
                created.append(dst)

        rewrite_history(
            history_file, old_stored, new_abs, content=rewrite_content, apply=True
        )
        _rewrite_claude_json(claude_json, old_stored, new_abs, apply=True)
    except BaseException as exc:
        print(f"Error: {exc}\nRolling back...", file=sys.stderr)
        if moved_real and Path(new_abs).exists() and not Path(old_abs).exists():
            shutil.move(new_abs, old_abs)
        for path in reversed(created):
            if not path.exists():
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        for original, saved in backup_pairs:
            _restore_pair(original, saved)
        print("Rolled back to the pre-move state.", file=sys.stderr)
        raise

    print("Done.")
    for w in warnings:
        print(f"  note: {w}")
    return 0


if __name__ == "__main__":
    app()
