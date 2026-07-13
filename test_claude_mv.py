"""Behavior tests for claude_mv.

Run with:
    uv run --with pytest --with 'cyclopts>=3' pytest test_claude_mv.py

They drive `main()` directly against a throwaway `~/.claude`-shaped tree built under
pytest's `tmp_path`, and assert on the exit code plus the resulting on-disk state.
"""

import contextlib
import io
import json
from pathlib import Path

import claude_mv


def enc(path: str) -> str:
    return claude_mv.encode_path(path)


def jline(obj: object) -> str:
    return json.dumps(obj, separators=(",", ":")) + "\n"


def build(
    tmp: Path,
    *,
    sessions: dict[str, list[dict]] | None = None,
    history: list[dict] | None = None,
    cjson: dict | None = None,
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


def run(claude_dir: Path, claude_json: Path, old: str, new: str, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = claude_mv.main(
            old, new, claude_dir=claude_dir, claude_json=claude_json, yes=True, **kw
        )
    return code, buf.getvalue()


def cwds(claude_dir: Path, enc_name: str) -> list[str]:
    out = []
    for f in sorted((claude_dir / "projects" / enc_name).glob("*.jsonl")):
        for line in f.read_text().splitlines():
            out.append(json.loads(line)["cwd"])
    return out


def one_session(claude_dir: Path, enc_name: str) -> dict:
    """Parse the single fixture session record in a project dir."""
    files = sorted((claude_dir / "projects" / enc_name).glob("*.jsonl"))
    return json.loads(files[0].read_text())


OLD = "/Users/me/proj"
NEW = "/Users/me/proj2"
E_OLD = enc(OLD)
E_NEW = enc(NEW)


def test_fresh_migrate(tmp_path):
    cd, cj = build(
        tmp_path, sessions={E_OLD: [{"cwd": OLD}]}, history=[{"project": OLD}]
    )
    code, out = run(cd, cj, OLD, NEW)
    assert code == 0
    assert not (cd / "projects" / E_OLD).exists()
    assert cwds(cd, E_NEW) == [NEW]
    assert json.loads((cd / "history.jsonl").read_text())["project"] == NEW


def test_incidental_preserved_by_default(tmp_path):
    rec = {"cwd": OLD, "x": {"cmd": f"cd {OLD} && ls"}}
    cd, cj = build(tmp_path, sessions={E_OLD: [rec]})
    run(cd, cj, OLD, NEW)
    obj = one_session(cd, E_NEW)
    assert obj["cwd"] == NEW
    assert obj["x"]["cmd"] == f"cd {OLD} && ls"  # incidental left intact


def test_prefix_trap_untouched(tmp_path):
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


def test_dry_run_touches_nothing(tmp_path):
    cd, cj = build(tmp_path, sessions={E_OLD: [{"cwd": OLD}]})
    code, out = run(cd, cj, OLD, NEW, dry_run=True)
    assert code == 0
    assert "Dry run" in out
    assert (cd / "projects" / E_OLD).exists()
    assert cwds(cd, E_OLD) == [OLD]


def test_idempotent_noop(tmp_path):
    cd, cj = build(tmp_path, sessions={E_OLD: [{"cwd": OLD}]})
    run(cd, cj, OLD, NEW)
    code, out = run(cd, cj, OLD, NEW)
    assert code == 0
    assert "Already migrated; nothing to do." in out


def test_resume_rewrite_content(tmp_path):
    rec = {"cwd": OLD, "x": f"cd {OLD}"}
    cd, cj = build(tmp_path, sessions={E_OLD: [rec]})
    run(cd, cj, OLD, NEW)  # default: incidental stays old
    assert one_session(cd, E_NEW)["x"] == f"cd {OLD}"
    code, out = run(cd, cj, OLD, NEW, rewrite_content=True)  # resume
    assert code == 0
    assert one_session(cd, E_NEW)["x"] == f"cd {NEW}"


def test_conflict_abort(tmp_path):
    cd, cj = build(tmp_path, sessions={E_OLD: [{"cwd": OLD}], E_NEW: [{"cwd": NEW}]})
    code, out = run(cd, cj, OLD, NEW)
    assert code == 2
    assert "Destination exists" in out


def test_conflict_merge(tmp_path):
    cd, cj = build(tmp_path, sessions={E_OLD: [{"cwd": OLD}], E_NEW: [{"cwd": NEW}]})
    code, out = run(cd, cj, OLD, NEW, on_conflict="merge")
    assert code == 0
    assert not (cd / "projects" / E_OLD).exists()
    assert len(list((cd / "projects" / E_NEW).glob("*.jsonl"))) == 2


def test_conflict_clean(tmp_path):
    cd, cj = build(
        tmp_path,
        sessions={E_OLD: [{"cwd": OLD}], E_NEW: [{"cwd": NEW}, {"cwd": NEW}]},
    )
    code, out = run(cd, cj, OLD, NEW, on_conflict="clean")
    assert code == 0
    # clean replaces dest with the (migrated) source content
    assert cwds(cd, E_NEW) == [NEW]


def test_collision_guard_refuses_unrelated(tmp_path):
    # dest encodes the same but belongs to a different real project
    cd, cj = build(
        tmp_path,
        sessions={E_OLD: [{"cwd": OLD}], E_NEW: [{"cwd": "/Users/me.proj2"}]},
    )
    code, out = run(cd, cj, OLD, NEW, on_conflict="merge")
    assert code == 2
    assert "different" in out and "collision" in out


def test_move_dir_pending(tmp_path):
    real_old = tmp_path / "real" / "proj"
    real_old.mkdir(parents=True)
    (real_old / "main.py").write_text("code")
    real_new = tmp_path / "real" / "proj2"
    cd, cj = build(tmp_path, sessions={enc(str(real_old)): [{"cwd": str(real_old)}]})
    code, out = run(cd, cj, str(real_old), str(real_new), move_dir=True)
    assert code == 0
    assert not real_old.exists()
    assert (real_new / "main.py").read_text() == "code"


def test_move_dir_bad_both(tmp_path):
    real_old = tmp_path / "real" / "proj"
    real_new = tmp_path / "real" / "proj2"
    real_old.mkdir(parents=True)
    real_new.mkdir(parents=True)
    cd, cj = build(tmp_path, sessions={enc(str(real_old)): [{"cwd": str(real_old)}]})
    code, out = run(cd, cj, str(real_old), str(real_new), move_dir=True)
    assert code == 2
    assert "both" in out.lower()


def test_move_dir_bad_neither(tmp_path):
    real_old = tmp_path / "real" / "proj"
    real_new = tmp_path / "real" / "proj2"
    cd, cj = build(tmp_path, sessions={enc(str(real_old)): [{"cwd": str(real_old)}]})
    code, out = run(cd, cj, str(real_old), str(real_new), move_dir=True)
    assert code == 2
    assert "neither" in out.lower()


def test_claude_json_remap(tmp_path):
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


def test_mixed_refused_then_heal(tmp_path):
    # dir already at enc_new, one stray old cwd -> genuine (non-nested) partial state
    cd, cj = build(tmp_path)
    pdir = cd / "projects" / E_NEW
    pdir.mkdir()
    (pdir / "new.jsonl").write_text(jline({"cwd": NEW}))
    (pdir / "old.jsonl").write_text(jline({"cwd": OLD}))
    code, out = run(cd, cj, OLD, NEW)
    assert code == 2
    assert "partial migration" in out
    assert OLD in cwds(cd, E_NEW)  # untouched
    code, out = run(cd, cj, OLD, NEW, heal=True)
    assert code == 0
    assert set(cwds(cd, E_NEW)) == {NEW}
