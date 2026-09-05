"""Types and vocabularies shared across all routers.

This is where values live that appear in more than one router, or that are shared
between a router and the infrastructure: host, availability status, sudo mode, captured
command output, the `llms.txt` source (its built-in registry lives in constants), and
preset client hints for tools.
"""

from typing import Annotated, Literal

from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

# A string tool parameter for which an empty string is a call error.
NonEmptyStr = Annotated[str, Field(min_length=1)]

# Host availability: the status value in the hosts router's responses and in probes.
Availability = Literal["available", "unavailable", "unknown"]

# sudo mode in run/start.
SudoMode = Literal["auto", "true", "false"]

# Client hints shared by several tools; all four are set explicitly because the MCP
# defaults (destructive and open_world — true) are almost always wrong. A tool with a
# unique set describes it in its own module.
READS_REMOTE = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
READS_LOCAL = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
# An arbitrary command on the host: it can change and delete anything.
RUNS_REMOTE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)


class Host(BaseModel):
    """A host from ~/.ssh/config with parameters as seen by `ssh -G`."""

    model_config = ConfigDict(frozen=True)

    alias: str
    hostname: str
    user: str
    port: int
    proxyjump: str = Field(description="jump host alias; empty if the connection is direct")


class CapturedOutput(BaseModel):
    """Command output with flags marking whether the buffer cap was exceeded."""

    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


class Checked(BaseModel):
    """A response whose data may have been obtained before this call."""

    checked_ago: float = Field(description="data age in seconds; 0 means obtained by this call")


class KnownSource(BaseModel):
    """A `llms.txt` source: where the index is and what it covers."""

    model_config = ConfigDict(frozen=True)

    domain: str = Field(description="how to name the source in llms_index/llms_search")
    index: str = Field(description="the `llms.txt` address")
    covers: str
    # We don't store the llms-full.txt address — it's derived from index; its size can't be
    # guessed from that, and full can be tens of MB, so we keep the size as a hint.
    full_size: int | None = Field(default=None, description="llms-full.txt size in bytes; null means no full file")
    default: bool = Field(default=False, description="built-in; cannot be removed")


class Link(BaseModel):
    """One entry for the shared markdown template (`core/template`)."""

    model_config = ConfigDict(frozen=True)

    title: str
    url: str
    description: str = ""
    section: str = ""


class Invocation(BaseModel):
    """A ready-to-run ssh invocation: argv, the stdin payload, and the password for masking.

    stdin and the password are hidden from `repr`: an exception's text or a
    debug log entry with the invocation must not reveal the sudo password.
    """

    argv: list[str]
    stdin: bytes = Field(repr=False)
    password: str | None = Field(repr=False)


class Stamped(BaseModel):
    """A stored record with a timestamp; the remaining fields are the content."""

    model_config = ConfigDict(extra="allow")

    checked_at: float
