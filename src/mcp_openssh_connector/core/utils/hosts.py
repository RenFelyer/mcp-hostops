"""Хосты из `~/.ssh/config` и их параметры глазами самого ssh.

Алиасы читаем из конфига и его `Include`-файлов в порядке появления, параметры
каждого получаем через `ssh -G`: он раскрывает наследование из `Host *`,
`Include` и `Match` так же, как это увидит настоящее соединение. Источник правды
— только конфиг: адреса вида `user@host` в обход него сервер не принимает.

Путь к конфигу не настраивается: `ssh -G` и сами соединения всё равно читают
`~/.ssh/config`, а перечислять хосты из одного файла и ходить по другому — обман.
"""

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio.to_thread

from ..config import get_settings
from ..errors import UserError
from ..schemas import Host
from .ssh import run_sync

SSH_DIR = Path.home() / ".ssh"
SSH_CONFIG = SSH_DIR / "config"

_MAX_RESOLVERS = 16  # параллельных `ssh -G` при перечислении


def pairs(text: str) -> dict[str, str]:
    """Строки «ключ значение» (разрез по первому пробелу) — в словарь."""
    return {key: value for key, _, value in (line.partition(" ") for line in text.splitlines())}


def _hidden_by_wildcard(pattern: Path, match: Path) -> bool:
    """Компонент, подставленный маской, начинается с точки.

    `Path.glob` такие подхватывает, а glob(3) в ssh — нет; равняемся на ssh.
    """
    return any(p != m and m.startswith(".") for p, m in zip(pattern.parts, match.parts, strict=False))


def _expand_include(pattern: str) -> list[Path]:
    """Раскрыть путь `Include`-директивы в существующие файлы.

    `~` и абсолютные пути — как есть, относительные — от `~/.ssh` (правило ssh
    для пользовательского конфига). Маски раскрываются.
    """
    if pattern.startswith("~"):
        base = Path(pattern).expanduser()
    elif pattern.startswith("/"):
        base = Path(pattern)
    else:
        base = SSH_DIR / pattern
    matches = Path(base.anchor).glob(str(base.relative_to(base.anchor)))
    return sorted(m for m in matches if not _hidden_by_wildcard(base, m))


def _scan_aliases(config: Path, names: list[str], seen: set[Path]) -> None:
    """Добавить имена Host из файла и его `Include`; циклы отсекаются по `seen`."""
    try:
        resolved = config.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        words = line.partition("#")[0].split()
        if len(words) <= 1:
            continue
        keyword = words[0].lower()
        if keyword == "host":
            names += [w for w in words[1:] if "*" not in w and "?" not in w]
        elif keyword == "include":
            for pattern in words[1:]:
                for included in _expand_include(pattern):
                    _scan_aliases(included, names, seen)


def read_aliases(config: Path = SSH_CONFIG) -> list[str]:
    """Имена Host из конфига и его `Include`-файлов.

    Нечитаемый конфиг даёт пустой список — ошибок ввода-вывода наружу нет.

    Args:
        config: Файл конфига; по умолчанию `~/.ssh/config`.

    Returns:
        Имена в порядке появления, без дублей и без масок.
    """
    names: list[str] = []
    _scan_aliases(config, names, set())
    return list(dict.fromkeys(names))


def resolve(alias: str, timeout: float) -> Host | None:
    """Параметры хоста глазами ssh.

    `ssh -G` раскрывает наследование из `Host *`, `Include` и `Match`.

    Returns:
        Хост или None, если ssh не ответил, упал или не уложился в `timeout`.
    """
    try:
        done = run_sync(["ssh", "-G", alias], timeout)
        # `ssh -G` печатает без отступов и всегда в нижнем регистре.
        fields = pairs(done.stdout) if done.returncode == 0 else {}
        port = int(fields["port"])
    except (OSError, subprocess.SubprocessError, KeyError, ValueError):
        return None
    return Host(
        alias=alias,
        hostname=fields.get("hostname", alias),
        user=fields.get("user", "-"),
        port=port,
        proxyjump=fields.get("proxyjump", ""),
    )


def discover(timeout: float) -> list[Host]:
    """Хосты конфига с уже вычисленными параметрами, в порядке конфига."""
    names = read_aliases()
    if not names:
        return []
    with ThreadPoolExecutor(max_workers=min(len(names), _MAX_RESOLVERS)) as pool:
        resolved = pool.map(lambda name: resolve(name, timeout), names)
        return [host for host in resolved if host]


def host_detail(alias: str, timeout: float) -> Host | None:
    """Параметры хоста, только если алиас описан в конфиге; иначе None.

    `ssh -G` успешен для любого имени, поэтому членство в конфиге — единственный
    способ отличить наш алиас от произвольного.
    """
    if alias not in read_aliases():
        return None
    return resolve(alias, timeout)


async def require_host(alias: str) -> Host:
    """Хост по алиасу; общий вход для всех роутеров, разрешение уходит в поток.

    Raises:
        UserError: алиаса нет в `~/.ssh/config` — сервер работает только с
            хостами из конфига.
    """
    timeout = get_settings().ssh_g_timeout
    host = await anyio.to_thread.run_sync(host_detail, alias, timeout)
    if host is None:
        raise UserError(f"хост {alias!r} не описан в ~/.ssh/config")
    return host
