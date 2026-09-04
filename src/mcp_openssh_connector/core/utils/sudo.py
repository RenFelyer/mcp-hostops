"""Разбор sudo в команде, чтение пароля и его маскировка.

Пароль хоста лежит в `~/.ssh/<alias>.secret` (права 0600, наш файл) и читается
только в момент вызова, не кэшируется. Если в команде есть sudo, сервер шлёт
пароль первой строкой stdin, а удалённый скрипт (`ssh.remote_script`) отдаёт
эту строку одному вызову `sudo -v`, который кладёт тикет в кэш; дальше исходная
команда идёт без изменений, а её sudo берут тикет уже без запроса. В любом
выводе строка, равная паролю, заменяется на `***`.

Разбор команды — эвристика для режима «решить по команде»: ищем sudo/doas в
позиции глагола, в том числе за обёртками вроде `env` или `timeout` и внутри
`sh -c '…'`. Подстановки `$(…)` и обратные кавычки не разбираются — там sudo
задаётся явно через параметр `sudo`.
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
    """Пароль для sudo недоступен: файла нет или у него небезопасные права."""


def _is_assignment(token: str) -> bool:
    name, sep, _ = token.partition("=")
    return bool(sep) and name.isidentifier()


def _split(text: str) -> list[str]:
    """Токены по правилам оболочки; при кривых кавычках — по пробелам."""
    try:
        return shlex.split(text, comments=False)
    except ValueError:
        return text.split()


def _simple_commands(script: str) -> Iterator[str]:
    """Простые команды скрипта: разрез по `;`, `&&`, `||`, `|`, `&` вне кавычек.

    Одиночный `&` — тоже разделитель (фоновая команда), кроме перенаправлений
    `>&`, `<&` и `&>`.
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
    """Глагол и его аргументы после снятия присваиваний и обёрток."""
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
    """Код после `-c` у оболочки; флаг может быть склеен (`-lc`, `-xec`).

    `-o` берёт отдельное значение (`bash -o pipefail -c '…'`), остальные флаги
    без него.
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
    """Есть ли sudo/doas в позиции глагола, в том числе внутри `<shell> -c '…'`."""
    for simple in _simple_commands(command):
        verb, args = _verb(_split(simple))
        if verb in ("sudo", "doas"):
            return True
        if verb in SHELLS and (inner := _inline_code(args)) is not None and uses_sudo(inner):
            return True
    return False


def decide_prime(command: str, sudo_mode: SudoMode) -> bool:
    """Нужен ли прайминг пароля: auto решает по команде, true/false — принудительно."""
    if sudo_mode == "auto":
        return uses_sudo(command)
    return sudo_mode == "true"


def read_secret(alias: str, s: Settings) -> str:
    """Пароль из `~/.ssh/<alias>.secret`.

    В сообщение об ошибке попадает только путь, но не содержимое.

    Raises:
        SudoError: алиас выводит путь за пределы каталога секретов, файла нет
            или он не читается, это не обычный наш файл или права не 0600.
    """
    path: Path = s.secret_file(alias)
    # Алиас с `/` или `..` увёл бы путь из каталога секретов; сравнение без
    # обращения к диску, так что своя ссылка на файл в другом месте допустима.
    if path.parent != s.secret_dir:
        raise SudoError(f"алиас ведёт за пределы каталога секретов: {alias!r}")
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise SudoError(f"файл с паролем должен быть нашим обычным файлом: {path}")
        if info.st_mode & 0o077:
            raise SudoError(f"у файла с паролем небезопасные права, нужно 0600: {path}")
        return path.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as err:
        raise SudoError(f"нет файла с паролем: {path} ({err.strerror})") from err


def mask(text: str, password: str | None) -> str:
    """Заменить строки, состоящие ровно из пароля, на `***`.

    Пароль утекает в вывод только эхом pty (отдельной строкой), поэтому маскируем
    построчно, а не подстрокой: иначе короткий пароль-подстрока (например `root`)
    испортил бы обычный вывод.
    """
    if not password:
        return text
    return "\n".join(SUDO_MASK if line.strip("\r") == password else line for line in text.split("\n"))
