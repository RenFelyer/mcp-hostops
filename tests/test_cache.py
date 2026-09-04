"""Кэш статусов: чужое значение делает кэш недействительным целиком."""

import json
from math import inf
from pathlib import Path

import pytest

from mcp_openssh_connector.core import cache
from mcp_openssh_connector.core.config.environment import Settings


@pytest.fixture
def s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return Settings()


def test_roundtrip(s: Settings) -> None:
    cache.write({"a": "available", "b": "unknown"}, s)
    age, statuses = cache.read(s)
    assert 0 <= age < 5
    assert statuses == {"a": "available", "b": "unknown"}


def test_garbage_status_invalidates_cache(s: Settings) -> None:
    cache.write({"a": "available"}, s)
    s.cache_file.write_text(json.dumps({"checked_at": 1e12, "hosts": {"a": "available", "b": "maybe"}}))
    assert cache.read(s) == (inf, {})
