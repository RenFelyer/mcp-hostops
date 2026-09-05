"""Low-level ssh primitives shared by the commands and jobs routers and by probes.

Connections are reused via ControlMaster (a socket in the private runtime
directory); for hosts in `pty_hosts`, ssh runs with `-tt`. Login is
non-interactive (`BatchMode=yes`) — authentication is key-only; a password on
stdin is a sudo password, not a login one. The host shell is assumed to be
POSIX-compatible: the remote script uses `&&`, `read -r`, and `printf`.
"""

import codecs
import contextlib
import logging
import shlex
import subprocess
from collections.abc import AsyncGenerator

import anyio
import anyio.abc
from pydantic import BaseModel, Field

from ..config.constants import SUDO_PRIME
from ..config.environment import Settings
from ..schemas import CapturedOutput, Host, SudoMode
from ..store import private_dir
from .sudo import decide_prime, mask, read_secret

log = logging.getLogger(__name__)


def run_sync(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """A local process with no stdin, capturing output; launch errors propagate."""
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def control_args(s: Settings) -> list[str]:
    """ControlMaster options for connection reuse.

    Raises:
        PermissionError: the socket directory belongs to someone else or is
            open to others — a socket that carries commands and the sudo
            password cannot live there.
    """
    control_dir = private_dir(s.control_dir)
    options = [
        "ControlMaster=auto",
        f"ControlPath={control_dir}/%C",
        f"ControlPersist={s.control_persist}",
    ]
    return [arg for option in options for arg in ("-o", option)]


def ssh_argv(host: Host, s: Settings, *, tty: bool = False) -> list[str]:
    """Base ssh invocation with ControlMaster, without a command; `tty` adds `-tt`.

    `--` before the alias: a config name starting with `-` won't become an option.
    """
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={max(1, int(s.connect_timeout))}",
        *control_args(s),
    ]
    if tty:
        argv.append("-tt")
    argv += ["--", host.alias]
    return argv


def _quote_cwd(cwd: str) -> str:
    """Escape cwd, letting `~` and `~/` be expanded by the shell."""
    if cwd == "~":
        return "~"
    if cwd.startswith("~/"):
        return "~/" + shlex.quote(cwd[2:])
    return shlex.quote(cwd)


def remote_script(command: str, cwd: str, prime: bool) -> str:
    """Script for the host: cd into cwd, prime sudo if needed, then the command."""
    parts = [f"cd -- {_quote_cwd(cwd)}"]
    if prime:
        parts.append(SUDO_PRIME)
    parts.append(command)
    return " && ".join(parts)


def build_stdin(password: str | None, user_stdin: str | None) -> bytes:
    """The stdin payload: sudo password as the first line, then the user's own."""
    payload = b""
    if password is not None:
        payload += (password + "\n").encode()
    if user_stdin:
        payload += user_stdin.encode()
    return payload


class Invocation(BaseModel):
    """A ready-to-run invocation: argv, the stdin payload, and the password for masking.

    stdin and the password are hidden from `repr`: an exception's text or a
    debug log entry with the invocation must not reveal the sudo password.
    """

    argv: list[str]
    stdin: bytes = Field(repr=False)
    password: str | None = Field(repr=False)


def prepare(
    host: Host,
    command: str,
    cwd: str,
    sudo_mode: SudoMode,
    user_stdin: str | None,
    s: Settings,
) -> Invocation:
    """Build the command invocation for the host.

    Raises:
        SudoError: a password is needed, but the file is missing or has unsafe
            permissions.
        PermissionError: the ControlMaster socket directory belongs to someone else.
    """
    prime = decide_prime(command, sudo_mode)
    password = read_secret(host.alias, s) if prime else None
    argv = [
        *ssh_argv(host, s, tty=host.alias in s.pty_hosts),
        remote_script(command, cwd, prime),
    ]
    return Invocation(argv=argv, stdin=build_stdin(password, user_stdin), password=password)


class Output:
    """A single stream's buffer with a cap: excess is dropped, truncation is remembered.

    The decoder is incremental: a UTF-8 character split by a read boundary
    isn't turned into garbage but is glued back together on the next read.
    When there's something to mask, an incomplete last line is likewise held
    back: a password split into two chunks by a read boundary wouldn't be
    recognized by the line-based mask.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""

    def feed(self, chunk: bytes) -> None:
        """Append a chunk without exceeding the cap."""
        kept = chunk[: max(0, self.limit - len(self.data))]
        self.data += kept
        if len(kept) < len(chunk):
            self.truncated = True

    def take(self) -> bytes:
        """Take what's accumulated and start over with an empty buffer (chunked reading)."""
        data, self.data = self.data, bytearray()
        return bytes(data)

    def text(self, password: str | None, *, final: bool) -> str:
        """Take what's accumulated as text with the password masked.

        `final` — there will be no more output: an unfinished tail is given as
        is, and an incomplete character is replaced.
        """
        text = self._pending + self._decoder.decode(self.take(), final)
        self._pending = ""
        if password and not final:
            text, newline, self._pending = text.rpartition("\n")
            text += newline
        return mask(text, password)


class Capture:
    """Both of a process's streams and the password that must be masked in them."""

    def __init__(self, limit: int, password: str | None) -> None:
        self.stdout = Output(limit)
        self.stderr = Output(limit)
        self.password = password

    def drained(self, *, final: bool = True) -> CapturedOutput:
        """Take what's accumulated; the buffers are empty afterward, truncation flags remain."""
        return CapturedOutput(
            stdout=self.stdout.text(self.password, final=final),
            stderr=self.stderr.text(self.password, final=final),
            stdout_truncated=self.stdout.truncated,
            stderr_truncated=self.stderr.truncated,
        )


async def _feed_stdin(proc: anyio.abc.Process, payload: bytes) -> None:
    """Send the payload and close stdin so a reading command sees EOF.

    The process may close stdin before we're done writing (or not survive to
    the write at all): that's not an invocation error but its outcome — it
    shows up in the exit code.
    """
    if proc.stdin is None:
        return
    try:
        if payload:
            await proc.stdin.send(payload)
    except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
        pass
    finally:
        await proc.stdin.aclose()


async def _drain(stream: anyio.abc.ByteReceiveStream | None, out: Output) -> None:
    """Read the stream into `out` to the end; on cancellation, what was read stays in the buffer."""
    if stream is None:
        return
    async for chunk in stream:
        out.feed(chunk)


@contextlib.asynccontextmanager
async def spawn(call: Invocation) -> AsyncGenerator[anyio.abc.Process]:
    """Start ssh; on exit a still-running process is killed, not waited for.

    External cancellation (timeout, job removal, server shutdown) must not
    wait for the remote command: closing the process alone would not kill it.
    """
    log.debug("launching: %s", call.argv)
    async with await anyio.open_process(call.argv) as proc:
        try:
            yield proc
        finally:
            if proc.returncode is None:
                proc.kill()
            log.debug("finished with code %s", proc.returncode)


async def execute(proc: anyio.abc.Process, call: Invocation, capture: Capture) -> int:
    """Feed stdin, drain both streams to the end, and return the exit code.

    Output is written as it's read, so on cancellation (timeout, removal) the
    buffers retain everything received so far.
    """
    async with anyio.create_task_group() as tg:
        tg.start_soon(_feed_stdin, proc, call.stdin)
        tg.start_soon(_drain, proc.stdout, capture.stdout)
        tg.start_soon(_drain, proc.stderr, capture.stderr)
    return await proc.wait()
