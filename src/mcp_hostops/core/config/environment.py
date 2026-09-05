"""Server settings: timeouts, thresholds, paths to state.

Values are read from the environment with the `HOSTOPS_MCP_` prefix. State lives in
three places by lifetime: the runtime directory (ControlMaster sockets, host status
cache, llms source check results — until reboot), the cache (downloaded via `llms.txt`,
sometimes tens of megabytes), and data (user-defined llms sources — must not be lost).
The environment is read when settings are created; the runtime directory is fetched from
`XDG_RUNTIME_DIR` on access. Immutable values live in `constants`.
"""

import os
import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..schemas import JumpProbe
from .constants import APP_NAME


def _xdg(var: str, fallback: Path) -> Path:
    return Path(os.environ.get(var) or fallback) / APP_NAME


def _runtime_dir() -> Path:
    """The runtime directory: `XDG_RUNTIME_DIR`, or a temp dir scoped to our uid otherwise.

    A shared `/tmp` without the uid in the name would let another user plant a directory
    under our sockets; the ownership check lives in `store.private_dir`.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / APP_NAME
    return Path(tempfile.gettempdir()) / f"{APP_NAME}-{os.getuid()}"


class Settings(BaseSettings):
    """Settings fields with defaults; each one's meaning is in the comment beside it."""

    model_config = SettingsConfigDict(env_prefix="HOSTOPS_MCP_")

    # ── probe timeouts (seconds) ────────────────────────────────────────────────
    connect_timeout: float = 1.0  # a direct TCP connect and ssh's ConnectTimeout
    ssh_g_timeout: float = 2.0  # parsing one host's config via `ssh -G`
    jump_timeout: float = 4.0  # ssh to the jump host and probes behind it
    deep_timeout: float = 6.0  # a real login via `ssh ... true`
    jump_probe: JumpProbe = "script"  # "forward" — probe behind a jump via `ssh -W`, without a shell on it
    keyscan_timeout: float = 10.0  # ssh-keyscan when trusting a host (also passed as its own -T)

    # ── command execution ───────────────────────────────────────────────────────
    run_timeout: float = 60.0  # default `run` timeout when the call doesn't set its own
    max_command_timeout: float = 3600.0  # cap on the client-supplied `run` timeout; more is a call error
    max_wait: float = 3600.0  # cap on the client-supplied `wait` in `job`; more and we just wait that long
    output_limit: int = 1_000_000  # bytes of stdout/stderr, beyond that we truncate
    control_persist: int = 60  # seconds the master connection stays alive after the last command
    job_history: int = 50  # how many finished jobs to remember; older ones are forgotten

    # ── status cache ────────────────────────────────────────────────────────────
    cache_ttl: float = 900.0  # seconds; older and list_hosts remeasures on its own

    # ── llms.txt ────────────────────────────────────────────────────────────────
    llms_timeout: float = 20.0  # seconds for one HTTP request
    llms_cache_ttl: float = 86400.0  # seconds a download stays valid in the on-disk cache
    llms_max_bytes: int = 64_000_000  # cap on a single download (full can be 40 MB)
    llms_page_chars: int = 20_000  # characters of a page per llms_fetch call
    llms_hit_chars: int = 2_000  # characters of one match in llms_search
    llms_max_hits: int = 20  # matches per llms_search call
    llms_status_ttl: float = 86400.0  # seconds for which a source check counts as fresh

    # ── paths ───────────────────────────────────────────────────────────────────
    # State lives in the tiered store (see `core/store`); tier roots are the properties
    # below and aren't configurable (they follow XDG). Directory with sudo passwords:
    # `<alias>.secret`, mode 0600.
    secret_dir: Path = Field(default_factory=lambda: Path.home() / ".ssh")

    # ── managing ~/.ssh/config ──────────────────────────────────────────────────
    # The main ssh config and the managed file where add_host writes its Host blocks:
    # the managed file is wired into the main one via `Include` (once). Kept as separate
    # settings so tests don't touch the real ~/.ssh.
    ssh_config_file: Path = Field(default_factory=lambda: Path.home() / ".ssh" / "config")
    managed_config_file: Path = Field(default_factory=lambda: Path.home() / ".ssh" / "config.d" / "mcp.conf")
    known_hosts_file: Path = Field(default_factory=lambda: Path.home() / ".ssh" / "known_hosts")
    copy_id_timeout: float = 30.0  # seconds for ssh-copy-id (logging in and writing the key to the host)

    # Hosts where sudo requires a terminal: ssh runs with `-tt` for them.
    # In the environment this is a JSON list: HOSTOPS_MCP_PTY_HOSTS='["a","b"]'.
    pty_hosts: frozenset[str] = frozenset()

    # The debug log file; without it, logging is disabled (see `logger`).
    debug_log: Path | None = None

    @property
    def runtime_dir(self) -> Path:
        """Runtime tier root: `XDG_RUNTIME_DIR` (tmpfs), or a uid-scoped temp dir; until reboot."""
        return _runtime_dir()

    @property
    def cache_dir(self) -> Path:
        """Persistent tier root: `XDG_CACHE_HOME`; survives a reboot (the OS may clear a cache)."""
        return _xdg("XDG_CACHE_HOME", Path.home() / ".cache")

    def secret_file(self, alias: str) -> Path:
        """The sudo password file for an alias."""
        return self.secret_dir / f"{alias}.secret"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The single settings instance."""
    return Settings()
