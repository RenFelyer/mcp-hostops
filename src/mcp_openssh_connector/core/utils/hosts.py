"""Хосты из `~/.ssh/config` и их параметры глазами самого ssh.

Алиасы читаем из конфига и его `Include`-файлов в порядке появления, параметры
каждого получаем через `ssh -G`: он раскрывает наследование из `Host *`,
`Include` и `Match` так же, как это увидит настоящее соединение. Источник правды
— только конфиг: адреса вида `user@host` в обход него сервер не принимает.

Путь к конфигу не настраивается: `ssh -G` и сами соединения всё равно читают
`~/.ssh/config`, а перечислять хосты из одного файла и ходить по другому — обман.
"""

import subprocess
from pathlib import Path

import anyio.to_thread
from pydantic import ValidationError

from ..config.constants import SSH_CONFIG, SSH_DIR
from ..config.environment import get_settings
from ..errors import UserError
from ..schemas import Host
from .parallel import fan_out
from .ssh import run_sync


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


def _is_alias(word: str) -> bool:
    """Имя хоста, а не маска (`*`, `?`) и не отрицание (`!`)."""
    return not word.startswith("!") and "*" not in word and "?" not in word


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
        # ssh допускает и `Host x`, и `Host=x`; хвостовой `#` он отбрасывает.
        words = line.partition("#")[0].replace("=", " ", 1).split()
        if len(words) <= 1:
            continue
        keyword = words[0].lower()
        if keyword == "host":
            names += filter(_is_alias, words[1:])
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
        Имена в порядке появления, без дублей, масок и отрицаний.
    """
    names: list[str] = []
    _scan_aliases(config, names, set())
    return list(dict.fromkeys(names))


def resolve(alias: str, timeout: float) -> Host | None:
    """Параметры хоста глазами ssh.

    `ssh -G` раскрывает наследование из `Host *`, `Include` и `Match`. `--`
    перед алиасом не даёт имени, начинающемуся с `-`, стать опцией ssh.

    Returns:
        Хост или None, если ssh не ответил, упал, не уложился в `timeout` или
        напечатал не то, из чего собирается `Host`.
    """
    try:
        done = run_sync(["ssh", "-G", "--", alias], timeout)
        # `ssh -G` печатает без отступов и всегда в нижнем регистре; лишние
        # ключи модель отбрасывает, недостающий или кривой port — ошибка.
        fields = pairs(done.stdout) if done.returncode == 0 else {}
        return Host.model_validate({"alias": alias, "proxyjump": "", **fields})
    except (OSError, subprocess.SubprocessError, ValidationError):
        return None


def _resolve_all(aliases: list[str], timeout: float) -> dict[str, Host]:
    """`ssh -G` для каждого алиаса параллельно; не разобранные в ответ не попадают."""
    resolved = fan_out(lambda alias: resolve(alias, timeout), aliases)
    return {host.alias: host for host in resolved if host}


def resolve_known(aliases: list[str], timeout: float) -> dict[str, Host]:
    """Параметры тех из `aliases`, что описаны в конфиге.

    `ssh -G` успешен для любого имени, поэтому членство в конфиге — единственный
    способ отличить наш алиас от произвольного. Чужие и не разобранные алиасы в
    ответ не попадают.
    """
    known = set(read_aliases())
    return _resolve_all([alias for alias in dict.fromkeys(aliases) if alias in known], timeout)


def discover(timeout: float) -> list[Host]:
    """Хосты конфига с уже вычисленными параметрами, в порядке конфига."""
    return list(_resolve_all(read_aliases(), timeout).values())


async def require_host(alias: str) -> Host:
    """Хост по алиасу; общий вход для всех роутеров, разрешение уходит в поток.

    Raises:
        UserError: алиаса нет в `~/.ssh/config` — сервер работает только с
            хостами из конфига.
    """
    timeout = get_settings().ssh_g_timeout
    hosts = await anyio.to_thread.run_sync(resolve_known, [alias], timeout)
    if alias not in hosts:
        raise UserError(f"хост {alias!r} не описан в ~/.ssh/config")
    return hosts[alias]
