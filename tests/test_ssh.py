"""Building the remote script, stdin, and ControlMaster options — pure logic."""

from pathlib import Path

import pytest

from mcp_hostops.core.config.environment import Settings
from mcp_hostops.core.schemas import Host
from mcp_hostops.core.utils.ssh import (
    Invocation,
    Output,
    build_stdin,
    control_args,
    remote_script,
    ssh_argv,
)


def test_remote_script_plain() -> None:
    assert remote_script("ls -la", "/srv/app", prime=False) == "cd -- /srv/app && ls -la"


def test_remote_script_quotes_cwd() -> None:
    got = remote_script("ls", "/srv/with space", prime=False)
    assert got == "cd -- '/srv/with space' && ls"


def test_remote_script_home_unquoted() -> None:
    # `~` and `~/…` must be expanded by the host shell, so the tilde is unquoted.
    assert remote_script("pwd", "~", prime=False) == "cd -- ~ && pwd"
    assert remote_script("ls", "~/my dir", prime=False) == "cd -- ~/'my dir' && ls"
    assert remote_script("ls", "~user", prime=False) == "cd -- '~user' && ls"


def test_remote_script_primes_sudo() -> None:
    got = remote_script("sudo reboot", "/root", prime=True)
    # The shell reads the first stdin line and pipes it to a single `sudo -v`:
    # sudo gets exactly one line, the password isn't in argv, `-k` clears the ticket.
    assert got == (
        "cd -- /root && IFS= read -r __sudo_pw && printf '%s\\n' \"$__sudo_pw\" | sudo -k -S -p '' -v "
        "&& unset __sudo_pw && sudo reboot"
    )


def test_build_stdin_password_first() -> None:
    assert build_stdin("pw", "data") == b"pw\ndata"


def test_build_stdin_no_password() -> None:
    assert build_stdin(None, "x") == b"x"
    assert build_stdin(None, None) == b""


def test_control_args_opts_and_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    s = Settings()
    args = control_args(s)
    assert "ControlMaster=auto" in args
    assert any(a.startswith("ControlPath=") for a in args)
    assert s.control_dir.is_dir()  # socket directory was created
    assert s.control_dir.stat().st_mode & 0o777 == 0o700


def test_control_args_refuse_shared_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A foreign or world-open socket directory is refused: the sudo password travels over the socket.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    s = Settings()
    s.control_dir.mkdir(parents=True)
    s.control_dir.chmod(0o755)
    with pytest.raises(PermissionError):
        control_args(s)


def test_ssh_argv_separates_alias_from_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    host = Host(alias="-oProxyCommand=evil", hostname="h", user="u", port=22, proxyjump="")
    argv = ssh_argv(host, Settings(), tty=True)
    assert argv[-3:] == ["-tt", "--", "-oProxyCommand=evil"]
    assert "BatchMode=yes" in argv


def test_output_decodes_utf8_across_reads() -> None:
    # A multi-byte character split by a read boundary is glued back together, not corrupted.
    out = Output(100)
    data = "áéíóú".encode()
    out.feed(data[:3])
    first = out.text(None, final=False)
    out.feed(data[3:])
    second = out.text(None, final=True)
    assert first + second == "áéíóú"


def test_output_holds_partial_line_while_masking() -> None:
    # A password split by a read boundary must not go out in two halves:
    # without `final`, an incomplete line waits for its continuation.
    out = Output(100)
    out.feed(b"ok\ns3c")
    assert out.text("s3cret", final=False) == "ok\n"
    out.feed(b"ret\r\ntail")
    assert out.text("s3cret", final=False) == "***\n"
    assert out.text("s3cret", final=True) == "tail"


def test_output_streams_partial_line_without_password() -> None:
    out = Output(100)
    out.feed(b"progress 50%")
    assert out.text(None, final=False) == "progress 50%"


def test_invocation_repr_hides_password() -> None:
    call = Invocation(argv=["ssh"], stdin=b"s3cret\n", password="s3cret")
    assert "s3cret" not in repr(call)
    assert "s3cret" not in str(call)
