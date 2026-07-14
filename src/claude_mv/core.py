"""Engine for moving a Claude Code project's bookkeeping when its directory moves.

Claude Code stores each project's sessions under `~/.claude/projects/<encoded-path>/`,
where the directory name is the project's absolute path with every non-alphanumeric
character replaced by `-`.
Rename the project on disk and that encoded name no longer matches, so Claude Code
starts a fresh, empty history and the old sessions look lost.

This module repoints the bookkeeping at the new path:
it renames the `projects/<encoded>` directory, rewrites the `cwd` field inside the
session `*.jsonl` files, and rewrites the `project` field in `~/.claude/history.jsonl`.
`resolve_plan` reads the current state without touching disk; `execute` performs the
move and rolls back fully on any failure.
Nothing outside `~/.claude` is touched unless the caller sets `move_dir`.
"""

import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_CLAUDE_DIR = Path.home() / ".claude"
DEFAULT_CLAUDE_JSON = Path.home() / ".claude.json"

# Directories under ~/.claude that are (or on some versions were) keyed by the encoded
# project path. On current Claude Code only `projects` is path-keyed and carries the
# session files; the rest are keyed by session id or content hash, so their `<encoded>`
# entry simply won't exist and is skipped. Renaming them anyway keeps the tool correct
# across versions that do key them by path (see the reference scripts).
PATH_KEYED_DIRS = ("projects", "todos", "file-history", "shell-snapshots", "debug")


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
    # Not Path.resolve(): that would resolve symlinks, which this deliberately avoids.
    return os.path.abspath(Path(p).expanduser())  # noqa: PTH100


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


def sub_projects_under(projects_dir: Path, old: str, src_dir: Path | None) -> list[str]:
    """Root paths of *other* projects that sit strictly under `old`.

    If `/a` is being moved and `/a/c` is its own Claude project (a separate `projects/`
    dir, e.g. a worktree), moving `/a` alone would leave `/a/c` half-migrated. The
    caller refuses when this returns anything, so those are moved deliberately instead.
    """
    found: list[str] = []
    if projects_dir.is_dir():
        for sub in sorted(projects_dir.iterdir()):
            if sub == src_dir or not sub.is_dir():
                continue
            cwd = read_root_cwd(sub, sub.name)
            if cwd:
                root = to_abs(cwd)
                if root != old and is_under(root, old):
                    found.append(root)
    return found


def is_under(path: str, base: str) -> bool:
    """Return whether `path` is `base` or a descendant, on a real separator boundary.

    The boundary check keeps `/proj` from being considered under `/proj-2`.
    """
    return path == base or path.startswith(base + os.sep)


def remap(value: str, old: str, new: str) -> str | None:
    """Return `value` with its `old` path prefix swapped for `new`, else None."""
    return new + value[len(old) :] if is_under(value, old) else None


