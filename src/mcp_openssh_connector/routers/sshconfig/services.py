"""Сервис роутера управления ~/.ssh/config: managed-файл, known_hosts, ssh-copy-id.

Сервер владеет отдельным managed-файлом (`managed_config_file`) и подключает его
к основному конфигу директивой `Include` — один раз, абсолютным путём. Ручной
`~/.ssh/config` при этом не переписывается: add_host/remove_host трогают только
managed-файл, который всегда пишется в каноническом виде (`Host`, отступ в четыре
пробела, порядок ключей из `MANAGED_KEY_ORDER`). Так конфиг остаётся стандартным
и пополняемым, а правки руками — нетронутыми.

Разбор алиаса и настройки сервис берёт сам (`get_settings`, `resolve`);
синхронную работу с файлами и подпроцессами обработчики уводят в поток. Ошибки
клиенту — `UserError`. Пароль для ssh-copy-id — тот же `~/.ssh/<alias>.secret`,
что и для sudo: его отдаёт хосту `sshpass -f`, минуя argv и логи.
"""

import re
import shutil
import subprocess
from pathlib import Path

from ...core.config.constants import (
    MANAGED_HEADER,
    MANAGED_KEY_ORDER,
    PRIVATE_DIR_MODE,
    SECRET_FILE_MODE,
    SSH_DEFAULT_PORT,
)
from ...core.config.environment import Settings, get_settings
from ...core.errors import UserError
from ...core.schemas import Host
from ...core.store import write_bytes
from ...core.utils.hosts import config_files, read_aliases, resolve
from ...core.utils.ssh import run_sync
from ...core.utils.sudo import mask, read_secret
from .schemas import AddHostResult, CopyIdResult, ForgetHostResult, ManagedHost, RemoveHostResult

# Строка, открывающая Host-блок: ключевое слово `Host`, форма `Host x` или `Host=x`.
_HOST_LINE = re.compile(r"(?i)^\s*host[\s=]")

# Блок managed-файла: алиасы из строки `Host` и полный текст блока.
Block = tuple[list[str], str]


def _check_alias(alias: str) -> None:
    """Проверить, что алиас годится для записи в конфиг.

    Raises:
        UserError: пусто, есть пробел, символ маски (`*?`), отрицание или ведущий `-`.
    """
    if not alias or any(ch.isspace() for ch in alias) or alias[0] in "-!" or any(ch in alias for ch in "*?#"):
        raise UserError(f"алиас {alias!r}: без пробелов и без * ? # !, не начинается с -")


def _parse_blocks(text: str) -> list[Block]:
    """Разобрать managed-файл на Host-блоки; всё вне блоков (шапка) отбрасывается."""
    blocks: list[Block] = []
    aliases: list[str] | None = None
    buf: list[str] = []
    for raw in text.splitlines():
        if _HOST_LINE.match(raw):
            if aliases is not None:
                blocks.append((aliases, "\n".join(buf).rstrip() + "\n"))
            aliases = raw.partition("#")[0].replace("=", " ", 1).split()[1:]
            buf = [raw]
        elif aliases is not None:
            buf.append(raw)
    if aliases is not None:
        blocks.append((aliases, "\n".join(buf).rstrip() + "\n"))
    return blocks


def _render_block(host: ManagedHost) -> str:
    """Собрать канонический Host-блок: заголовок и опции с отступом в четыре пробела."""
    named = {
        "HostName": host.hostname,
        "User": host.user,
        "Port": str(host.port),
        "IdentityFile": host.identity_file,
        "ProxyJump": host.proxy_jump,
    }
    lines = [f"Host {host.alias}"]
    lines += [f"    {key} {named[key]}" for key in MANAGED_KEY_ORDER if named[key]]
    lines += [f"    {key} {value}" for key, value in host.extra.items()]
    return "\n".join(lines) + "\n"


