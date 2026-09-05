"""Service of the ~/.ssh/config management router: managed file, known_hosts, ssh-copy-id.

The server owns a separate managed file (`managed_config_file`) and wires it into
the main config via an `Include` directive — once, with an absolute path. The manual
`~/.ssh/config` is never rewritten: add_host/remove_host only touch the managed
file, which is always written in canonical form (`Host`, four-space indent, key
order from `MANAGED_KEY_ORDER`). This keeps the config standard and appendable,
while manual edits stay untouched.

The service resolves the alias and settings itself (`get_settings`, `resolve`);
handlers move synchronous file and subprocess work to a thread. Errors to the
client are `UserError`. The password for ssh-copy-id is the same
`~/.ssh/<alias>.secret` used for sudo: it is passed to the host via `sshpass -f`,
bypassing argv and logs.
"""

import re
import shutil
import subprocess
from pathlib import Path

from ...core.config.constants import (
    MANAGED_HEADER,
    MANAGED_KEY_ORDER,
    PRIVATE_DIR_MODE,
    SECRET_FILE_MODE,
    SSH_DEFAULT_PORT,
)
from ...core.config.environment import Settings, get_settings
from ...core.errors import UserError
from ...core.schemas import Host
from ...core.store import write_bytes
from ...core.utils.hosts import config_files, read_aliases, resolve
from ...core.utils.ssh import run_sync
from ...core.utils.sudo import mask, read_secret
from .schemas import AddHostResult, CopyIdResult, ForgetHostResult, ManagedHost, RemoveHostResult, TrustHostResult

# Line opening a Host block: keyword `Host`, form `Host x` or `Host=x`.
_HOST_LINE = re.compile(r"(?i)^\s*host[\s=]")

# Managed-file block: aliases from the `Host` line and the block's full text.
Block = tuple[list[str], str]


def _check_alias(alias: str) -> None:
    """Check that the alias is fit to be written into the config.

    Raises:
        UserError: empty, contains whitespace, is one of `* ? # !`, or starts with `-`.
    """
    if not alias or any(ch.isspace() for ch in alias) or alias[0] in "-!" or any(ch in alias for ch in "*?#"):
        raise UserError(f"alias {alias!r}: no spaces and no * ? # !, must not start with -")


def _parse_blocks(text: str) -> list[Block]:
    """Parse the managed file into Host blocks; everything outside blocks (the header) is dropped."""
    blocks: list[Block] = []
    aliases: list[str] | None = None
    buf: list[str] = []
    for raw in text.splitlines():
        if _HOST_LINE.match(raw):
            if aliases is not None:
                blocks.append((aliases, "\n".join(buf).rstrip() + "\n"))
            aliases = raw.partition("#")[0].replace("=", " ", 1).split()[1:]
            buf = [raw]
        elif aliases is not None:
            buf.append(raw)
    if aliases is not None:
        blocks.append((aliases, "\n".join(buf).rstrip() + "\n"))
    return blocks


def _render_block(host: ManagedHost) -> str:
    """Build the canonical Host block: header and options with four-space indent."""
    named = {
        "HostName": host.hostname,
        "User": host.user,
        "Port": str(host.port),
        "IdentityFile": host.identity_file,
        "ProxyJump": host.proxy_jump,
    }
    lines = [f"Host {host.alias}"]
    lines += [f"    {key} {named[key]}" for key in MANAGED_KEY_ORDER if named[key]]
    lines += [f"    {key} {value}" for key, value in host.extra.items()]
    return "\n".join(lines) + "\n"


def _render_file(blocks: list[Block]) -> bytes:
    """The managed file as a whole: header and blocks separated by a blank line."""
    return "".join([MANAGED_HEADER, *(f"\n{text}" for _, text in blocks)]).encode()


def _read_blocks(s: Settings) -> list[Block]:
    """Blocks of the managed file; a missing or unreadable file — empty."""
    try:
        return _parse_blocks(s.managed_config_file.read_text(encoding="utf-8"))
    except OSError:
        return []


def _write_blocks(blocks: list[Block], s: Settings) -> None:
    """Atomically rewrite the managed file and lock its permissions down to 0600."""
    write_bytes(s.managed_config_file, _render_file(blocks))
    s.managed_config_file.chmod(SECRET_FILE_MODE)


