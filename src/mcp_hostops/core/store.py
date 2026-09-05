"""One tiered store for all saved and cached state.

A slot is addressed by (tier, name); `name` may contain `/` for subdirectories on a
disk tier. Three tiers by lifetime:

- `session` — in process memory, gone when the server stops;
- `runtime` — the runtime dir (tmpfs), until the machine reboots;
- `persistent` — the cache dir, survives a reboot (though the OS may clear a cache).

Values are raw bytes or JSON; the `stamped` variants add a timestamp so a reader can
tell the record's age (TTL). Reading tolerates garbage — a missing or corrupt slot reads
as absent; disk writes are atomic (a unique temp file plus `replace`), so concurrent
writers don't corrupt a slot and a reader never sees a half-written one.

New state picks a tier by how long it must live and goes here — no per-purpose module.
"""

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from time import time

from pydantic import JsonValue, TypeAdapter, ValidationError

from .config.constants import PRIVATE_DIR_MODE
from .config.environment import Settings
from .schemas import Stamped, Tier

_OBJECT = TypeAdapter(dict[str, JsonValue])
_MEMORY: dict[str, bytes] = {}  # backing for the session tier; lives as long as the process


def _dir(s: Settings, tier: Tier) -> Path | None:
    """Base directory of a disk tier; None for the in-memory session tier."""
    if tier == "session":
        return None
    return s.runtime_dir if tier == "runtime" else s.cache_dir


def read_bytes(s: Settings, tier: Tier, name: str) -> bytes | None:
    """Slot contents as bytes; None if it's missing or unreadable."""
    base = _dir(s, tier)
    if base is None:
        return _MEMORY.get(name)
    try:
        return (base / name).read_bytes()
    except OSError:
        return None


def atomic_write(path: Path, data: bytes) -> None:
    """Write bytes to an exact path atomically (temp file + `replace`); errors propagate.

    For files the store doesn't own but must write safely (the managed ssh config).

    Raises:
        OSError: the directory is inaccessible or the disk rejected the write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_bytes(s: Settings, tier: Tier, name: str, data: bytes) -> None:
    """Write a slot atomically; errors propagate.

    Raises:
        OSError: the directory is inaccessible or the disk rejected the write.
    """
    base = _dir(s, tier)
    if base is None:
        _MEMORY[name] = data
    else:
        atomic_write(base / name, data)


def forget(s: Settings, tier: Tier, name: str) -> None:
    """Remove a slot; absence is not an error."""
    base = _dir(s, tier)
    if base is None:
        _MEMORY.pop(name, None)
        return
    (base / name).unlink(missing_ok=True)


def read_json(s: Settings, tier: Tier, name: str) -> dict[str, JsonValue] | None:
    """Slot as a JSON object; None if missing, unreadable, corrupt, or not an object."""
    raw = read_bytes(s, tier, name)
    if raw is None:
        return None
    try:
        return _OBJECT.validate_json(raw)
    except ValidationError:
        return None


def write_json(s: Settings, tier: Tier, name: str, data: Mapping[str, JsonValue]) -> None:
    """Write a JSON object atomically; errors propagate."""
    write_bytes(s, tier, name, json.dumps(data, ensure_ascii=False).encode())


def read_stamped(s: Settings, tier: Tier, name: str) -> tuple[float, dict[str, JsonValue]]:
    """Read a timestamped record.

    Returns:
        The record's age in seconds and its content without the timestamp. Age `inf` and
        empty content mean the slot is missing, corrupt, or the timestamp isn't a number.
    """
    try:
        stamped = Stamped.model_validate(read_json(s, tier, name))
    except ValidationError:
        return float("inf"), {}
    return time() - stamped.checked_at, stamped.model_extra or {}


def write_stamped(s: Settings, tier: Tier, name: str, data: Mapping[str, JsonValue]) -> None:
    """Write content with the current timestamp; errors propagate."""
    write_json(s, tier, name, {"checked_at": time(), **data})


def private_dir(s: Settings, tier: Tier, name: str) -> Path:
    """A 0700 subdirectory of a disk tier that must be ours and not a symlink.

    Needed where a foreign directory is dangerous: a ControlMaster socket in a planted
    directory would hand the connection and the sudo password to another process. The
    path is checked without following a symlink.

    Raises:
        ValueError: the session tier has no directory.
        PermissionError: the path is not a directory (including a symlink), is not
            owned by us, or is open to group/others.
        OSError: the directory can't be created.
    """
    base = _dir(s, tier)
    if base is None:
        raise ValueError("the session tier has no directory")
    path = base / name
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError(f"directory {path} must be ours, not a symlink, and mode 0700")
    return path
