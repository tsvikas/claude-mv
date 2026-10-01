"""Behavior tests for the claude-mv engine and CLI.

They drive `cli.claude_mv()` directly against a throwaway `~/.claude`-shaped tree built
under pytest's `tmp_path`, and assert on the exit code plus the resulting on-disk state.
"""

import contextlib
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

import pytest

from claude_mv import cli, core

# Fixture records are read-only here, so the container params use covariant Mapping /
# Sequence: a concrete `dict[str, str]` literal at a call site is not assignable to an
# invariant `dict[str, object]` parameter, but it is to `Mapping[str, object]`.
Record = Mapping[str, object]


def enc(path: str) -> str:
    return core.encode_path(path)


def jline(obj: object) -> str:
    return json.dumps(obj, separators=(",", ":")) + "\n"


def build(
    tmp: Path,
    *,
    sessions: Mapping[str, Sequence[Record]] | None = None,
    history: Sequence[Record] | None = None,
    cjson: Mapping[str, object] | None = None,
) -> tuple[Path, Path]:
    """Build a fake claude dir. `sessions` maps encoded-dir-name -> list of records."""
    claude_dir = tmp / "claude"
    projects = claude_dir / "projects"
    projects.mkdir(parents=True)
    for enc_name, records in (sessions or {}).items():
        pdir = projects / enc_name
        pdir.mkdir()
        # Name uniquely per project (real session files are UUID-named) so merging two
        # projects doesn't collide on the filename.
        (pdir / f"{enc_name}.jsonl").write_text("".join(jline(r) for r in records))
    (claude_dir / "history.jsonl").write_text(
        "".join(jline(h) for h in (history or []))
    )
    claude_json = tmp / "claude.json"
    if cjson is not None:
        claude_json.write_text(json.dumps(cjson, indent=2) + "\n")
    return claude_dir, claude_json


def run(
    claude_dir: Path,
    claude_json: Path,
    old: str,
    new: str,
    *,
    on_conflict: Literal["abort", "merge", "clean"] = "abort",
    move_dir: bool = False,
    rewrite_content: bool = False,
    heal: bool = False,
    dry_run: bool = False,
) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.claude_mv(
            old,
            new,
            claude_dir=claude_dir,
            claude_json=claude_json,
            yes=True,
            on_conflict=on_conflict,
            move_dir=move_dir,
            rewrite_content=rewrite_content,
            heal=heal,
            dry_run=dry_run,
        )
    return code, buf.getvalue()


def cwds(claude_dir: Path, enc_name: str) -> list[str]:
    out: list[str] = []
    for f in sorted((claude_dir / "projects" / enc_name).glob("*.jsonl")):
        out.extend(json.loads(line)["cwd"] for line in f.read_text().splitlines())
    return out


def one_session(claude_dir: Path, enc_name: str) -> dict[str, object]:
    """Parse the single fixture session record in a project dir."""
    files = sorted((claude_dir / "projects" / enc_name).glob("*.jsonl"))
    obj: dict[str, object] = json.loads(files[0].read_text())
    return obj


OLD = "/Users/me/proj"
NEW = "/Users/me/proj2"
E_OLD = enc(OLD)
E_NEW = enc(NEW)


def test_fresh_migrate(tmp_path: Path) -> None:
    cd, cj = build(
        tmp_path, sessions={E_OLD: [{"cwd": OLD}]}, history=[{"project": OLD}]
    )
    code, _out = run(cd, cj, OLD, NEW)
    assert code == 0
    assert not (cd / "projects" / E_OLD).exists()
    assert cwds(cd, E_NEW) == [NEW]
    assert json.loads((cd / "history.jsonl").read_text())["project"] == NEW


def test_incidental_preserved_by_default(tmp_path: Path) -> None:
    rec = {"cwd": OLD, "x": {"cmd": f"cd {OLD} && ls"}}
    cd, cj = build(tmp_path, sessions={E_OLD: [rec]})
    run(cd, cj, OLD, NEW)
    obj = one_session(cd, E_NEW)
    assert obj["cwd"] == NEW
    assert obj["x"] == {"cmd": f"cd {OLD} && ls"}  # incidental left intact


def test_prefix_trap_untouched(tmp_path: Path) -> None:
    cd, cj = build(
        tmp_path,
        sessions={E_OLD: [{"cwd": OLD}]},
        history=[{"project": OLD}, {"project": OLD + "-2"}],
    )
    run(cd, cj, OLD, NEW)
    projects = [
        json.loads(x)["project"]
        for x in (cd / "history.jsonl").read_text().splitlines()
    ]
    assert projects == [NEW, OLD + "-2"]


