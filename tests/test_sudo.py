"""Разбор sudo, чтение секрета и маскировка — чистая логика без сети."""

from pathlib import Path

import pytest

from mcp_openssh_connector.core.config.environment import Settings
from mcp_openssh_connector.core.utils.sudo import (
    SudoError,
    decide_prime,
    mask,
    read_secret,
    uses_sudo,
)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("whoami", False),
        ("sudo whoami", True),
        ("ls && sudo apt update", True),
        ("echo sudo", False),  # sudo как аргумент, не глагол
        ("grep -r sudo /etc", False),
        ("env FOO=bar sudo systemctl restart x", True),  # обёртка env
        ("env -u HOME sudo true", True),  # опция env со значением
        ("VAR=1 sudo -n true", True),  # присваивание перед sudo
        ("nohup sudo backup &", True),
        ("sleep 1 & sudo reboot", True),  # фоновая команда — тоже разделитель
        ("ls 2>&1 | grep sudo", False),  # `>&` — перенаправление, не разделитель
        ("timeout 5 sudo systemctl stop x", True),  # обёртка с позиционным аргументом
        ("timeout -s KILL 5 sudo true", True),
        ("time -p sudo true", True),  # `-p` у time — флаг, а не опция со значением
        ("nice -n 10 sudo make", True),
        ("bash -c 'sudo reboot'", True),  # вложенный sudo внутри sh -c
        ("bash -lc 'sudo reboot'", True),  # флаг -c склеен с другим
        ("bash -o pipefail -c 'sudo reboot'", True),  # `-o` берёт значение
        ("sh -c 'ls; sudo tail -f /var/log/x'", True),
        ("bash -c 'echo hi'", False),
        ("doas pkg upgrade", True),
        ("/usr/bin/sudo -u www ls", True),
    ],
)
def test_uses_sudo(command: str, expected: bool) -> None:
    assert uses_sudo(command) is expected


def test_decide_prime_modes() -> None:
    assert decide_prime("whoami", "true") is True
    assert decide_prime("sudo whoami", "false") is False
    assert decide_prime("sudo whoami", "auto") is True
    assert decide_prime("whoami", "auto") is False


def test_mask_masks_whole_line_password() -> None:
    # Эхо pty — пароль отдельной строкой: маскируется целиком.
    assert mask("s3cret\r\nреальный вывод", "s3cret") == "***\nреальный вывод"
    assert mask("s3cret", "s3cret") == "***"


def test_mask_does_not_corrupt_substrings() -> None:
    # Короткий пароль-подстрока не должен портить обычный вывод.
    assert mask("chroot to /root", "root") == "chroot to /root"
    assert mask("banana bread", "an") == "banana bread"


def test_mask_noop() -> None:
    assert mask("текст", None) == "текст"
    assert mask("текст", "") == "текст"


def test_read_secret_rejects_traversal(tmp_path: Path) -> None:
    s = Settings(secret_dir=tmp_path)
    with pytest.raises(SudoError, match="за пределы"):
        read_secret("../evil", s)


def test_read_secret_ok(tmp_path: Path) -> None:
    secret = tmp_path / "host1.secret"
    secret.write_text("пароль\r\n", encoding="utf-8")
    secret.chmod(0o600)
    s = Settings(secret_dir=tmp_path)
    assert read_secret("host1", s) == "пароль"


def test_read_secret_missing(tmp_path: Path) -> None:
    s = Settings(secret_dir=tmp_path)
    with pytest.raises(SudoError, match="нет файла"):
        read_secret("nope", s)


def test_read_secret_bad_mode(tmp_path: Path) -> None:
    secret = tmp_path / "host2.secret"
    secret.write_text("x", encoding="utf-8")
    secret.chmod(0o644)
    s = Settings(secret_dir=tmp_path)
    with pytest.raises(SudoError, match="небезопасные права"):
        read_secret("host2", s)


def test_read_secret_not_regular(tmp_path: Path) -> None:
    (tmp_path / "dir.secret").mkdir(mode=0o700)
    s = Settings(secret_dir=tmp_path)
    with pytest.raises(SudoError, match="обычным файлом"):
        read_secret("dir", s)


def test_read_secret_follows_own_symlink(tmp_path: Path) -> None:
    # Своя ссылка на файл в другом месте — допустима: проверяется сам файл.
    store = tmp_path / "store"
    store.mkdir()
    real = store / "pw"
    real.write_text("пароль", encoding="utf-8")
    real.chmod(0o600)
    (tmp_path / "linked.secret").symlink_to(real)
    assert read_secret("linked", Settings(secret_dir=tmp_path)) == "пароль"
