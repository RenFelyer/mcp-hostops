"""Tiered store: bytes/json/stamped across tiers, atomic writes, private directory."""

import json
from math import inf
from pathlib import Path

import pytest

from mcp_hostops.core import store
from mcp_hostops.core.config.environment import Settings


@pytest.fixture
def s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    store._MEMORY.clear()
    return Settings()


def test_bytes_roundtrip_all_tiers(s: Settings) -> None:
    for tier in ("session", "runtime", "persistent"):
        store.write_bytes(s, tier, "sub/blob", b"\x00\xff")
        assert store.read_bytes(s, tier, "sub/blob") == b"\x00\xff"
    assert store.read_bytes(s, "runtime", "absent") is None


def test_json_roundtrip_and_tolerant(s: Settings) -> None:
    store.write_json(s, "persistent", "state.json", {"a": 1})
    assert store.read_json(s, "persistent", "state.json") == {"a": 1}
    store.write_bytes(s, "persistent", "bad", b"{garbage")
    assert store.read_json(s, "persistent", "bad") is None
    store.write_bytes(s, "persistent", "arr", b"[1, 2]")
    assert store.read_json(s, "persistent", "arr") is None  # not an object


def test_stamped_roundtrip_and_bad_timestamp(s: Settings) -> None:
    assert store.read_stamped(s, "runtime", "s.json") == (inf, {})
    store.write_stamped(s, "runtime", "s.json", {"hosts": {"a": "available"}})
    age, data = store.read_stamped(s, "runtime", "s.json")
    assert 0 <= age < 5
    assert data == {"hosts": {"a": "available"}}
    store.write_bytes(s, "runtime", "s.json", json.dumps({"checked_at": "yesterday"}).encode())
    assert store.read_stamped(s, "runtime", "s.json") == (inf, {})


def test_stamped_extra_fields_are_content(s: Settings) -> None:
    store.write_bytes(
        s, "runtime", "s.json", json.dumps({"checked_at": "123", "hosts": {"a": "x"}, "n": [1, None]}).encode()
    )
    age, data = store.read_stamped(s, "runtime", "s.json")
    assert age > 0  # pydantic accepts a number given as a string
    assert data == {"hosts": {"a": "x"}, "n": [1, None]}


def test_forget_removes_slot(s: Settings) -> None:
    store.write_bytes(s, "session", "x", b"1")
    store.write_bytes(s, "persistent", "sub/x", b"1")
    store.forget(s, "session", "x")
    store.forget(s, "persistent", "sub/x")
    store.forget(s, "runtime", "never")  # absence is not an error
    assert store.read_bytes(s, "session", "x") is None
    assert store.read_bytes(s, "persistent", "sub/x") is None


def test_atomic_write_leaves_no_temp(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "cfg"
    store.atomic_write(path, b"data")
    assert path.read_bytes() == b"data"
    assert list(path.parent.iterdir()) == [path]


def test_private_dir_runtime(s: Settings) -> None:
    made = store.private_dir(s, "runtime", "control")
    assert made.stat().st_mode & 0o777 == 0o700
    store.private_dir(s, "runtime", "control")  # again — no error
    made.chmod(0o750)
    with pytest.raises(PermissionError):
        store.private_dir(s, "runtime", "control")


def test_private_dir_rejects_symlink(s: Settings) -> None:
    # A planted symlink to our own 0700 directory would redirect sockets elsewhere.
    s.runtime_dir.mkdir(parents=True, exist_ok=True)
    real = s.runtime_dir / "real"
    real.mkdir(mode=0o700)
    (s.runtime_dir / "control").symlink_to(real)
    with pytest.raises(PermissionError):
        store.private_dir(s, "runtime", "control")


def test_private_dir_session_has_no_directory(s: Settings) -> None:
    with pytest.raises(ValueError, match="session"):
        store.private_dir(s, "session", "control")
