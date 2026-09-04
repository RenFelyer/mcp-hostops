"""Низкоуровневые примитивы ssh, общие для роутеров команд и задач и для проб.

Соединения переиспользуются через ControlMaster (сокет в приватном каталоге
runtime), для хостов из `pty_hosts` ssh идёт с `-tt`. Вход в хост
неинтерактивный (`BatchMode=yes`) — аутентификация только по ключу; пароль на
stdin — это пароль sudo, а не вход. Оболочка хоста считается POSIX-совместимой:
удалённый скрипт использует `&&`, `read -r` и `printf`.
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
    """Опции ControlMaster для переиспользования соединения.

    Raises:
        PermissionError: каталог сокетов чужой или открыт другим — в нём нельзя
            держать сокет, через который уходят команды и пароль sudo.
    """
    control_dir = private_dir(s.control_dir)
    options = [
        "ControlMaster=auto",
        f"ControlPath={control_dir}/%C",
        f"ControlPersist={s.control_persist}",
    ]
    return [arg for option in options for arg in ("-o", option)]


def ssh_argv(host: Host, s: Settings, *, tty: bool = False) -> list[str]:
    """Базовый вызов ssh с ControlMaster, без команды; `tty` добавляет `-tt`.

    `--` перед алиасом: имя из конфига, начинающееся с `-`, не станет опцией.
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
    """Экранировать cwd, оставив `~` и `~/` раскрываться оболочкой."""
    if cwd == "~":
        return "~"
    if cwd.startswith("~/"):
        return "~/" + shlex.quote(cwd[2:])
    return shlex.quote(cwd)


def remote_script(command: str, cwd: str, prime: bool) -> str:
    """Скрипт для хоста: заход в cwd, при необходимости прайминг sudo, затем команда."""
    parts = [f"cd -- {_quote_cwd(cwd)}"]
    if prime:
        parts.append(SUDO_PRIME)
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
    """Готовый к запуску вызов: argv, нагрузка stdin и пароль для маскировки.

    stdin и пароль скрыты из `repr`: текст исключения или отладочная запись с
    вызовом не должны показывать пароль sudo.
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
    """Собрать вызов команды на хосте.

    Raises:
        SudoError: пароль нужен, но файла нет или у него небезопасные права.
        PermissionError: каталог сокетов ControlMaster чужой.
    """
    prime = decide_prime(command, sudo_mode)
    password = read_secret(host.alias, s) if prime else None
    argv = [
        *ssh_argv(host, s, tty=host.alias in s.pty_hosts),
        remote_script(command, cwd, prime),
    ]
    return Invocation(argv=argv, stdin=build_stdin(password, user_stdin), password=password)


class Output:
    """Буфер одного потока с потолком: лишнее отбрасывается, обрезка запоминается.

    Декодер инкрементальный: символ UTF-8, разрезанный границей чтения, не
    превращается в мусор, а доклеивается при следующем чтении. Когда есть что
    маскировать, так же придерживается и неполная последняя строка: пароль,
    разрезанный границей чтения на два куска, построчная маска не узнала бы.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""

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

    def text(self, password: str | None, *, final: bool) -> str:
        """Забрать накопленное текстом с замаскированным паролем.

        `final` — вывода больше не будет: недоклеенный хвост отдаётся как есть,
        а неполный символ — с заменой.
        """
        text = self._pending + self._decoder.decode(self.take(), final)
        self._pending = ""
        if password and not final:
            text, newline, self._pending = text.rpartition("\n")
            text += newline
        return mask(text, password)


class Capture:
    """Оба потока процесса и пароль, который в них надо маскировать."""

    def __init__(self, limit: int, password: str | None) -> None:
        self.stdout = Output(limit)
        self.stderr = Output(limit)
        self.password = password

    def drained(self, *, final: bool = True) -> CapturedOutput:
        """Забрать накопленное; буферы после этого пусты, флаги обрезки остаются."""
        return CapturedOutput(
            stdout=self.stdout.text(self.password, final=final),
            stderr=self.stderr.text(self.password, final=final),
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


@contextlib.asynccontextmanager
async def spawn(call: Invocation) -> AsyncGenerator[anyio.abc.Process]:
    """Запустить ssh; на выходе живой процесс убивается, а не дожидается.

    Отмена извне (таймаут, снятие задачи, остановка сервера) не должна ждать
    удалённую команду: закрытие процесса само его не убьёт.
    """
    log.debug("запуск: %s", call.argv)
    async with await anyio.open_process(call.argv) as proc:
        try:
            yield proc
        finally:
            if proc.returncode is None:
                proc.kill()
            log.debug("завершён с кодом %s", proc.returncode)


async def execute(proc: anyio.abc.Process, call: Invocation, capture: Capture) -> int:
    """Скормить stdin, вычитать оба потока до конца и вернуть код возврата.

    Вывод пишется по мере чтения, поэтому при отмене (таймаут, снятие) в буферах
    остаётся всё, что успели получить.
    """
    async with anyio.create_task_group() as tg:
        tg.start_soon(_feed_stdin, proc, call.stdin)
        tg.start_soon(_drain, proc.stdout, capture.stdout)
        tg.start_soon(_drain, proc.stderr, capture.stderr)
    return await proc.wait()
