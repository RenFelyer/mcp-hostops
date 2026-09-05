"""Handlers of the ~/.ssh/config management router: add_host, remove_host, forget_host, copy_id.

Services are synchronous (files, ssh -G, ssh-keygen, ssh-copy-id) — handlers move
them to a thread. Client hints differ per tool: all of them edit local files
(except copy_id, which also connects to the host), so there are no shared presets here.
"""

import anyio.to_thread
from fastmcp import FastMCP
from mcp_types import ToolAnnotations

from ...core.schemas import NonEmptyStr, Port
from . import services
from .schemas import AddHostResult, CopyIdResult, ForgetHostResult, ManagedHost, RemoveHostResult

router: FastMCP = FastMCP(name="sshconfig", on_duplicate="error")

# Edits only local files, no network (ssh -G is local); applying the same input
# twice lands the same state. Shared by add_host (managed block) and forget_host
# (known_hosts) — what each one edits is spelled out in its docstring.
_EDITS_LOCAL_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)


@router.tool(title="Add host", tags={"sshconfig"}, annotations=_EDITS_LOCAL_IDEMPOTENT)
async def add_host(
    alias: NonEmptyStr,
    hostname: NonEmptyStr,
    user: str = "",
    port: Port = 22,
    identity_file: str = "",
    proxy_jump: str = "",
    extra: dict[str, str] | None = None,
) -> AddHostResult:
    """Add a host to ~/.ssh/config via the server's managed file.

    The block is written in canonical form to a separate file, wired into the
    main config via Include; the manual config is not rewritten. An existing
    managed block for the same alias is replaced; an alias described manually is taken.

    Args:
        alias: Host name for ssh (`ssh <alias>`); no spaces and no * ? # !.
        hostname: Host address or domain name (HostName).
        user: Login user; empty — don't write User.
        port: ssh port.
        identity_file: Path to the private key (IdentityFile); empty — don't write it.
        proxy_jump: Alias of the jump host (ProxyJump); empty — direct connection.
        extra: Other ssh options as "Key: Value", written into the block as-is.
    """
    spec = ManagedHost(
        alias=alias,
        hostname=hostname,
        user=user,
        port=port,
        identity_file=identity_file,
        proxy_jump=proxy_jump,
        extra=extra or {},
    )
    return await anyio.to_thread.run_sync(services.add_host, spec)


@router.tool(
    title="Remove host",
    tags={"sshconfig"},
    # Removes the managed block and, per flags, known_hosts entries and the secret;
    # a repeat call won't find the host and will fail — hence not idempotent.
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
    ),
)
async def remove_host(alias: NonEmptyStr, forget_known: bool = True, drop_secret: bool = False) -> RemoveHostResult:
    """Remove a host from the managed file and clean up its trace.

    Touches only entries added by the server: a host from the manual config is
    an error. By default also cleans known_hosts.

    Args:
        alias: Alias previously added by add_host.
        forget_known: Also remove this host's entries from known_hosts.
        drop_secret: Also remove the ~/.ssh/<alias>.secret file with the sudo password.
    """
    return await anyio.to_thread.run_sync(services.remove_host, alias, forget_known, drop_secret)


@router.tool(title="Forget host key", tags={"sshconfig"}, annotations=_EDITS_LOCAL_IDEMPOTENT)
async def forget_host(target: NonEmptyStr) -> ForgetHostResult:
    """Remove known_hosts entries for a host without touching the config.

    For the "Remote host identification has changed" case: the next connection
    will accept the new key. The config and secrets are left in place.

    Args:
        target: Alias from the config (its hostname is cleaned) or the hostname/IP itself.
    """
    return await anyio.to_thread.run_sync(services.forget_host, target)


@router.tool(
    title="Install key on host",
    tags={"sshconfig"},
    # Connects to the host and edits its authorized_keys; ssh-copy-id skips a key
    # that's already installed, so repeating is safe.
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
    ),
)
async def copy_id(alias: NonEmptyStr, identity: str = "") -> CopyIdResult:
    """Install a public key on the host (ssh-copy-id); the password comes from the secret.

    The password is taken from ~/.ssh/<alias>.secret and passed to the host via
    sshpass, without landing in argv or logs; ssh-copy-id and sshpass must be
    installed. After this, login proceeds by key, and sudo uses the same secret.

    Args:
        alias: Alias from ~/.ssh/config.
        identity: Path to the public key (-i); empty — the default key.
    """
    return await anyio.to_thread.run_sync(services.copy_id, alias, identity)
