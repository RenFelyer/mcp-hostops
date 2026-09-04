"""Сборка удалённого скрипта, stdin и опций ControlMaster — чистая логика."""

from pathlib import Path

import pytest

from mcp_openssh_connector.core.config import Settings
from mcp_openssh_connector.core.utils.ssh import (
    build_stdin,
    control_args,
    remote_script,
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
    # `sudo -k` перед `-v`: с живым тикетом sudo пароль не читает, и строка
    # пароля досталась бы stdin самой команды.
    assert got == "cd -- /root && sudo -k && sudo -S -p '' -v && sudo reboot"


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
