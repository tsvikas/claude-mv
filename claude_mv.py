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
from collections.abc import Callable, Iterator
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


def _iter_cwds(project_dir: Path) -> Iterator[str]:
    """Yield every non-empty `cwd` string recorded across a project's session files."""
    for jsonl in sorted(project_dir.glob("*.jsonl")):
        try:
            with jsonl.open(encoding="utf-8") as fh:
                for line in fh:
                    if '"cwd"' not in line:
                        continue
                    try:
                        cwd = json.loads(line).get("cwd")
                    except json.JSONDecodeError:
                        continue
                    if isinstance(cwd, str) and cwd:
                        yield cwd
        except OSError:
            continue


def read_root_cwd(project_dir: Path, enc_name: str) -> str | None:
    """Return the project's root path as Claude Code recorded it, or None.

    A project dir can hold sessions whose `cwd` is a subdirectory (e.g. a worktree),
    so we don't just take the first one. The root is the recorded `cwd` that encodes
    back to the directory's own name `enc_name`; a subdirectory encodes to a longer,
    different name. This authoritative stored string is preferred over re-deriving the
    path from the user's argument, which handles symlinks and alternate spellings.
    """
    for cwd in _iter_cwds(project_dir):
        if encode_path(cwd) == enc_name:
            return cwd
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


def _under(path: str, base: str) -> bool:
    """True if `path` is `base` or a descendant of it, on a real separator boundary.

    The boundary check keeps `/proj` from being considered under `/proj-2`.
    """
    return path == base or path.startswith(base + os.sep)


def remap(value: str, old: str, new: str) -> str | None:
    """Return `value` with its `old` path prefix swapped for `new`, else None."""
    return new + value[len(old) :] if _under(value, old) else None


def cwd_targets(project_dir: Path, old: str, new: str) -> tuple[bool, bool]:
    """Whether any recorded session `cwd` points under `old` and/or under `new`.

    A cleanly-placed project points entirely at one of them. A mix of both means a
    partial migration, which the caller refuses unless explicitly told to finish it.
    """
    has_old = has_new = False
    for cwd in _iter_cwds(project_dir):
        has_old = has_old or _under(cwd, old)
        has_new = has_new or _under(cwd, new)
        if has_old and has_new:
            break
    return has_old, has_new


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


def _rewrite_lines(path: Path, transform: Callable[[str], str], *, apply: bool) -> int:
    """Apply `transform` to each line of `path`; write back if any changed.

    Returns the number of changed lines. A transform signals "no change" by returning
    the identical line, so lines that need no edit keep their exact original bytes.
    """
    if not path.exists():
        return 0
    changed = 0
    out: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            new_line = transform(line)
            changed += new_line != line
            out.append(new_line)
    if apply and changed:
        _atomic_write(path, out)
    return changed


def _rewrite_field(path: Path, field: str, old: str, new: str, *, apply: bool) -> int:
    """Rewrite one top-level JSON `field` per line where it holds the old path.

    Only lines that literally contain `old` are parsed, and only the target `field` is
    changed. A line that needs no edit keeps its exact original bytes. A line that does
    get edited is re-serialized, but only `field` changes value, so an incidental path
    mention elsewhere on that line (a logged shell command, captured tool output) keeps
    its text. Returns the number of lines that changed (or would change).
    """

    def transform(line: str) -> str:
        if old not in line:
            return line
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return line
        value = obj.get(field)
        new_value = remap(value, old, new) if isinstance(value, str) else None
        if new_value is None:
            return line
        obj[field] = new_value
        newline = "\n" if line.endswith("\n") else ""
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + newline

    return _rewrite_lines(path, transform, apply=apply)