def cwd_targets(project_dir: Path, old: str, new: str) -> tuple[bool, bool]:
    """Whether any recorded session `cwd` points under `old` and/or under `new`.

    A cleanly-placed project points entirely at one of them. A mix of both means a
    partial migration, which the caller refuses unless explicitly told to finish it.
    """
    has_old = has_new = False
    for cwd in _iter_cwds(project_dir):
        has_old = has_old or is_under(cwd, old)
        has_new = has_new or is_under(cwd, new)
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

    Best-effort, and only behind --rewrite-content: raw text can't disambiguate every
    case, so there are two known edges. (1) It is not idempotent when the new path
    extends the old across a boundary char, e.g. `/proj` -> `/proj v2`: re-running turns
    `/proj v2` into `/proj v2 v2`. (2) A sibling whose boundary char isn't `-`, `_`, or
    `.` (say `/proj@bak`) is treated as a distinct path and rewritten. Only the `-` `_`
    `.` and `/` renames are handled cleanly. The default pointer rewrites avoid all of
    this by matching whole field values via `is_under`, so a normal run never touches
    it.
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
        # A string edge ("") is a boundary; a filename char (alnum or ._-) is not.
        on_boundary = not (before and (before.isalnum() or before in "._-")) and not (
            after and (after.isalnum() or after in "._-")
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
                key = new_key  # noqa: PLW2901
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
                # JSON object keys are always strings; the guard also narrows the type.
                if isinstance(key, str):
                    target = remap(key, old, new) or key
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


@dataclass
class Plan:
    """Everything resolved about one move: where things are and what work remains.

    Stores only the facts read from disk; everything derivable from them is a property.
    """

    claude_dir: Path
    claude_json: Path
    old_abs: str
    new_abs: str
    old_stored: str  # the old path as Claude stored it (authoritative match string)
    enc_old: str
    enc_new: str
    src_dir: (
        Path | None
    )  # the project dir to operate on (enc_old, or enc_new if migrated)
    migrated: bool  # already at enc_new from a prior run
    rewrite_content: bool
    n_sessions: int
    sess_hits: int
    hist_hits: int
    cjson_hits: int
    extra_moves: list[tuple[str, Path, Path]]  # (sibling dir name, src, dst)
    move_dir: bool
    old_exists: bool  # real dirs, only meaningful with move_dir
    new_exists: bool
    mixed: bool  # sessions reference both old and new (partial migration)
    cjson_collisions: list[str]
    sub_projects: list[str]  # separate projects nested under old (moving would half-do)
    content_warn: bool  # --rewrite-content where new extends old across a boundary char
    conflict: bool  # a *different* project already sits at enc_new
    conflict_cwd: str | None
    conflict_related: bool

    @property
    def history_file(self) -> Path:
        """Path to `~/.claude/history.jsonl`."""
        return self.claude_dir / "history.jsonl"

    @property
    def dst_dir(self) -> Path:
        """Destination `projects/<enc_new>` directory."""
        return self.claude_dir / "projects" / self.enc_new

    @property
    def move_pending(self) -> bool:
        """Whether the real dir still needs moving (old exists, new doesn't)."""
        return self.move_dir and self.old_exists and not self.new_exists

    @property
    def move_done(self) -> bool:
        """Whether the real dir was already moved (new exists, old is gone)."""
        return self.move_dir and self.new_exists and not self.old_exists

    @property
    def move_bad(self) -> bool:
        """Whether `--move-dir` was asked for but the dir state is neither of those."""
        return self.move_dir and not (self.move_pending or self.move_done)

    @property
    def projects_rename_pending(self) -> bool:
        """Whether the `projects/` dir still needs renaming to `enc_new`."""
        return self.src_dir is not None and not self.migrated

    @property
    def any_work(self) -> bool:
        """Whether any migration step still remains to do."""
        return bool(
            self.projects_rename_pending
            or self.sess_hits
            or self.hist_hits
            or self.cjson_hits
            or self.extra_moves
            or self.move_pending
        )


def resolve_plan(
    old_abs: str,
    new_abs: str,
    claude_dir: Path,
    claude_json: Path,
    *,
    move_dir: bool,
    rewrite_content: bool,
) -> Plan:
    """Compute the full state of the move. Pure: reads disk, writes/prints nothing."""
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

    # The authoritative old-path string is the project root as Claude stored it, when
    # found at the old location; otherwise (migrated, or not found) the user's argument.
    old_stored = (read_root_cwd(src_dir, enc_old) if src_dir else None) or old_abs

    session_files = sorted(src_dir.glob("*.jsonl")) if src_dir else []
    sess_hits = sum(
        rewrite_jsonl(
            f, "cwd", old_stored, new_abs, content=rewrite_content, apply=False
        )
        for f in session_files
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

    # --move-dir checks the real dir up front (so we fail cleanly, not mid-transaction).
    # old_exists/new_exists are meaningful only when move_dir; Plan derives the
    # pending/done/bad states from them.
    old_exists = Path(old_abs).exists() if move_dir else False
    new_exists = Path(new_abs).exists() if move_dir else False

    has_old_cwd, has_new_cwd = (
        cwd_targets(src_dir, old_stored, new_abs) if src_dir else (False, False)
    )
    sub_projects = sub_projects_under(projects_dir, old_stored, src_dir)
    # --rewrite-content isn't idempotent when the new path extends the old across a
    # boundary char (space, @, +, ...); flag it so the user can check the result.
    tail = new_abs[len(old_stored) :] if new_abs.startswith(old_stored) else ""
    content_warn = bool(
        rewrite_content and tail and not (tail[0].isalnum() or tail[0] in "._-")
    )

    # A *different* project already sitting at enc_new. The encoding is lossy, so trust
    # it as this project's history only if its recorded cwd resolves to old or new.
    conflict = src_dir is not None and not migrated and dst_dir.exists()
    conflict_cwd = read_root_cwd(dst_dir, enc_new) if conflict else None
    conflict_related = bool(
        conflict_cwd and to_abs(conflict_cwd) in {new_abs, old_abs, to_abs(old_stored)}
    )

    return Plan(
        claude_dir=claude_dir,
        claude_json=claude_json,
        old_abs=old_abs,
        new_abs=new_abs,
        old_stored=old_stored,
        enc_old=enc_old,
        enc_new=enc_new,
        src_dir=src_dir,
        migrated=migrated,
        rewrite_content=rewrite_content,
        n_sessions=len(session_files),
        sess_hits=sess_hits,
        hist_hits=hist_hits,
        cjson_hits=cjson_hits,
        extra_moves=extra_moves,
        move_dir=move_dir,
        old_exists=old_exists,
        new_exists=new_exists,
        mixed=has_old_cwd and has_new_cwd,
        cjson_collisions=claude_json_collisions(claude_json, old_stored, new_abs),
        sub_projects=sub_projects,
        content_warn=content_warn,
        conflict=conflict,
        conflict_cwd=conflict_cwd,
        conflict_related=conflict_related,
    )


def _rollback(
    *,
    moved_real: bool,
    old_abs: str,
    new_abs: str,
    created: list[Path],
    backup_pairs: list[tuple[Path, Path]],
) -> None:
    """Undo a partial execute.

    Move the real dir back, delete rename-created dirs, then restore every backed-up
    item to its pre-run bytes.
    """
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


def _backup_items(plan: Plan) -> list[Path]:
    """Everything execute may modify or delete.

    The source dir, any merge destinations (restored wholesale on failure), history, and
    the config file. Non-existent entries are dropped by `_backup`.
    """
    items: list[Path] = [plan.src_dir] if plan.src_dir else []
    if plan.conflict:
        items.append(plan.dst_dir)
    for _name, src, dst in plan.extra_moves:
        items += [src, dst]
    items.append(plan.history_file)
    if plan.cjson_hits:
        items.append(plan.claude_json)
    return items


def execute(
    plan: Plan, *, on_conflict: str, report: Callable[[str], None] | None = None
) -> list[str]:
    """Back up, perform the move + rewrites, and roll back fully on any failure.

    Args:
        plan: The resolved move, from `resolve_plan`.
        on_conflict: `abort`, `merge`, or `clean` when the destination already exists.
        report: Optional sink for progress lines (e.g. the backup location), called
            before the risky work so the location is known even if the process is
            killed. The engine itself never writes to stdout.

    Returns:
        Collected warnings; re-raises the original error after rolling back.
    """
    backup_items = _backup_items(plan)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    backup_root = plan.claude_dir / "claude-mv-backups" / f"{stamp}-{plan.enc_old}"
    backup_pairs = _backup(backup_items, backup_root)
    if report is not None:
        report(f"  backup   : {backup_root}")

    warnings: list[str] = []
    created: list[Path] = []  # paths a rename created; delete these on rollback
    moved_real = False
    try:
        if plan.move_pending:
            Path(plan.new_abs).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(plan.old_abs, plan.new_abs)
            moved_real = True

        if plan.src_dir is not None:
            if plan.migrated:
                pass  # already at enc_new; only the rewrites below remain
            elif plan.conflict and on_conflict == "clean":
                shutil.rmtree(plan.dst_dir)
                plan.src_dir.rename(plan.dst_dir)
            elif plan.conflict and on_conflict == "merge":
                _merge_move(plan.src_dir, plan.dst_dir, warnings)
                plan.src_dir.rmdir()
            else:
                plan.src_dir.rename(plan.dst_dir)
                created.append(plan.dst_dir)

            for jsonl in plan.dst_dir.glob("*.jsonl"):
                rewrite_jsonl(
                    jsonl,
                    "cwd",
                    plan.old_stored,
                    plan.new_abs,
                    content=plan.rewrite_content,
                    apply=True,
                )

        for _name, src, dst in plan.extra_moves:
            if dst.exists():
                _merge_move(src, dst, warnings)
                if src.is_dir():
                    src.rmdir()
            else:
                src.rename(dst)
                created.append(dst)

        rewrite_jsonl(
            plan.history_file,
            "project",
            plan.old_stored,
            plan.new_abs,
            content=plan.rewrite_content,
            apply=True,
        )
        _rewrite_claude_json(
            plan.claude_json, plan.old_stored, plan.new_abs, apply=True
        )
    except BaseException:
        _rollback(
            moved_real=moved_real,
            old_abs=plan.old_abs,
            new_abs=plan.new_abs,
            created=created,
            backup_pairs=backup_pairs,
        )
        raise
    return warnings
