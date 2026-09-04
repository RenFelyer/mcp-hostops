"""Доступность хостов: TCP-проба порта, а за jump-хостами — проба изнутри.

Прямой хост проверяем TCP-коннектом к порту, а не полным входом. Хосты за
ProxyJump прямого маршрута не имеют — их пробуем изнутри jump-хоста одним
ssh-вызовом. Флаг deep добавляет реальный вход `ssh ... true`: он отвечает уже
не на «порт открыт», а на «ключ принят, внутрь пускают».

Логика синхронная (socket и subprocess): сервер вызывает её через поток, чтобы
не блокировать цикл событий.
"""

import errno
import shlex
import socket
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from time import sleep, time

from ..config import Settings
from ..schemas import AVAILABLE, UNAVAILABLE, Availability, Host, as_availability
from .hosts import pairs, resolve
from .ssh import run_sync, ssh_argv

# ZeroTier поднимает путь к пиру лениво: пока идёт рандеву через корневые
# серверы, ядро мгновенно отдаёт EHOSTUNREACH. Одна проба принимает это за
# «узел мёртв», хотя вторая уже проходит. Больший таймаут не лечит — ошибка
# приходит сразу, а не по нему.
_RETRY_ERRNOS = frozenset({errno.EHOSTUNREACH, errno.ENETUNREACH})
_PATH_BUDGET = 2.5  # с суммарно на повторы, пока оверлей поднимает путь
_PATH_PAUSE = 0.25  # с между попытками


def _reachable(host: Host, connect_timeout: float) -> bool:
    """Открыт ли порт: TCP-коннект, а не полный вход с аутентификацией.

    Ошибку маршрутизации перепроверяем до `_PATH_BUDGET` — на оверлее она значит
    лишь «путь ещё не поднят». Отказ и таймаут перепроверять нечего: это ответ.
    """
    deadline = time() + _PATH_BUDGET
    while True:
        try:
            with socket.create_connection((host.hostname, host.port), connect_timeout):
                return True
        except OSError as err:
            if err.errno not in _RETRY_ERRNOS or time() >= deadline:
                return False
        sleep(_PATH_PAUSE)


def _probe_direct(host: Host, s: Settings) -> dict[str, Availability]:
    return {host.alias: AVAILABLE if _reachable(host, s.connect_timeout) else UNAVAILABLE}


def _jump_script(group: list[Host]) -> str:
    """Скрипт проверки портов группы на jump-хосте.

    hostname/port/alias экранируются `shlex.quote`: значения из конфига не должны
    подставляться в удалённый bash как код.
    """
    checks = []
    for h in group:
        host_q = shlex.quote(h.hostname)
        port_q = shlex.quote(str(h.port))
        alias_q = shlex.quote(h.alias)
        checks.append(
            f'if timeout 1 bash -c \'exec 3<>/dev/tcp/"$0"/"$1"\' '
            f"{host_q} {port_q} 2>/dev/null; "
            f"then echo {alias_q} {AVAILABLE}; else echo {alias_q} {UNAVAILABLE}; fi"
        )
    return "; ".join(checks)


def _probe_via(jump: Host, group: list[Host], s: Settings) -> dict[str, Availability]:
    """Группа за одним jump-хостом: прямого маршрута нет, пробуем изнутри его сети.

    Недоступный jump означает недоступность всего за ним — это вывод, а не догадка.
    """
    down = {h.alias: UNAVAILABLE for h in group}
    # ssh к самому jump-хосту ленивость оверлея задевает так же, но повтора внутри
    # себя не имеет: холодный путь утащил бы в unavailable всю группу за ним.
    if not _reachable(jump, s.connect_timeout):
        return down
    try:
        done = run_sync([*ssh_argv(jump, s), _jump_script(group)], s.jump_timeout)
    except (OSError, subprocess.SubprocessError):
        return down
    if done.returncode != 0:
        return down
    seen = pairs(done.stdout)
    return {h.alias: as_availability(seen.get(h.alias, "")) for h in group}


def measure(hosts: list[Host], s: Settings) -> dict[str, Availability]:
    """Все пробы разом; время ограничено таймаутами самих проб.

    Returns:
        Статус по каждому алиасу из `hosts`.
    """
    if not hosts:
        return {}
    known = {h.alias: h for h in hosts}
    direct = [h for h in hosts if not h.proxyjump]
    behind: dict[str, list[Host]] = defaultdict(list)
    for host in hosts:
        if host.proxyjump:
            behind[host.proxyjump].append(host)
    # jump обычно и сам описан в конфиге — тогда он уже разобран, второй `ssh -G`
    # не нужен. Нераспознанный jump делает всю группу за ним недоступной.
    statuses: dict[str, Availability] = {}
    probes = []
    for alias, group in behind.items():
        via = known.get(alias) or resolve(alias, s.ssh_g_timeout)
        if via is None:
            statuses |= {h.alias: UNAVAILABLE for h in group}
        else:
            probes.append((via, group))

    with ThreadPoolExecutor(max_workers=len(direct) + len(probes) or 1) as pool:
        futures = [pool.submit(_probe_direct, h, s) for h in direct]
        futures += [pool.submit(_probe_via, via, group, s) for via, group in probes]
        for future in futures:
            statuses |= future.result()
    return statuses


def deep_check(host: Host, s: Settings) -> tuple[bool, str]:
    """Реальный вход `ssh ... true`.

    Returns:
        Пара «пустили ли» и короткая причина отказа; при успехе причина пустая.
    """
    try:
        done = run_sync([*ssh_argv(host, s), "true"], s.deep_timeout)
    except subprocess.TimeoutExpired:
        return False, "таймаут входа"
    except OSError as err:
        return False, f"ssh не запустился: {err}"
    if done.returncode == 0:
        return True, ""
    reason = done.stderr.strip().splitlines()
    return False, reason[-1] if reason else f"код возврата {done.returncode}"
