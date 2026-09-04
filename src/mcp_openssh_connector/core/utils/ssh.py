"""Низкоуровневые примитивы ssh, общие для роутеров команд и задач и для проб.

Соединения переиспользуются через ControlMaster (сокет в runtime-каталоге), для
хостов из `pty_hosts` ssh идёт с `-tt`. Вход в хост неинтерактивный
(`BatchMode=yes`) — аутентификация только по ключу; пароль на stdin — это пароль
sudo, а не вход. Команда выполняется в явном cwd: `~` и `~/…` раскрываются
оболочкой хоста, остальное экранируется и берётся буквально.
"""

import shlex
import subprocess

import anyio
import anyio.abc
from pydantic import BaseModel

from ..config import Settings
from ..schemas import CapturedOutput, Host, SudoMode
from .sudo import decide_prime, mask, read_secret


def run_sync(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Локальный процесс без stdin с захватом вывода; ошибки запуска — наружу."""
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def control_args(s: Settings) -> list[str]:
    """Опции ControlMaster для переиспользования соединения; каталог сокета — 0700."""
    s.control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    options = [
        "ControlMaster=auto",
        f"ControlPath={s.control_dir}/%C",
        f"ControlPersist={s.control_persist}",
    ]
    return [arg for option in options for arg in ("-o", option)]


def ssh_argv(host: Host, s: Settings, *, tty: bool = False) -> list[str]:
    """Базовый вызов ssh с ControlMaster, без команды; `tty` добавляет `-tt`."""
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
    argv.append(host.alias)
    return argv


def _quote_cwd(cwd: str) -> str:
    """Экранировать cwd, оставив `~` и `~/` раскрываться оболочкой."""
    if cwd == "~":
        return "~"
    if cwd.startswith("~/"):
        return "~/" + shlex.quote(cwd[2:])
    return shlex.quote(cwd)


def remote_script(command: str, cwd: str, prime: bool) -> str:
    """Скрипт для хоста: заход в cwd, при необходимости прайминг sudo, затем команда.

    Перед праймингом тикет сбрасывается (`sudo -k`): с живым тикетом `sudo -v`
    пароль не читает, и строка пароля досталась бы stdin самой команды.
    """
    parts = [f"cd -- {_quote_cwd(cwd)}"]
    if prime:
        parts.append("sudo -k && sudo -S -p '' -v")
    parts.append(command)
    return " && ".join(parts)


def build_stdin(password: str | None, user_stdin: str | None) -> bytes:
    """Полезная нагрузка stdin: пароль sudo первой строкой, затем пользовательский."""
    payload = b""
    if password is not None:
        payload += (password + "\n").encode()
    if user_stdin:
        payload += user_stdin.encode()
    return payload


class Invocation(BaseModel):
    """Готовый к запуску вызов: argv, нагрузка stdin и пароль для маскировки."""

    argv: list[str]
    stdin: bytes
    password: str | None


def prepare(
    host: Host,
    command: str,
    cwd: str,
    sudo_mode: SudoMode,
    user_stdin: str | None,
    s: Settings,
) -> Invocation:
    """Собрать вызов команды на хосте.

    Raises:
        SudoError: пароль нужен, но файла нет или у него небезопасные права.
    """
    prime = decide_prime(command, sudo_mode)
    password = read_secret(host.alias, s) if prime else None
    argv = [
        *ssh_argv(host, s, tty=host.alias in s.pty_hosts),
        remote_script(command, cwd, prime),
    ]
    return Invocation(argv=argv, stdin=build_stdin(password, user_stdin), password=password)


class Output:
    """Буфер одного потока с потолком: лишнее отбрасывается, обрезка запоминается."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def feed(self, chunk: bytes) -> None:
        """Добавить кусок, не превышая потолка."""
        kept = chunk[: max(0, self.limit - len(self.data))]
        self.data += kept
        if len(kept) < len(chunk):
            self.truncated = True

    def take(self) -> bytes:
        """Забрать накопленное и начать с пустого буфера (чтение по частям)."""
        data, self.data = self.data, bytearray()
        return bytes(data)

    def text(self, password: str | None) -> str:
        """Забрать накопленное текстом с замаскированным паролем."""
        return mask(self.take().decode("utf-8", "replace"), password)


class Capture:
    """Оба потока процесса и пароль, который в них надо маскировать."""

    def __init__(self, limit: int, password: str | None) -> None:
        self.stdout = Output(limit)
        self.stderr = Output(limit)
        self.password = password

    def drained(self) -> CapturedOutput:
        """Забрать накопленное; буферы после этого пусты, флаги обрезки остаются."""
        return CapturedOutput(
            stdout=self.stdout.text(self.password),
            stderr=self.stderr.text(self.password),
            stdout_truncated=self.stdout.truncated,
            stderr_truncated=self.stderr.truncated,
        )


async def _feed_stdin(proc: anyio.abc.Process, payload: bytes) -> None:
    """Отправить нагрузку и закрыть stdin, чтобы читающая команда увидела EOF.

    Процесс вправе закрыть stdin раньше, чем мы дописали (или вовсе не дожить до
    записи): это не ошибка вызова, а его результат — он виден по коду возврата.
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
    """Читать поток в `out` до конца; при отмене прочитанное остаётся в буфере."""
    if stream is None:
        return
    async for chunk in stream:
        out.feed(chunk)


async def pump(proc: anyio.abc.Process, stdin: bytes, capture: Capture) -> None:
    """Скормить stdin и вычитать оба потока до их конца.

    Вывод пишется по мере чтения, поэтому при отмене (таймаут, снятие) в буферах
    остаётся всё, что успели получить.
    """
    async with anyio.create_task_group() as tg:
        tg.start_soon(_feed_stdin, proc, stdin)
        tg.start_soon(_drain, proc.stdout, capture.stdout)
        tg.start_soon(_drain, proc.stderr, capture.stderr)
