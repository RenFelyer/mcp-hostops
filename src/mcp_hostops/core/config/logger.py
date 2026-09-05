"""Debug logging to a file; disabled during normal operation.

Enabled by the `HOSTOPS_MCP_DEBUG_LOG=<path>` variable: then everything from DEBUG level
up, including fastmcp, is written to that file. The file is kept at mode 0600 — records
sometimes contain commands and host names. Without the variable, no handlers are added,
and the server over stdio outputs nothing extra. The sudo password never ends up in the
log: argv and exit codes are logged, but not stdin.
"""

import logging
from pathlib import Path

from .environment import get_settings

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_FILE_MODE = 0o600


def setup() -> None:
    """Attach the debug file handler if one is set in the settings."""
    path: Path | None = get_settings().debug_log
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=_FILE_MODE)
    path.chmod(_FILE_MODE)  # `touch` doesn't change the permissions of an existing file
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