def _ensure_available(s: Settings) -> bool:
    """Create the managed file and wire it into the main config.

    Returns:
        True — the `Include` line was added by this call; False — it was already there.
    """
    s.managed_config_file.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    if not s.managed_config_file.exists():
        _write_blocks([], s)
    if s.managed_config_file.resolve() in config_files(s.ssh_config_file):
        return False
    # Absolute path: a relative one would be resolved by ssh from ~/.ssh, not from
    # this config's directory. Prepended so a specific Host wins over a general `Host *`.
    include = f"Include {s.managed_config_file}\n".encode()
    s.ssh_config_file.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    try:
        existing = s.ssh_config_file.read_bytes()
    except OSError:
        existing = b""
    write_bytes(s.ssh_config_file, include + existing)
    return True


def add_host(spec: ManagedHost) -> AddHostResult:
    """Write a Host block to the managed file and return how ssh -G now sees it.

    An existing managed block for the same alias is replaced; an alias already
    described manually in the config is left untouched — otherwise one alias
    would end up with two Host entries.

    Raises:
        UserError: the alias is invalid, hostname is empty, or the alias is taken
            by a manual entry.
        OSError: the config or managed file could not be written.
    """
    _check_alias(spec.alias)
    if not spec.hostname.strip():
        raise UserError("hostname is empty")
    s = get_settings()
    include_added = _ensure_available(s)
    blocks = _read_blocks(s)
    managed = {alias for aliases, _ in blocks for alias in aliases}
    if spec.alias in read_aliases(s.ssh_config_file) and spec.alias not in managed:
        raise UserError(f"alias {spec.alias!r} is already described manually in the config")
    kept = [(aliases, text) for aliases, text in blocks if spec.alias not in aliases]
    kept.append(([spec.alias], _render_block(spec)))
    _write_blocks(kept, s)
    return AddHostResult(
        alias=spec.alias,
        config_file=str(s.managed_config_file),
        include_added=include_added,
        host=resolve(spec.alias, s.ssh_g_timeout),
    )


def remove_host(alias: str, forget_known: bool, drop_secret: bool) -> RemoveHostResult:
    """Remove the alias's managed block and, per flags, its known_hosts entries and secret.

    Raises:
        UserError: the alias isn't described in the managed file (manual entries
            are left untouched).
        OSError: the managed file could not be written.
    """
    s = get_settings()
    blocks = _read_blocks(s)
    if not any(alias in aliases for aliases, _ in blocks):
        raise UserError(f"host {alias!r} is not in the managed file; remove_host does not touch manual entries")
    host = resolve(alias, s.ssh_g_timeout)  # while the block is still in place — get the hostname for known_hosts
    _write_blocks([(aliases, text) for aliases, text in blocks if alias not in aliases], s)
    removed = _forget(host, s) if forget_known and host is not None else 0
    dropped = _drop_secret(alias, s) if drop_secret else False
    return RemoveHostResult(alias=alias, known_hosts_removed=removed, secret_removed=dropped)


def forget_host(target: str) -> ForgetHostResult:
    """Remove known_hosts entries for a host without touching the config.

    `target` is an alias from the config (in which case we clean its hostname)
    or the hostname/IP itself. Needed when the host key changed
    ("Remote host identification has changed").
    """
    s = get_settings()
    host = resolve(target, s.ssh_g_timeout)
    return ForgetHostResult(
        target=host.hostname if host is not None else target,
        known_hosts_file=str(s.known_hosts_file),
        removed=_forget(host, s) if host is not None else _keygen_remove([target], s.known_hosts_file),
    )


def trust_host(target: str) -> TrustHostResult:
    """Fetch a host's keys with ssh-keyscan and add them to known_hosts.

    The inverse of forget_host: afterwards a non-interactive ssh to the host
    won't stop on the trust prompt. `target` is an alias from the config (its
    hostname and port are scanned) or a hostname/IP. Any existing entries for
    the host are removed first, so re-trusting after a key change leaves no
    duplicates. A host that returns no keys leaves known_hosts untouched.

    Raises:
        UserError: ssh-keyscan is missing or exceeded the timeout.
    """
    s = get_settings()
    if shutil.which("ssh-keyscan") is None:
        raise UserError("ssh-keyscan not found")
    host = resolve(target, s.ssh_g_timeout)
    hostname = host.hostname if host is not None else target
    port = host.port if host is not None else SSH_DEFAULT_PORT
    argv = ["ssh-keyscan", "-T", str(max(1, int(s.keyscan_timeout)))]
    if port != SSH_DEFAULT_PORT:
        argv += ["-p", str(port)]
    argv.append(hostname)
    try:
        done = run_sync(argv, s.keyscan_timeout + 1)
    except subprocess.TimeoutExpired as err:
        raise UserError(f"ssh-keyscan: timeout after {s.keyscan_timeout}s") from err
    except OSError as err:
        raise UserError(f"ssh-keyscan failed to start: {err}") from err
    # ssh-keyscan writes host-key lines to stdout and `#` diagnostics to stderr.
    keys = [line for line in done.stdout.splitlines() if line.strip() and not line.startswith("#")]
    if keys:
        if host is not None:
            _forget(host, s)  # drop old entries (hostname and [host]:port) so re-trust doesn't duplicate
        else:
            _keygen_remove([hostname], s.known_hosts_file)
        _append_known_hosts(keys, s.known_hosts_file)
    return TrustHostResult(target=hostname, known_hosts_file=str(s.known_hosts_file), added=len(keys))