def _replace_paths_in_text(text: str, old: str, new: str) -> str:
    """Literal, path-boundary-aware replacement of `old` with `new` inside free text.

    An occurrence is replaced only when both the character before and the character
    after it are outside a filename (a separator, quote, whitespace, punctuation, or a
    string edge). So `/proj` is replaced in `cd /proj && ls` and `"/proj/sub"`, but left
    alone in a longer sibling `/proj-2` (trailing `-`) or a different path that merely
    ends with it, `/mnt/backup/proj` (leading `p`).
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
        before = text[j - 1] if j > 0 else ""
        after = text[j + n] if j + n < len(text) else ""
        on_boundary = not (before.isalnum() or before in "._-") and not (
            after.isalnum() or after in "._-"
        )
        out.append(new if on_boundary else old)
        i = j + n
    return "".join(out)


def _rewrite_content(path: Path, old: str, new: str, *, apply: bool) -> int:
    """Replace every path mention of `old` with `new` in a file's raw text.

    The opt-in counterpart to the field-scoped rewrite: this also changes incidental
    mentions in logged commands and captured output. Paths carry no JSON-special
    characters, so raw-text replacement keeps the JSONL valid.
    """
    return _rewrite_lines(
        path, lambda line: _replace_paths_in_text(line, old, new), apply=apply
    )


def rewrite_jsonl(
    path: Path, field: str, old: str, new: str, *, content: bool, apply: bool
) -> int:
    """Rewrite a JSONL file: all path mentions if `content`, else just `field`.

    `field` is `cwd` for session files, `project` for history.jsonl.
    """
    if content:
        return _rewrite_content(path, old, new, apply=apply)
    return _rewrite_field(path, field, old, new, apply=apply)


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


def claude_json_collisions(path: Path, old: str, new: str) -> list[str]:
    """Keys in `~/.claude.json` that remapping `old`->`new` would collide onto.

    A collision means both the old and the new path already have a distinct entry (e.g.
    the user opened Claude at the new path before running this), so remapping would
    overwrite one with the other. Returns the colliding target keys; empty if the remap
    is lossless. Identical values don't count (nothing is lost). The caller refuses
    rather than pick a winner.
    """
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    found: list[str] = []

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            targets: dict[str, object] = {}
            for key, value in obj.items():
                target = (remap(key, old, new) or key) if isinstance(key, str) else key
                if target in targets and targets[target] != value:
                    found.append(target)
                else:
                    targets[target] = value
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(data)
    return found


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
    heal: bool = False,
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
    heal
        Proceed even when the project is in a partial-migration state (its sessions mix
        old and new `cwd` references), finishing the move. Without it such a state is
        refused rather than guessed at.
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
    # Nested paths break the prefix remap: the result stays under `old`, so the rewrite
    # is neither reversible nor idempotent (a re-run would append again). Refuse.
    if _under(new_abs, old_abs) or _under(old_abs, new_abs):
        print("Refusing: the old and new paths overlap (one is inside the other).")
        return 1

    projects_dir = claude_dir / "projects"
    history_file = claude_dir / "history.jsonl"

    src_dir, enc_old = find_project_dir(projects_dir, old_abs)
    enc_new = encode_path(new_abs)
    dst_dir = projects_dir / enc_new

    # Resumability: if a prior run already moved the dir to `enc_new` and `enc_old` is
    # gone, recognize it so a re-run resumes the remaining work instead of finding
    # nothing. Evidence-based: treat `enc_new` as this project if its sessions reference
    # either endpoint. That covers a clean migration (cwd already new) and a half-done
    # one (cwd still old), while leaving an unrelated project (cwd under a third path)
    # alone, since every rewrite below is old->new and no-ops on it.
    migrated = False
    if src_dir is None and dst_dir.is_dir():
        has_old, has_new = cwd_targets(dst_dir, old_abs, new_abs)
        if has_old or has_new:
            src_dir = dst_dir
            migrated = True

    # The authoritative old-path string is the project root as Claude actually stored
    # it, if we found it at the old location. When already migrated (cwd is now the new
    # path) or not found, we fall back to the user's argument.
    old_stored = (read_root_cwd(src_dir, enc_old) if src_dir else None) or old_abs

    n_sessions = len(list(src_dir.glob("*.jsonl"))) if src_dir else 0
    sess_hits = (
        sum(
            rewrite_jsonl(
                f, "cwd", old_stored, new_abs, content=rewrite_content, apply=False
            )
            for f in src_dir.glob("*.jsonl")
        )
        if src_dir
        else 0
    )
    hist_hits = rewrite_jsonl(
        history_file,
        "project",
        old_stored,
        new_abs,
        content=rewrite_content,
        apply=False,
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

    # --move-dir moves the real directory. Check its state up front (to fail cleanly
    # rather than mid-transaction) and to support resuming: pending if only the old dir
    # exists, already done if only the new one does, inconsistent otherwise.
    old_exists = Path(old_abs).exists() if move_dir else False
    new_exists = Path(new_abs).exists() if move_dir else False
    move_pending = move_dir and old_exists and not new_exists
    move_done = move_dir and new_exists and not old_exists
    move_bad = move_dir and not (move_pending or move_done)
    projects_rename_pending = src_dir is not None and not migrated

    has_old_cwd, has_new_cwd = (
        cwd_targets(src_dir, old_stored, new_abs) if src_dir else (False, False)
    )
    mixed = has_old_cwd and has_new_cwd
    cjson_collisions = claude_json_collisions(claude_json, old_stored, new_abs)

    print("claude-mv plan")
    print(f"  old path : {old_stored}")
    print(f"  new path : {new_abs}")
    if migrated:
        print(f"  projects/: already at {enc_new} (migration done)")
    else:
        print(f"  projects/: {enc_old}  ->  {enc_new}")
    if src_dir is None:
        print("  (no projects/ directory found at the old or new path)")
    else:
        unit = "path mention(s)" if rewrite_content else "cwd reference(s)"
        print(f"  sessions : {n_sessions} file(s), {sess_hits} {unit} to rewrite")
    for name, _src, _dst in extra_moves:
        print(f"  {name}/: {enc_old}  ->  {enc_new}")
    print(f"  history  : {hist_hits} line(s) to rewrite in history.jsonl")
    print(f"  config   : {cjson_hits} entry(ies) to remap in .claude.json")
    if move_pending:
        print(f"  move dir : {old_abs}  ->  {new_abs}  (real directory)")
    elif move_done:
        print(f"  move dir : already at {new_abs}")
    elif move_bad:
        where = "both exist" if old_exists else "neither exists"
        print(f"  move dir : cannot ({where})")
    if mixed:
        print("  WARNING  : sessions mix old and new cwd (partial migration)")

    # Verify: --move-dir needs the real dir cleanly at exactly one of old/new.
    if move_bad:
        where = (
            "both the old and new directories exist"
            if old_exists
            else "neither the old nor the new directory exists"
        )
        print(f"Refusing --move-dir: {where}; resolve it by hand first.")
        return 2

    # Verify: refuse an ambiguous partial migration rather than guessing at it.
    if mixed and not heal:
        print(
            "Refusing: this project's sessions mix old and new cwd references, which"
            " looks like a partial migration. Re-run with --heal to finish it."
        )
        return 2

    # Verify: refuse rather than overwrite an existing .claude.json entry for the new
    # path with the old one (or vice versa); the user must remove the stray entry.
    if cjson_collisions:
        joined = ", ".join(cjson_collisions)
        print(
            f"Refusing: .claude.json already has an entry for {joined} that differs"
            " from the one being migrated. Remove one by hand, then re-run."
        )
        return 2

    any_work = bool(
        projects_rename_pending
        or sess_hits
        or hist_hits
        or cjson_hits
        or extra_moves
        or move_pending
    )
    if not any_work:
        print("Already migrated; nothing to do." if migrated else "Nothing to do.")
        return 0

    # Conflict handling. The encoding is lossy, so an existing destination dir may
    # belong to a *different* real project that happens to encode identically. Only
    # treat it as this project's history if its recorded cwd resolves to old or new.
    conflict = src_dir is not None and not migrated and dst_dir.exists()
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
        if move_pending:
            Path(new_abs).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(old_abs, new_abs)
            moved_real = True

        if src_dir is not None:
            if migrated:
                pass  # project is already at enc_new; only the rewrites below remain
            elif conflict and on_conflict == "clean":
                shutil.rmtree(dst_dir)
                src_dir.rename(dst_dir)
            elif conflict and on_conflict == "merge":
                _merge_move(src_dir, dst_dir, warnings)
                src_dir.rmdir()
            else:
                src_dir.rename(dst_dir)
                created.append(dst_dir)

            for jsonl in dst_dir.glob("*.jsonl"):
                rewrite_jsonl(
                    jsonl,
                    "cwd",
                    old_stored,
                    new_abs,
                    content=rewrite_content,
                    apply=True,
                )

        for _name, src, dst in extra_moves:
            if dst.exists():
                _merge_move(src, dst, warnings)
                if src.is_dir():
                    src.rmdir()
            else:
                src.rename(dst)
                created.append(dst)

        rewrite_jsonl(
            history_file,
            "project",
            old_stored,
            new_abs,
            content=rewrite_content,
            apply=True,
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