def _render_file(blocks: list[Block]) -> bytes:
    """Managed-файл целиком: шапка и блоки через пустую строку."""
    return "".join([MANAGED_HEADER, *(f"\n{text}" for _, text in blocks)]).encode()


def _read_blocks(s: Settings) -> list[Block]:
    """Блоки managed-файла; отсутствующего или нечитаемого файла — пусто."""
    try:
        return _parse_blocks(s.managed_config_file.read_text(encoding="utf-8"))
    except OSError:
        return []


def _write_blocks(blocks: list[Block], s: Settings) -> None:
    """Атомарно перезаписать managed-файл и закрыть его права до 0600."""
    write_bytes(s.managed_config_file, _render_file(blocks))
    s.managed_config_file.chmod(SECRET_FILE_MODE)


def _ensure_available(s: Settings) -> bool:
    """Создать managed-файл и подключить его к основному конфигу.

    Returns:
        True — строка `Include` добавлена этим вызовом; False — уже была.
    """
    s.managed_config_file.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    if not s.managed_config_file.exists():
        _write_blocks([], s)
    if s.managed_config_file.resolve() in config_files(s.ssh_config_file):
        return False
    # Абсолютный путь: относительный ssh раскрыл бы от ~/.ssh, а не от каталога
    # этого конфига. В начало — чтобы адресный Host побеждал общий `Host *`.
    include = f"Include {s.managed_config_file}\n".encode()
    s.ssh_config_file.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    try:
        existing = s.ssh_config_file.read_bytes()
    except OSError:
        existing = b""
    write_bytes(s.ssh_config_file, include + existing)
    return True


def add_host(spec: ManagedHost) -> AddHostResult:
    """Записать Host-блок в managed-файл и вернуть, как ssh -G его теперь видит.

    Существующий managed-блок того же алиаса заменяется; алиас, уже описанный в
    конфиге вручную, не трогается — иначе на один алиас пришлось бы два Host.

    Raises:
        UserError: алиас негоден, hostname пуст или алиас занят ручной записью.
        OSError: конфиг или managed-файл не записались.
    """
    _check_alias(spec.alias)
    if not spec.hostname.strip():
        raise UserError("hostname пуст")
    s = get_settings()
    include_added = _ensure_available(s)
    blocks = _read_blocks(s)
    managed = {alias for aliases, _ in blocks for alias in aliases}
    if spec.alias in read_aliases(s.ssh_config_file) and spec.alias not in managed:
        raise UserError(f"алиас {spec.alias!r} уже описан в конфиге вручную")
    kept = [(aliases, text) for aliases, text in blocks if spec.alias not in aliases]
    kept.append(([spec.alias], _render_block(spec)))
    _write_blocks(kept, s)
    return AddHostResult(
        alias=spec.alias,
        config_file=str(s.managed_config_file),
        include_added=include_added,
        host=resolve(spec.alias, s.ssh_g_timeout),
    )


def remove_host(alias: str, forget_known: bool, drop_secret: bool) -> RemoveHostResult:
    """Убрать managed-блок алиаса и, по флагам, его записи known_hosts и секрет.

    Raises:
        UserError: алиас не описан в managed-файле (ручные записи не трогаем).
        OSError: managed-файл не записался.
    """
    s = get_settings()
    blocks = _read_blocks(s)
    if not any(alias in aliases for aliases, _ in blocks):
        raise UserError(f"хост {alias!r} не в managed-файле; ручные записи remove_host не трогает")
    host = resolve(alias, s.ssh_g_timeout)  # пока блок на месте — узнаём hostname для known_hosts
    _write_blocks([(aliases, text) for aliases, text in blocks if alias not in aliases], s)
    removed = _forget(host, s) if forget_known and host is not None else 0
    dropped = _drop_secret(alias, s) if drop_secret else False
    return RemoveHostResult(alias=alias, known_hosts_removed=removed, secret_removed=dropped)


