"""Schemas of the ~/.ssh/config management router."""

from pydantic import BaseModel, Field

from ...core.schemas import Host


class ManagedHost(BaseModel):
    """Desired Host block for the managed file; the canonical text is built from it."""

    alias: str
    hostname: str
    user: str = ""
    port: int = 22
    identity_file: str = ""
    proxy_jump: str = Field(default="", description="jump host alias")
    extra: dict[str, str] = Field(default_factory=dict, description='other ssh options, "Key Value"')


class AddHostResult(BaseModel):
    """Response of add_host: what was written and how ssh now sees the host."""

    alias: str
    config_file: str = Field(description="managed file the Host block was written to")
    include_added: bool = Field(description="an Include line pointing at the managed file was added to the main config")
    host: Host | None = Field(description="the host as seen by ssh -G after writing; null — ssh -G couldn't parse it")


class RemoveHostResult(BaseModel):
    """Response of remove_host: what was cleaned up along with the Host block."""

    alias: str
    known_hosts_removed: int = Field(description="known_hosts entries removed")
    secret_removed: bool = Field(description="whether the ~/.ssh/<alias>.secret file was removed")


class ForgetHostResult(BaseModel):
    """Response of forget_host: known_hosts cleanup without touching the config."""

    target: str = Field(description="host name used to clean known_hosts")
    known_hosts_file: str
    removed: int = Field(description="entries removed")


class CopyIdResult(BaseModel):
    """Response of copy_id: installing a public key on the host."""

    alias: str
    ok: bool = Field(description="the key was installed (return code 0)")
    detail: str = Field(description="last line of ssh-copy-id output, or the reason for failure")