def test_dry_run_touches_nothing(tmp_path: Path) -> None:
    cd, cj = build(tmp_path, sessions={E_OLD: [{"cwd": OLD}]})
    code, out = run(cd, cj, OLD, NEW, dry_run=True)
    assert code == 0
    assert "Dry run" in out
    assert (cd / "projects" / E_OLD).exists()
    assert cwds(cd, E_OLD) == [OLD]


def test_idempotent_noop(tmp_path: Path) -> None:
    cd, cj = build(tmp_path, sessions={E_OLD: [{"cwd": OLD}]})
    run(cd, cj, OLD, NEW)
    code, out = run(cd, cj, OLD, NEW)
    assert code == 0
    assert "Already migrated; nothing to do." in out


def test_resume_rewrite_content(tmp_path: Path) -> None:
    rec = {"cwd": OLD, "x": f"cd {OLD}"}
    cd, cj = build(tmp_path, sessions={E_OLD: [rec]})
    run(cd, cj, OLD, NEW)  # default: incidental stays old
    assert one_session(cd, E_NEW)["x"] == f"cd {OLD}"
    code, _out = run(cd, cj, OLD, NEW, rewrite_content=True)  # resume
    assert code == 0
    assert one_session(cd, E_NEW)["x"] == f"cd {NEW}"


def test_conflict_abort(tmp_path: Path) -> None:
    cd, cj = build(tmp_path, sessions={E_OLD: [{"cwd": OLD}], E_NEW: [{"cwd": NEW}]})
    code, out = run(cd, cj, OLD, NEW)
    assert code == 3
    assert "Destination exists" in out


def test_conflict_merge(tmp_path: Path) -> None:
    cd, cj = build(tmp_path, sessions={E_OLD: [{"cwd": OLD}], E_NEW: [{"cwd": NEW}]})
    code, _out = run(cd, cj, OLD, NEW, on_conflict="merge")
    assert code == 0
    assert not (cd / "projects" / E_OLD).exists()
    assert len(list((cd / "projects" / E_NEW).glob("*.jsonl"))) == 2


def test_conflict_clean(tmp_path: Path) -> None:
    cd, cj = build(
        tmp_path,
        sessions={E_OLD: [{"cwd": OLD}], E_NEW: [{"cwd": NEW}, {"cwd": NEW}]},
    )
    code, _out = run(cd, cj, OLD, NEW, on_conflict="clean")
    assert code == 0
    # clean replaces dest with the (migrated) source content
    assert cwds(cd, E_NEW) == [NEW]


def test_collision_guard_refuses_unrelated(tmp_path: Path) -> None:
    # dest encodes the same but belongs to a different real project
    cd, cj = build(
        tmp_path,
        sessions={E_OLD: [{"cwd": OLD}], E_NEW: [{"cwd": "/Users/me.proj2"}]},
    )
    code, out = run(cd, cj, OLD, NEW, on_conflict="merge")
    assert code == 3
    assert "different" in out
    assert "collision" in out


def test_move_dir_pending(tmp_path: Path) -> None:
    real_old = tmp_path / "real" / "proj"
    real_old.mkdir(parents=True)
    (real_old / "main.py").write_text("code")
    real_new = tmp_path / "real" / "proj2"
    cd, cj = build(tmp_path, sessions={enc(str(real_old)): [{"cwd": str(real_old)}]})
    code, _out = run(cd, cj, str(real_old), str(real_new), move_dir=True)
    assert code == 0
    assert not real_old.exists()
    assert (real_new / "main.py").read_text() == "code"


def test_move_dir_bad_both(tmp_path: Path) -> None:
    real_old = tmp_path / "real" / "proj"
    real_new = tmp_path / "real" / "proj2"
    real_old.mkdir(parents=True)
    real_new.mkdir(parents=True)
    cd, cj = build(tmp_path, sessions={enc(str(real_old)): [{"cwd": str(real_old)}]})
    code, out = run(cd, cj, str(real_old), str(real_new), move_dir=True)
    assert code == 3
    assert "both" in out.lower()


def test_move_dir_bad_neither(tmp_path: Path) -> None:
    real_old = tmp_path / "real" / "proj"
    real_new = tmp_path / "real" / "proj2"
    cd, cj = build(tmp_path, sessions={enc(str(real_old)): [{"cwd": str(real_old)}]})
    code, out = run(cd, cj, str(real_old), str(real_new), move_dir=True)
    assert code == 3
    assert "neither" in out.lower()


