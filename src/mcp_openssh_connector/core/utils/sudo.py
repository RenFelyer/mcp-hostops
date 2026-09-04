"""Разбор sudo в команде, чтение пароля и его маскировка.

Пароль хоста лежит в `~/.ssh/<alias>.secret` (права 0600) и читается только в
момент вызова, не кэшируется. Если в команде есть sudo, сервер прайминг-строкой
`sudo -S -p '' -v` скармливает пароль первой строкой stdin и кладёт тикет в кэш;
дальше исходная команда идёт без изменений, а её sudo берут тикет уже без
запроса. В любом выводе пароль заменяется на `***`.
"""

import shlex
import stat
from collections.abc import Iterator
from pathlib import Path

from ..config import Settings
from ..errors import UserError
from ..schemas import SudoMode

MASK = "***"

# Обёртки, за которыми стоит настоящая команда: их и их опции пропускаем, чтобы
# добраться до глагола. Сам sudo/doas сюда не входят — их и надо распознать.
_SKIP_WRAPPERS = frozenset({"env", "nohup", "time", "command", "exec"})
_WRAPPER_VALUED = frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-T", "-U"})
# Оболочки, у которых `-c '<код>'` запускает вложенную команду: в неё надо
# заглянуть, иначе sudo внутри `sh -c '…'` останется незамеченным.
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ash", "ksh", "mksh", "fish"})


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
    """Простые команды скрипта: разрез по `;`, `&&`, `||`, `|` вне кавычек."""
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
            if ch == "&" and script[i + 1 : i + 2] != "&":
                i += 1  # одиночный `&` — фон или `2>&1`, не разделитель
                continue
            yield script[start:i]
            while i < len(script) and script[i] in ";|&":
                i += 1
            start = i
            continue
        i += 1
    yield script[start:]


def _verb(tokens: list[str]) -> tuple[str, list[str]]:
    """Глагол и его аргументы после снятия присваиваний и обёрток (env, nohup…)."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _is_assignment(tok):
            i += 1
            continue
        if tok.rsplit("/", 1)[-1] in _SKIP_WRAPPERS:
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 2 if tokens[i] in _WRAPPER_VALUED else 1
            continue
        return tok.rsplit("/", 1)[-1], tokens[i + 1 :]
    return "", []


def uses_sudo(command: str) -> bool:
    """Есть ли sudo/doas в позиции глагола.

    Смотрим каждую простую команду и рекурсивно — код внутри `<shell> -c '…'`:
    `bash -c 'sudo …'` тоже должна быть распознана, иначе sudo в ней останется
    без пароля.
    """
    for simple in _simple_commands(command):
        verb, args = _verb(_split(simple))
        if verb in ("sudo", "doas"):
            return True
        if verb in _SHELLS and "-c" in args:
            inner = args[args.index("-c") + 1 :]
            if inner and uses_sudo(inner[0]):
                return True
    return False


def decide_prime(command: str, sudo_mode: SudoMode) -> bool:
    """Нужен ли прайминг пароля: auto решает по команде, true/false — принудительно."""
    if sudo_mode == "true":
        return True
    if sudo_mode == "false":
        return False
    return uses_sudo(command)


def read_secret(alias: str, s: Settings) -> str:
    """Пароль из `~/.ssh/<alias>.secret`.

    В сообщение об ошибке попадает только путь, но не содержимое.

    Raises:
        SudoError: файла нет, это не обычный файл, права не 0600 или алиас
            выводит путь за пределы каталога секретов (защита от `../`).
    """
    path: Path = s.secret_file(alias)
    if path.resolve().parent != s.secret_dir.resolve():
        raise SudoError(f"алиас ведёт за пределы каталога секретов: {alias!r}")
    try:
        info = path.stat()
    except OSError as err:
        raise SudoError(f"нет файла с паролем: {path} ({err.strerror})") from err
    if not stat.S_ISREG(info.st_mode):
        raise SudoError(f"файл с паролем не обычный файл: {path}")
    if info.st_mode & 0o077:
        raise SudoError(f"у файла с паролем небезопасные права, нужно 0600: {path}")
    return path.read_text(encoding="utf-8").rstrip("\n")


def mask(text: str, password: str | None) -> str:
    """Заменить строки, состоящие ровно из пароля, на `***`.

    Пароль утекает в вывод только эхом pty (отдельной строкой), поэтому маскируем
    построчно, а не подстрокой: иначе короткий пароль-подстрока (например `root`)
    испортил бы обычный вывод.
    """
    if not password:
        return text
    return "\n".join(MASK if line.strip("\r") == password else line for line in text.split("\n"))
