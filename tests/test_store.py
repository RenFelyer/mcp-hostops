"""State files: atomic writes, timestamps, private directory."""

import json
from math import inf
from pathlib import Path

import pytest

from mcp_hostops.core import store


def test_save_atomic_and_load_tolerant(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    store.save(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    assert list(tmp_path.joinpath("nested").iterdir()) == [path]  # no leftover temp file

    path.write_text("{garbage")
    assert store.load(path) is None
    path.write_text("[1, 2]")
    assert store.load(path) is None  # not an object — also empty


def test_stamped_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    assert store.load_stamped(path) == (inf, {})
    store.save_stamped(path, {"hosts": {"a": "available"}})
    age, data = store.load_stamped(path)
    assert 0 <= age < 5
    assert data == {"hosts": {"a": "available"}}
    path.write_text(json.dumps({"checked_at": "yesterday", "hosts": {}}))
    assert store.load_stamped(path) == (inf, {})


def test_private_dir(tmp_path: Path) -> None:
    made = store.private_dir(tmp_path / "priv")
    assert made.stat().st_mode & 0o777 == 0o700
    store.private_dir(made)  # again — no errors
    made.chmod(0o750)
    with pytest.raises(PermissionError):
        store.private_dir(made)


def test_private_dir_rejects_symlink(tmp_path: Path) -> None:
    # A planted symlink to our own 0700 directory would redirect sockets elsewhere.
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(PermissionError):
        store.private_dir(link)


def test_write_bytes_atomic(tmp_path: Path) -> None:
    path = tmp_path / "blob"
    store.write_bytes(path, b"\x00\xff")
    assert path.read_bytes() == b"\x00\xff"
    assert list(tmp_path.iterdir()) == [path]


def test_stamped_extra_fields_are_content(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"checked_at": "123", "hosts": {"a": "x"}, "n": [1, None]}))
    age, data = store.load_stamped(path)
    assert age > 0  # pydantic accepts a number given as a string
    assert data == {"hosts": {"a": "x"}, "n": [1, None]}
