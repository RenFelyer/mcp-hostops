"""Сборка удалённого скрипта, stdin и опций ControlMaster — чистая логика."""

from pathlib import Path

import pytest

from mcp_openssh_connector.core.config.environment import Settings
from mcp_openssh_connector.core.schemas import Host
from mcp_openssh_connector.core.utils.ssh import (
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
    got = remote_script("ls", "/срв/с пробелом", prime=False)
    assert got == "cd -- '/срв/с пробелом' && ls"


def test_remote_script_home_unquoted() -> None:
    # `~` и `~/…` должна раскрыть оболочка хоста, поэтому тильда без кавычек.
    assert remote_script("pwd", "~", prime=False) == "cd -- ~ && pwd"
    assert remote_script("ls", "~/my dir", prime=False) == "cd -- ~/'my dir' && ls"
    assert remote_script("ls", "~user", prime=False) == "cd -- '~user' && ls"


def test_remote_script_primes_sudo() -> None:
    got = remote_script("sudo reboot", "/root", prime=True)
    # Первую строку stdin читает оболочка и отдаёт по трубе одному `sudo -v`:
    # sudo получает ровно одну строку, пароля нет в argv, `-k` сбрасывает тикет.
    assert got == (
        "cd -- /root && IFS= read -r __sudo_pw && printf '%s\\n' \"$__sudo_pw\" | sudo -k -S -p '' -v "
        "&& unset __sudo_pw && sudo reboot"
    )


def test_build_stdin_password_first() -> None:
    assert build_stdin("pw", "данные") == b"pw\n\xd0\xb4\xd0\xb0\xd0\xbd\xd0\xbd\xd1\x8b\xd0\xb5"


def test_build_stdin_no_password() -> None:
    assert build_stdin(None, "x") == b"x"
    assert build_stdin(None, None) == b""


def test_control_args_opts_and_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    s = Settings()
    args = control_args(s)
    assert "ControlMaster=auto" in args
    assert any(a.startswith("ControlPath=") for a in args)
    assert s.control_dir.is_dir()  # каталог сокета создан
    assert s.control_dir.stat().st_mode & 0o777 == 0o700


def test_control_args_refuse_shared_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Чужой или открытый каталог под сокеты — отказ: через сокет уходит пароль sudo.
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
    # Многобайтный символ, разрезанный границей чтения, доклеивается, а не портится.
    out = Output(100)
    data = "привет".encode()
    out.feed(data[:3])
    first = out.text(None, final=False)
    out.feed(data[3:])
    second = out.text(None, final=True)
    assert first + second == "привет"


def test_output_holds_partial_line_while_masking() -> None:
    # Пароль, разрезанный границей чтения, не должен уйти двумя половинами:
    # без `final` неполная строка ждёт продолжения.
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
