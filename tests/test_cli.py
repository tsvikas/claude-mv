import pytest

from claude_mv import __version__
from claude_mv.cli import app


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        app("--version")
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_app(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        app("path/to/file")
    assert exc_info.value.code == 0
    assert "path/to/file" in capsys.readouterr().out