def test_claude_json_remap(tmp_path: Path) -> None:
    cjson = {
        "projects": {
            OLD: {"allowedTools": ["Bash"]},
            OLD + "-setup": {"allowedTools": ["Read"]},  # sibling: must stay
            OLD + "/sub": {"allowedTools": ["Edit"]},  # under old: must remap
        },
        "githubRepoPaths": {"me/proj": [OLD, OLD + "-2"]},
    }
    cd, cj = build(tmp_path, sessions={E_OLD: [{"cwd": OLD}]}, cjson=cjson)
    run(cd, cj, OLD, NEW)
    got = json.loads(cj.read_text())
    assert set(got["projects"]) == {NEW, OLD + "-setup", NEW + "/sub"}
    assert got["projects"][NEW] == {"allowedTools": ["Bash"]}
    assert got["githubRepoPaths"]["me/proj"] == [NEW, OLD + "-2"]


def test_resume_stale_cwd_dir(tmp_path: Path) -> None:
    # dir already renamed to enc_new but its cwd is still old (interrupted/half-done);
    # the tool should recognize it and finish, not report "nothing found" + a false Done
    cd, cj = build(tmp_path, sessions={E_NEW: [{"cwd": OLD}]})
    code, _out = run(cd, cj, OLD, NEW)
    assert code == 0
    assert cwds(cd, E_NEW) == [NEW]


def test_unrelated_project_at_enc_new_untouched(tmp_path: Path) -> None:
    # a different real project happens to live at enc_new; leave it byte-for-byte alone
    third = "/Users/me/somethingelse"
    cd, cj = build(tmp_path, sessions={E_NEW: [{"cwd": third}]})
    before = (cd / "projects" / E_NEW / f"{E_NEW}.jsonl").read_bytes()
    code, out = run(cd, cj, OLD, NEW)
    assert code == 0
    assert "Nothing to do." in out
    assert (cd / "projects" / E_NEW / f"{E_NEW}.jsonl").read_bytes() == before


def test_content_mode_leading_boundary(tmp_path: Path) -> None:
    # a different path that merely ends with OLD must not be rewritten in content mode
    rec = {"cwd": OLD, "x": f"cp /mnt/backup{OLD}/f ./f"}
    cd, cj = build(tmp_path, sessions={E_OLD: [rec]})
    run(cd, cj, OLD, NEW, rewrite_content=True)
    obj = one_session(cd, E_NEW)
    assert obj["cwd"] == NEW
    assert obj["x"] == f"cp /mnt/backup{OLD}/f ./f"  # backup path left intact


def test_sub_project_refused(tmp_path: Path) -> None:
    # /a is moving; /a/c is its own project -> refuse, change nothing
    cd, cj = build(
        tmp_path,
        sessions={enc("/a"): [{"cwd": "/a"}], enc("/a/c"): [{"cwd": "/a/c"}]},
        history=[{"project": "/a"}, {"project": "/a/c"}],
    )
    code, out = run(cd, cj, "/a", "/b")
    assert code == 3
    assert "separate project" in out
    assert (cd / "projects" / enc("/a")).exists()  # untouched
    projects = [
        json.loads(x)["project"]
        for x in (cd / "history.jsonl").read_text().splitlines()
    ]
    assert projects == ["/a", "/a/c"]  # no cascade happened


def test_own_subdir_cwd_is_not_a_sub_project(tmp_path: Path) -> None:
    # a session in /a whose cwd is a subdir (e.g. a worktree in the same project) is
    # not a separate project, so the move proceeds and rewrites it
    cd, cj = build(tmp_path, sessions={enc("/a"): [{"cwd": "/a"}, {"cwd": "/a/wt"}]})
    code, _out = run(cd, cj, "/a", "/b")
    assert code == 0
    assert set(cwds(cd, enc("/b"))) == {"/b", "/b/wt"}


def test_content_warn_non_idempotent(tmp_path: Path) -> None:
    old, new = "/a/proj", "/a/proj v2"  # new extends old across a space
    cd, cj = build(tmp_path, sessions={enc(old): [{"cwd": old}]})
    code, out = run(cd, cj, old, new, rewrite_content=True)
    assert code == 0
    assert "WARNING" in out
    assert "double-apply" in out
    assert cwds(cd, enc(new)) == [new]


def test_nested_paths_refused(tmp_path: Path) -> None:
    cd, cj = build(tmp_path, sessions={E_OLD: [{"cwd": OLD}]})
    code, out = run(cd, cj, OLD, OLD + "/app")  # new under old
    assert code == 2
    assert "inside the other" in out
    assert cwds(cd, E_OLD) == [OLD]  # untouched
    code, _out = run(cd, cj, OLD + "/app", OLD)  # old under new
    assert code == 2


def test_identical_paths_rejected(tmp_path: Path) -> None:
    cd, cj = build(tmp_path, sessions={E_OLD: [{"cwd": OLD}]})
    code, out = run(cd, cj, OLD, OLD)
    assert code == 2
    assert "same location" in out
    assert cwds(cd, E_OLD) == [OLD]  # untouched


