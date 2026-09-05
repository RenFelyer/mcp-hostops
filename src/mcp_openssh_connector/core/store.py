"""State files and private directories.

Reading tolerates garbage: a corrupt file is treated as absent. Writing is atomic —
through a unique temporary file in the same directory and `replace`, so two servers
writing at the same time don't corrupt each other's file, and a reader never sees a
half-written file.
"""

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from time import time

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from .config.constants import PRIVATE_DIR_MODE

_OBJECT = TypeAdapter(dict[str, JsonValue])


class _Stamped(BaseModel):
    """A record with a timestamp; the remaining fields are the content."""

    model_config = ConfigDict(extra="allow")

    checked_at: float


def load(path: Path) -> dict[str, JsonValue] | None:
    """Read a JSON object; None means the file is missing, corrupt, or not an object."""
    try:
        return _OBJECT.validate_json(path.read_bytes())
    except (OSError, ValidationError):
        return None


def write_bytes(path: Path, data: bytes) -> None:
    """Write bytes atomically; errors propagate.

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


def save(path: Path, data: Mapping[str, JsonValue]) -> None:
    """Write a JSON object atomically; errors propagate.

    Raises:
        OSError: the directory is inaccessible or the disk rejected the write.
    """
    write_bytes(path, json.dumps(data, ensure_ascii=False).encode())


def load_stamped(path: Path) -> tuple[float, dict[str, JsonValue]]:
    """Read a record with its timestamp.

    Returns:
        The record's age in seconds and its content without the timestamp. Age `inf` and
        empty content mean the file is missing, corrupt, or the timestamp isn't a number.
    """
    try:
        stamped = _Stamped.model_validate(load(path))
    except ValidationError:
        return float("inf"), {}
    return time() - stamped.checked_at, stamped.model_extra or {}


def save_stamped(path: Path, data: Mapping[str, JsonValue]) -> None:
    """Write content with the current timestamp; errors propagate."""
    save(path, {"checked_at": time(), **data})


def private_dir(path: Path) -> Path:
    """Create a 0700 directory and make sure it's ours and closed to others.

    Needed where a foreign directory is dangerous: a ControlMaster socket in a planted
    directory would hand the connection and the sudo password to another process. The
    path itself is checked without following a symlink: a planted symlink would redirect
    sockets into a directory we didn't choose.

    Raises:
        PermissionError: the path is not a directory (including a symlink), is not
            owned by us, or is open to group/others.
        OSError: the directory can't be created.
    """
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError(f"directory {path} must be ours, not a symlink, and mode 0700")
    return path
