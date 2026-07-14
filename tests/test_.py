import importlib.metadata

import claude_mv


def test_version() -> None:
    assert importlib.metadata.version("claude_mv") == claude_mv.__version__
