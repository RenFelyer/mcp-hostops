"""Debug logging: off by default, a 0600 file handler when a path is configured."""

import logging
from pathlib import Path

import pytest

from mcp_hostops.core.config import logger
from mcp_hostops.core.config.environment import get_settings


def test_setup_noop_without_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "debug_log", None)
    before = list(logging.getLogger().handlers)
    logger.setup()
    assert logging.getLogger().handlers == before  # nothing attached


def test_setup_attaches_private_file_handler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "debug.log"
    monkeypatch.setattr(get_settings(), "debug_log", path)
    root = logging.getLogger()
    before = list(root.handlers)
    before_level = root.level
    try:
        logger.setup()
        added = [h for h in root.handlers if h not in before]
        assert len(added) == 1
        assert path.exists()
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        for handler in root.handlers:
            if handler not in before:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(before_level)
