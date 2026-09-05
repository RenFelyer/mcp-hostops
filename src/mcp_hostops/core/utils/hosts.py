"""Hosts from `~/.ssh/config` and their parameters as ssh itself sees them.

Aliases are read from the config and its `Include` files in order of
appearance; each host's parameters come from `ssh -G`, which expands
inheritance from `Host *`, `Include`, and `Match` the same way a real
connection would. The config is the only source of truth: `user@host`-style
addresses that bypass it are not accepted by the server.

The config path is not configurable: `ssh -G` and the connections themselves
always read `~/.ssh/config`, and listing hosts from one file while connecting
through another would be dishonest.
"""

import subprocess
from pathlib import Path

import anyio.to_thread
from pydantic import ValidationError

from ..config.constants import SSH_CONFIG, SSH_DIR
from ..config.environment import get_settings
from ..errors import UserError
from ..schemas import Host
from .parallel import fan_out
from .ssh import run_sync


def pairs(text: str) -> dict[str, str]:
    """Turn "key value" lines (split on the first space) into a dict."""
    return {key: value for key, _, value in (line.partition(" ") for line in text.splitlines())}


def _hidden_by_wildcard(pattern: Path, match: Path) -> bool:
    """Whether the component a wildcard matched starts with a dot.

    `Path.glob` picks these up, but ssh's glob(3) does not; we match ssh's behavior.
    """
    return any(p != m and m.startswith(".") for p, m in zip(pattern.parts, match.parts, strict=False))


def _expand_include(pattern: str) -> list[Path]:
    """Expand an `Include` directive's path into existing files.

    `~` and absolute paths are used as-is; relative ones are resolved against
    `~/.ssh` (ssh's rule for the user config). Wildcards are expanded.
    """
    if pattern.startswith("~"):
        base = Path(pattern).expanduser()
    elif pattern.startswith("/"):
        base = Path(pattern)
    else:
        base = SSH_DIR / pattern
    matches = Path(base.anchor).glob(str(base.relative_to(base.anchor)))
    return sorted(m for m in matches if not _hidden_by_wildcard(base, m))


def _is_alias(word: str) -> bool:
    """Whether this is a host name, not a wildcard (`*`, `?`) or a negation (`!`)."""
    return not word.startswith("!") and "*" not in word and "?" not in word


def _scan_aliases(config: Path, names: list[str], seen: set[Path]) -> None:
    """Add Host names from the file and its `Include`s; cycles are cut off via `seen`."""
    try:
        resolved = config.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        # ssh accepts both `Host x` and `Host=x`; it drops a trailing `#`.
        words = line.partition("#")[0].replace("=", " ", 1).split()
        if len(words) <= 1:
            continue
        keyword = words[0].lower()
        if keyword == "host":
            names += filter(_is_alias, words[1:])
        elif keyword == "include":
            for pattern in words[1:]:
                for included in _expand_include(pattern):
                    _scan_aliases(included, names, seen)


def read_aliases(config: Path = SSH_CONFIG) -> list[str]:
    """Host names from the config and its `Include` files.

    An unreadable config yields an empty list; no I/O errors propagate.

    Args:
        config: Config file; defaults to `~/.ssh/config`.

    Returns:
        Names in order of appearance, without duplicates, wildcards, or negations.
    """
    names: list[str] = []
    _scan_aliases(config, names, set())
    return list(dict.fromkeys(names))


def config_files(config: Path = SSH_CONFIG) -> set[Path]:
    """Files ssh would read for the config, with `Include` expanded.

    Needed to tell whether a managed file is already included: if it's among
    these paths, we don't add a second `Include`.
    """
    names: list[str] = []
    seen: set[Path] = set()
    _scan_aliases(config, names, seen)
    return seen


def resolve(alias: str, timeout: float) -> Host | None:
    """Host parameters as ssh sees them.

    `ssh -G` expands inheritance from `Host *`, `Include`, and `Match`. The
    `--` before the alias keeps a name starting with `-` from being parsed as
    an ssh option.

    Returns:
        The host, or None if ssh didn't respond, failed, exceeded `timeout`, or
        printed something a `Host` can't be built from.
    """
    try:
        done = run_sync(["ssh", "-G", "--", alias], timeout)
        # `ssh -G` prints unindented and always in lowercase; the model drops
        # extra keys, and a missing or malformed port is an error.
        fields = pairs(done.stdout) if done.returncode == 0 else {}
        return Host.model_validate({"alias": alias, "proxyjump": "", **fields})
    except (OSError, subprocess.SubprocessError, ValidationError):
        return None


def _resolve_all(aliases: list[str], timeout: float) -> dict[str, Host]:
    """Run `ssh -G` for each alias in parallel; unparsed aliases are left out of the result."""
    resolved = fan_out(lambda alias: resolve(alias, timeout), aliases)
    return {host.alias: host for host in resolved if host}


def resolve_known(aliases: list[str], timeout: float) -> dict[str, Host]:
    """Parameters for the aliases in `aliases` that are described in the config.

    `ssh -G` succeeds for any name, so config membership is the only way to
    tell our alias apart from an arbitrary one. Foreign and unparsed aliases
    are left out of the result.
    """
    known = set(read_aliases())
    return _resolve_all([alias for alias in dict.fromkeys(aliases) if alias in known], timeout)


def discover(timeout: float) -> list[Host]:
    """Config hosts with their parameters already resolved, in config order."""
    return list(_resolve_all(read_aliases(), timeout).values())


async def require_host(alias: str) -> Host:
    """Host by alias; the common entry point for all routers, resolution runs in a thread.

    Raises:
        UserError: the alias is not in `~/.ssh/config` — the server only works
            with hosts from the config.
    """
    timeout = get_settings().ssh_g_timeout
    hosts = await anyio.to_thread.run_sync(resolve_known, [alias], timeout)
    if alias not in hosts:
        raise UserError(f"host {alias!r} is not described in ~/.ssh/config")
    return hosts[alias]