def test_claude_json_collision_refused(tmp_path: Path) -> None:
    # both old and new already have a (differing) config entry -> refuse, change nothing
    cjson = {
        "projects": {OLD: {"allowedTools": ["Bash"]}, NEW: {"allowedTools": ["Read"]}}
    }
    cd, cj = build(tmp_path, sessions={E_OLD: [{"cwd": OLD}]}, cjson=cjson)
    code, out = run(cd, cj, OLD, NEW)
    assert code == 3
    assert ".claude.json" in out
    got = json.loads(cj.read_text())
    assert got["projects"][OLD] == {"allowedTools": ["Bash"]}  # nothing dropped
    assert got["projects"][NEW] == {"allowedTools": ["Read"]}
    assert (cd / "projects" / E_OLD).exists()  # projects dir untouched too


def test_claude_json_collision_identical_ok(tmp_path: Path) -> None:
    # identical entries: no information is lost, so proceed
    cfg = {"allowedTools": ["Bash"]}
    cjson = {"projects": {OLD: cfg, NEW: dict(cfg)}}
    cd, cj = build(tmp_path, sessions={E_OLD: [{"cwd": OLD}]}, cjson=cjson)
    code, _out = run(cd, cj, OLD, NEW)
    assert code == 0
    assert set(json.loads(cj.read_text())["projects"]) == {NEW}


def test_dry_run_conflict_returns_2(tmp_path: Path) -> None:
    # conflict refusal is evaluated before the dry-run gate, so exit is 2 not 0
    cd, cj = build(tmp_path, sessions={E_OLD: [{"cwd": OLD}], E_NEW: [{"cwd": NEW}]})
    code, _out = run(cd, cj, OLD, NEW, dry_run=True)
    assert code == 3


def test_rollback_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cd, cj = build(
        tmp_path,
        sessions={E_OLD: [{"cwd": OLD}]},
        history=[{"project": OLD}],
        cjson={"projects": {OLD: {"a": 1}}},
    )
    # make the final step (.claude.json write) blow up mid-execute
    original = core._rewrite_claude_json  # noqa: SLF001  (white-box: fail mid-execute)

    def boom(path: Path, old: str, new: str, *, apply: bool) -> int:
        if apply:
            raise RuntimeError("boom")
        return original(path, old, new, apply=apply)

    monkeypatch.setattr(core, "_rewrite_claude_json", boom)
    with pytest.raises(RuntimeError):
        run(cd, cj, OLD, NEW)
    # every mutation reverted
    assert (cd / "projects" / E_OLD).exists()
    assert not (cd / "projects" / E_NEW).exists()
    assert cwds(cd, E_OLD) == [OLD]
    assert json.loads((cd / "history.jsonl").read_text())["project"] == OLD
    assert json.loads(cj.read_text())["projects"] == {OLD: {"a": 1}}


def test_rollback_move_dir_restores_real_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the highest-stakes rollback branch: a failed --move-dir must put the real
    # project directory back and undo the metadata too
    real_old = tmp_path / "real" / "proj"
    real_old.mkdir(parents=True)
    (real_old / "main.py").write_text("code")
    real_new = tmp_path / "real" / "proj2"
    e_old = enc(str(real_old))
    cd, cj = build(
        tmp_path,
        sessions={e_old: [{"cwd": str(real_old)}]},
        cjson={"projects": {str(real_old): {"a": 1}}},
    )
    original = core._rewrite_claude_json  # noqa: SLF001  (white-box: fail mid-execute)

    def boom(path: Path, old: str, new: str, *, apply: bool) -> int:
        if apply:
            raise RuntimeError("boom")
        return original(path, old, new, apply=apply)

    monkeypatch.setattr(core, "_rewrite_claude_json", boom)
    with pytest.raises(RuntimeError):
        run(cd, cj, str(real_old), str(real_new), move_dir=True)
    assert (real_old / "main.py").read_text() == "code"  # real dir back
    assert not real_new.exists()
    assert (cd / "projects" / e_old).exists()  # metadata back too


def test_mixed_refused_then_heal(tmp_path: Path) -> None:
    # dir already at enc_new, one stray old cwd -> genuine (non-nested) partial state
    cd, cj = build(tmp_path)
    pdir = cd / "projects" / E_NEW
    pdir.mkdir()
    (pdir / "new.jsonl").write_text(jline({"cwd": NEW}))
    (pdir / "old.jsonl").write_text(jline({"cwd": OLD}))
    code, out = run(cd, cj, OLD, NEW)
    assert code == 3
    assert "partial migration" in out
    assert OLD in cwds(cd, E_NEW)  # untouched
    code, _out = run(cd, cj, OLD, NEW, heal=True)
    assert code == 0
    assert set(cwds(cd, E_NEW)) == {NEW}