def forget_host(target: str) -> ForgetHostResult:
    """Удалить записи known_hosts для хоста, не трогая конфиг.

    `target` — алиас из конфига (тогда чистим его hostname) или сам hostname/IP.
    Нужно, когда ключ хоста сменился («Remote host identification has changed»).
    """
    s = get_settings()
    host = resolve(target, s.ssh_g_timeout)
    return ForgetHostResult(
        target=host.hostname if host is not None else target,
        known_hosts_file=str(s.known_hosts_file),
        removed=_forget(host, s) if host is not None else _keygen_remove([target], s.known_hosts_file),
    )


def copy_id(alias: str, identity: str) -> CopyIdResult:
    """Установить публичный ключ на хост через ssh-copy-id, пароль — из секрета.

    Пароль хоста берётся из `~/.ssh/<alias>.secret` и отдаётся `sshpass -f`, не
    попадая ни в argv, ни в лог. Ключ хоста при первом входе принимается
    (`StrictHostKeyChecking=accept-new`), иначе неинтерактивный ssh завис бы на
    вопросе доверия.

    Raises:
        UserError: алиас не в конфиге, нет ssh-copy-id или sshpass, недоступен
            секрет, либо ssh-copy-id не уложился в таймаут.
    """
    s = get_settings()
    if alias not in read_aliases(s.ssh_config_file):
        raise UserError(f"хост {alias!r} не описан в ~/.ssh/config")
    for tool in ("ssh-copy-id", "sshpass"):
        if shutil.which(tool) is None:
            raise UserError(f"нужен {tool}: пароль хоста берётся из ~/.ssh/<alias>.secret")
    password = read_secret(alias, s)  # валидирует наличие и права секрета
    argv = ["sshpass", "-f", str(s.secret_file(alias)), "ssh-copy-id", "-o", "StrictHostKeyChecking=accept-new"]
    if identity:
        argv += ["-i", identity]
    argv.append(alias)
    try:
        done = run_sync(argv, s.copy_id_timeout)
    except subprocess.TimeoutExpired as err:
        raise UserError(f"ssh-copy-id: таймаут {s.copy_id_timeout} с") from err
    except OSError as err:
        raise UserError(f"ssh-copy-id не запустился: {err}") from err
    lines = [line for line in mask(done.stdout + done.stderr, password).splitlines() if line.strip()]
    return CopyIdResult(alias=alias, ok=done.returncode == 0, detail=lines[-1] if lines else "")


def _drop_secret(alias: str, s: Settings) -> bool:
    """Удалить файл `~/.ssh/<alias>.secret`; False — его не было."""
    try:
        s.secret_file(alias).unlink()
    except OSError:
        return False
    return True


def _forget(host: Host, s: Settings) -> int:
    """Удалить записи known_hosts хоста: по hostname и, если порт нестандартный, `[host]:port`."""
    names = [host.hostname]
    if host.port != SSH_DEFAULT_PORT:
        names.append(f"[{host.hostname}]:{host.port}")
    return _keygen_remove(names, s.known_hosts_file)


def _count_lines(path: Path) -> int:
    try:
        return len(path.read_bytes().splitlines())
    except OSError:
        return 0


def _keygen_remove(names: list[str], known_file: Path) -> int:
    """`ssh-keygen -R` по каждому имени; вернуть, на сколько строк файл убыл.

    ssh-keygen сам разбирает хешированные записи и переписывает файл; сколько
    удалено — считаем по разнице числа строк (он же кладёт рядом `.old`).

    Raises:
        UserError: ssh-keygen не найден.
    """
    if shutil.which("ssh-keygen") is None:
        raise UserError("ssh-keygen не найден")
    if not known_file.exists():
        return 0
    before = _count_lines(known_file)
    for name in names:
        try:
            run_sync(["ssh-keygen", "-R", name, "-f", str(known_file)], 10.0)
        except (OSError, subprocess.SubprocessError):
            continue
    return before - _count_lines(known_file)
