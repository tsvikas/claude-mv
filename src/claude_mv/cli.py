"""Command-line interface for claude-mv.

Presents the resolved :class:`~claude_mv.core.Plan`, applies the up-front safety gates,
and drives :func:`~claude_mv.core.execute`. All user-facing output lives here; the
engine in :mod:`claude_mv.core` stays print-free.
"""

import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal, NoReturn

from cyclopts import App, Parameter

from . import core

app = App(
    name="claude-mv",
    help="Move a project's Claude Code history when its directory is renamed.",
)
app.register_install_completion_command()

# How many nested sub-projects to name in a refusal message before "+N more".
_MAX_SHOWN_SUB_PROJECTS = 3


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def print_plan(plan: core.Plan) -> None:
    """Print the human-readable plan summary."""
    print("claude-mv plan")
    print(f"  old path : {plan.old_stored}")
    print(f"  new path : {plan.new_abs}")
    if plan.migrated:
        print(f"  projects/: already at {plan.enc_new} (migration done)")
    else:
        print(f"  projects/: {plan.enc_old}  ->  {plan.enc_new}")
    if plan.src_dir is None:
        print("  (no projects/ directory found at the old or new path)")
    else:
        unit = "path mention(s)" if plan.rewrite_content else "cwd reference(s)"
        print(
            f"  sessions : {plan.n_sessions} file(s),"
            f" {plan.sess_hits} {unit} to rewrite"
        )
    for name, _src, _dst in plan.extra_moves:
        print(f"  {name}/: {plan.enc_old}  ->  {plan.enc_new}")
    print(f"  history  : {plan.hist_hits} line(s) to rewrite in history.jsonl")
    print(f"  config   : {plan.cjson_hits} entry(ies) to remap in .claude.json")
    if plan.move_pending:
        print(f"  move dir : {plan.old_abs}  ->  {plan.new_abs}  (real directory)")
    elif plan.move_done:
        print(f"  move dir : already at {plan.new_abs}")
    elif plan.move_bad:
        print(
            "  move dir : cannot"
            f" ({'both exist' if plan.old_exists else 'neither exists'})"
        )
    if plan.mixed:
        print("  WARNING  : sessions mix old and new cwd (partial migration)")
    if plan.content_warn:
        print(
            "  WARNING  : --rewrite-content and the new path extends the old across a"
            " space/@/+; re-running would double-apply. Check the result, don't re-run."
        )


def check_refusals(
    plan: core.Plan, *, heal: bool, force: bool, on_conflict: str
) -> int | None:
    """Apply every up-front gate in order; return an exit code to stop, or None to go.

    Order is load-bearing: move-dir state, partial migration, config collision, the
    nothing-to-do shortcut, then the destination conflict (whose banner prints here).
    """
    if plan.move_bad:
        where = (
            "both the old and new directories exist"
            if plan.old_exists
            else "neither the old nor the new directory exists"
        )
        print(f"Refusing --move-dir: {where}; resolve it by hand first.")
        return 3

    if plan.sub_projects:
        shown = ", ".join(plan.sub_projects[:_MAX_SHOWN_SUB_PROJECTS])
        if len(plan.sub_projects) > _MAX_SHOWN_SUB_PROJECTS:
            shown += f", (+{len(plan.sub_projects) - _MAX_SHOWN_SUB_PROJECTS} more)"
        example = plan.sub_projects[0]
        target = core.remap(example, plan.old_stored, plan.new_abs)
        print(
            f"Refusing: {len(plan.sub_projects)} separate project(s) live under"
            f" {plan.old_stored}: {shown}."
            f" Move each first, e.g. claude-mv {example} {target}"
        )
        return 3

    if plan.mixed and not heal:
        print(
            "Refusing: this project's sessions mix old and new cwd references, which"
            " looks like a partial migration. Re-run with --heal to finish it."
        )
        return 3

    if plan.cjson_collisions:
        joined = ", ".join(plan.cjson_collisions)
        print(
            f"Refusing: .claude.json already has an entry for {joined} that differs"
            " from the one being migrated. Remove one by hand, then re-run."
        )
        return 3

    if not plan.any_work:
        print("Already migrated; nothing to do." if plan.migrated else "Nothing to do.")
        return 0

    if plan.conflict:
        print(
            f"  CONFLICT : destination {plan.enc_new} already exists"
            f" (belongs to {plan.conflict_cwd or 'unknown'})"
        )
        if not plan.conflict_related and not force:
            print(
                "Refusing: the existing destination history belongs to a different"
                " project (encoding collision)."
                " Re-run with --force only if you are sure."
            )
            return 3
        if on_conflict == "abort":
            print(
                "Destination exists. Re-run with --on-conflict merge|clean to proceed."
            )
            return 3

    return None


