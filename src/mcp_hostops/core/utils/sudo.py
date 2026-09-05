"""Detecting sudo in a command, reading the password, and masking it.

The host password lives in `~/.ssh/<alias>.secret` (mode 0600, our file) and
is read only at call time, never cached. If the command contains sudo, the
server sends the password as the first stdin line, and the remote script
(`ssh.remote_script`) feeds that line to a single `sudo -v` call, which caches
a ticket; the original command then runs unchanged, and its sudo invocations
pick up the ticket without prompting. In any output, a line equal to the
password is replaced with `***`.

Command parsing is a heuristic for the "decide from the command" mode: we look
for sudo/doas in the verb position, including behind wrappers like `env` or
`timeout` and inside `sh -c '…'`. `$(…)` substitutions and backticks are not
parsed — there, sudo is specified explicitly via the `sudo` parameter.
"""

import os
import shlex
import stat
from collections.abc import Iterator
from pathlib import Path

from ..config.constants import SHELLS, SUDO_MASK, SUDO_WRAPPERS
from ..config.environment import Settings
from ..errors import UserError
from ..schemas import SudoMode


class SudoError(UserError):
    """The sudo password is unavailable: the file is missing or has unsafe permissions."""


def _is_assignment(token: str) -> bool:
    name, sep, _ = token.partition("=")
    return bool(sep) and name.isidentifier()


def _split(text: str) -> list[str]:
    """Tokens per shell rules; on malformed quoting, split on whitespace."""
    try:
        return shlex.split(text, comments=False)
    except ValueError:
        return text.split()


def _simple_commands(script: str) -> Iterator[str]:
    """The script's simple commands: split on `;`, `&&`, `||`, `|`, `&` outside quotes.

    A lone `&` is also a separator (background command), except in the
    redirections `>&`, `<&`, and `&>`.
    """
    quote = ""
    start = 0
    i = 0
    while i < len(script):
        ch = script[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 1
            elif ch == quote:
                quote = ""
        elif ch == "\\":
            i += 1
        elif ch in "'\"":
            quote = ch
        elif ch in ";|&":
            redirect = ch == "&" and (script[i - 1 : i] in ("<", ">") or script[i + 1 : i + 2] == ">")
            if redirect:
                i += 1
                continue
            yield script[start:i]
            while i < len(script) and script[i] in ";|&":
                i += 1
            start = i
            continue
        i += 1
    yield script[start:]


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _verb(tokens: list[str]) -> tuple[str, list[str]]:
    """The verb and its arguments after stripping assignments and wrappers."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _is_assignment(tok):
            i += 1
            continue
        wrapper = SUDO_WRAPPERS.get(_basename(tok))
        if wrapper is None:
            return _basename(tok), tokens[i + 1 :]
        valued, positional = wrapper
        i += 1
        while i < len(tokens) and tokens[i].startswith("-"):
            i += 2 if tokens[i] in valued else 1
        i += positional
    return "", []


def _inline_code(args: list[str]) -> str | None:
    """The code after a shell's `-c`; the flag may be fused (`-lc`, `-xec`).

    `-o` takes a separate value (`bash -o pipefail -c '…'`); other flags don't.
    """
    i = 0
    while i < len(args) and args[i].startswith("-"):
        flag = args[i]
        if flag == "-o":
            i += 2
            continue
        if not flag.startswith("--") and "c" in flag[1:]:
            return args[i + 1] if i + 1 < len(args) else None
        i += 1
    return None


def uses_sudo(command: str) -> bool:
    """Whether sudo/doas appears in the verb position, including inside `<shell> -c '…'`."""
    for simple in _simple_commands(command):
        verb, args = _verb(_split(simple))
        if verb in ("sudo", "doas"):
            return True
        if verb in SHELLS and (inner := _inline_code(args)) is not None and uses_sudo(inner):
            return True
    return False


def decide_prime(command: str, sudo_mode: SudoMode) -> bool:
    """Whether password priming is needed: auto decides from the command, true/false force it."""
    if sudo_mode == "auto":
        return uses_sudo(command)
    return sudo_mode == "true"


def read_secret(alias: str, s: Settings) -> str:
    """The password from `~/.ssh/<alias>.secret`.

    Only the path, never the content, goes into the error message.

    Raises:
        SudoError: the alias leads the path outside the secrets directory, the
            file doesn't exist or can't be read, it isn't our regular file, or
            its permissions aren't 0600.
    """
    path: Path = s.secret_file(alias)
    # An alias containing `/` or `..` would move the path out of the secrets
    # directory; the comparison doesn't touch disk, so our own symlink to a
    # file elsewhere is fine.
    if path.parent != s.secret_dir:
        raise SudoError(f"alias leads outside the secrets directory: {alias!r}")
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise SudoError(f"the password file must be our own regular file: {path}")
        if info.st_mode & 0o077:
            raise SudoError(f"the password file has unsafe permissions, must be 0600: {path}")
        return path.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as err:
        raise SudoError(f"password file not found: {path} ({err.strerror})") from err


def mask(text: str, password: str | None) -> str:
    """Replace lines that consist exactly of the password with `***`.

    The password can only leak into output as pty echo (its own line), so we
    mask line by line rather than by substring: otherwise a short
    password-as-substring (e.g. `root`) would corrupt normal output.
    """
    if not password:
        return text
    return "\n".join(SUDO_MASK if line.strip("\r") == password else line for line in text.split("\n"))
