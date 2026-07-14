import pytest

from claude_mv import __version__
from claude_mv.cli import app


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        app("--version")
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_app_rejects_overlapping_paths(capsys: pytest.CaptureFixture[str]) -> None:
    # Drives cyclopts parsing through to main: overlapping paths are refused (exit 1)
    # before any disk is touched.
    with pytest.raises(SystemExit) as exc_info:
        app(["/a/proj", "/a/proj/sub"])
    assert exc_info.value.code == 1
    assert "inside the other" in capsys.readouterr().out