# --- Commands -------------------------------------------------------------------------
# This is the part to replace. `@app.default()` runs when no subcommand is
# given, so switch these to `@app.command()` once there is more than one, and
# keep the exit codes each returns listed in its docstring.
@app.default()
def claude_mv(
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
    claude_dir: Path = core.DEFAULT_CLAUDE_DIR,
    claude_json: Path = core.DEFAULT_CLAUDE_JSON,
) -> int:
    """Repoint Claude Code's bookkeeping from an old project path to a new one.

    Args:
        old: The project's old absolute path (before the rename/move). `~`,
            relative paths, and `..` are resolved. The directory need not still
            exist.
        new: The project's new absolute path (after the rename/move).
        on_conflict: What to do if the destination already has Claude history:
            `abort` (default), `merge` old sessions into it, or `clean` (back up
            and replace it). `merge` and `clean` refuse unless the existing
            history actually belongs to this project.
        move_dir: Also move the real project directory from `old` to `new`
            (default: leave the filesystem alone and only fix `~/.claude`).
        rewrite_content: Also replace incidental path mentions inside session
            files and history.jsonl (logged shell commands, captured output), not
            just the `cwd`/`project` pointer fields. Off by default, since that
            text is a record of what actually happened.
        heal: Proceed even when the project is in a partial-migration state (its
            sessions mix old and new `cwd` references), finishing the move.
            Without it such a state is refused rather than guessed at.
        dry_run: Show what would change and touch nothing.
        yes: Skip the confirmation prompt. Required to proceed in a
            non-interactive shell.
        force: Override the safety check that the destination history belongs to
            this project.
        claude_dir: Location of the Claude data directory (default: `~/.claude`).
            For testing.
        claude_json: Location of Claude's per-project config file (default:
            `~/.claude.json`). For testing.

    Returns:
        The process exit code.

    Exit Codes:
        0: Success, including a dry run and nothing to do.
        1: The move was not confirmed.
        2: Invalid usage, including identical or overlapping paths.
        3: The move was refused by a safety check.
        64-78: Reserved, an internal failure.
        129-159: Reserved, terminated by signal N, as 128 + N.
    """
    old_abs = core.to_abs(old)
    new_abs = core.to_abs(new)
    if old_abs == new_abs:
        print("Old and new paths resolve to the same location; nothing to do.")
        return 2
    # Nested paths break the prefix remap: the result stays under `old`, so the rewrite
    # is neither reversible nor idempotent (a re-run would append again). Refuse.
    if core.is_under(new_abs, old_abs) or core.is_under(old_abs, new_abs):
        print("Refusing: the old and new paths overlap (one is inside the other).")
        return 2

    plan = core.resolve_plan(
        old_abs,
        new_abs,
        claude_dir,
        claude_json,
        move_dir=move_dir,
        rewrite_content=rewrite_content,
    )
    print_plan(plan)

    code = check_refusals(plan, heal=heal, force=force, on_conflict=on_conflict)
    if code is not None:
        return code

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

    try:
        warnings = core.execute(plan, on_conflict=on_conflict, report=print)
    except BaseException as exc:
        # The engine already rolled back before re-raising; report and stop.
        print(f"Error: {exc}", file=sys.stderr)
        print("Rolled back to the pre-move state.", file=sys.stderr)
        raise
    print("Done.")
    for w in warnings:
        print(f"  note: {w}")
    return 0


# --- Entry point ----------------------------------------------------------------------
# Maps the commands above onto exit codes, and is what `[project.scripts]` and
# `__main__` both call.

# Cyclopts itself exits 2 on invalid usage. These are sysexits(3) codes.
# `os.EX_*` holds the same values but only exists on Unix, so they are inlined
# to keep the CLI importable on Windows.
EX_NOINPUT = 66
EX_UNAVAILABLE = 69
EX_SOFTWARE = 70
EX_NOPERM = 77


def _fail(exc: Exception, code: int) -> NoReturn:
    """Report `exc` on stderr and exit with `code`."""
    print(f"error: {exc}", file=sys.stderr)
    sys.exit(code)


def main(tokens: Sequence[str] | None = None) -> None:
    """Run the CLI, reporting failures and mapping them onto exit codes.

    Args:
        tokens: The command line to parse. Defaults to `sys.argv[1:]`.
    """
    try:
        # `tokens` is a parameter so that tests can pass a command line here.
        # Under pytest, a bare `app()` warns, since it would parse pytest's own
        # argv, and a test that does so passes while testing nothing.
        app(tokens)
    # Nothing reports the errors below, so without `_fail` the CLI would exit on
    # a bare code and no output. Match on the exception rather than on
    # `type(exc)`, so that subclasses such as ConnectionRefusedError still land
    # on the right code. Specific OSError subclasses must precede any bare
    # `except OSError`, which would otherwise swallow them.
    except FileNotFoundError as exc:
        _fail(exc, EX_NOINPUT)
    except PermissionError as exc:
        _fail(exc, EX_NOPERM)
    except ConnectionError as exc:
        _fail(exc, EX_UNAVAILABLE)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(EX_SOFTWARE)