def _append_known_hosts(lines: list[str], known_file: Path) -> None:
    """Append known_hosts entries, creating the file (and ~/.ssh) if needed."""
    known_file.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    with known_file.open("a", encoding="utf-8") as file:
        file.writelines(f"{line}\n" for line in lines)


def copy_id(alias: str, identity: str) -> CopyIdResult:
    """Install a public key on the host via ssh-copy-id; the password comes from the secret.

    The host password is taken from `~/.ssh/<alias>.secret` and passed to
    `sshpass -f`, landing neither in argv nor in logs. The host key is accepted
    on first connection (`StrictHostKeyChecking=accept-new`), otherwise a
    non-interactive ssh would hang on the trust prompt.

    Raises:
        UserError: the alias isn't in the config, ssh-copy-id or sshpass is
            missing, the secret is unavailable, or ssh-copy-id exceeded the timeout.
    """
    s = get_settings()
    if alias not in read_aliases(s.ssh_config_file):
        raise UserError(f"host {alias!r} is not described in ~/.ssh/config")
    for tool in ("ssh-copy-id", "sshpass"):
        if shutil.which(tool) is None:
            raise UserError(f"{tool} is required: the host password is taken from ~/.ssh/<alias>.secret")
    password = read_secret(alias, s)  # validates that the secret exists and has the right permissions
    argv = ["sshpass", "-f", str(s.secret_file(alias)), "ssh-copy-id", "-o", "StrictHostKeyChecking=accept-new"]
    if identity:
        argv += ["-i", identity]
    argv.append(alias)
    try:
        done = run_sync(argv, s.copy_id_timeout)
    except subprocess.TimeoutExpired as err:
        raise UserError(f"ssh-copy-id: timeout after {s.copy_id_timeout}s") from err
    except OSError as err:
        raise UserError(f"ssh-copy-id failed to start: {err}") from err
    lines = [line for line in mask(done.stdout + done.stderr, password).splitlines() if line.strip()]
    return CopyIdResult(alias=alias, ok=done.returncode == 0, detail=lines[-1] if lines else "")


def _drop_secret(alias: str, s: Settings) -> bool:
    """Remove the `~/.ssh/<alias>.secret` file; False — it wasn't there."""
    try:
        s.secret_file(alias).unlink()
    except OSError:
        return False
    return True


def _forget(host: Host, s: Settings) -> int:
    """Remove the host's known_hosts entries: by hostname and, if the port is non-default, `[host]:port`."""
    names = [host.hostname]
    if host.port != SSH_DEFAULT_PORT:
        names.append(f"[{host.hostname}]:{host.port}")
    return _keygen_remove(names, s.known_hosts_file)


def _count_lines(path: Path) -> int:
    try:
        return len(path.read_bytes().splitlines())
    except OSError:
        return 0


def _keygen_remove(names: list[str], known_file: Path) -> int:
    """`ssh-keygen -R` for each name; return by how many lines the file shrank.

    ssh-keygen parses hashed entries itself and rewrites the file; how many were
    removed is counted from the line-count difference (it also drops an `.old` file alongside).

    Raises:
        UserError: ssh-keygen not found.
    """
    if shutil.which("ssh-keygen") is None:
        raise UserError("ssh-keygen not found")
    if not known_file.exists():
        return 0
    before = _count_lines(known_file)
    for name in names:
        try:
            run_sync(["ssh-keygen", "-R", name, "-f", str(known_file)], 10.0)
        except (OSError, subprocess.SubprocessError):
            continue
    return before - _count_lines(known_file)
