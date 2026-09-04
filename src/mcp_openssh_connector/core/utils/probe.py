"""Доступность хостов: TCP-проба порта, а за jump-хостами — проба изнутри.

Прямой хост проверяем TCP-коннектом к порту, а не полным входом. Хосты за
ProxyJump прямого маршрута не имеют — их пробуем изнутри jump-хоста одним
ssh-вызовом. Флаг deep добавляет реальный вход `ssh ... true`: он отвечает уже
не на «порт открыт», а на «ключ принят, внутрь пускают».

Логика синхронная (socket и subprocess): сервер вызывает её через поток, чтобы
не блокировать цикл событий.
"""

import logging
import shlex
import socket
import subprocess
from collections import defaultdict
from collections.abc import Callable
from functools import partial
from time import sleep, time

from ..config.constants import PROBE_PATH_BUDGET, PROBE_PATH_PAUSE, PROBE_RETRY_ERRNOS
from ..config.environment import Settings
from ..schemas import Availability, Host, as_availability
from .hosts import pairs, resolve
from .parallel import fan_out
from .ssh import run_sync, ssh_argv

log = logging.getLogger(__name__)

Statuses = dict[str, Availability]


def _reachable(host: Host, connect_timeout: float) -> bool:
    """Открыт ли порт: TCP-коннект, а не полный вход с аутентификацией.

    Ошибку маршрутизации перепроверяем до `PROBE_PATH_BUDGET` — на оверлее она
    значит лишь «путь ещё не поднят». Отказ и таймаут перепроверять нечего: это
    ответ.
    """
    deadline = time() + PROBE_PATH_BUDGET
    while True:
        try:
            with socket.create_connection((host.hostname, host.port), connect_timeout):
                return True
        except OSError as err:
            if err.errno not in PROBE_RETRY_ERRNOS or time() >= deadline:
                return False
        sleep(PROBE_PATH_PAUSE)


def _jump_script(group: list[Host]) -> str:
    """Скрипт проверки портов группы на jump-хосте (нужны bash и `timeout`).

    Печатает по строке «алиас статус» словами из `Availability`. hostname, port
    и alias экранируются `shlex.quote`: значения из конфига не должны
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
            f"then echo {alias_q} available; else echo {alias_q} unavailable; fi"
        )
    return "; ".join(checks)


def _probe_direct(host: Host, s: Settings) -> Statuses:
    return {host.alias: "available" if _reachable(host, s.connect_timeout) else "unavailable"}


def _probe_via(jump: Host, group: list[Host], s: Settings) -> Statuses:
    """Группа за одним jump-хостом: прямого маршрута нет, пробуем изнутри его сети.

    Недоступный jump означает недоступность всего за ним — это вывод, а не догадка.
    TCP-проба самого jump возможна, только если он прямой: за своим jump-хостом
    его порт отсюда не виден, и решает уже ssh-вызов.
    """
    down: Statuses = dict.fromkeys((h.alias for h in group), "unavailable")
    # ssh к самому jump-хосту ленивость оверлея задевает так же, но повтора внутри
    # себя не имеет: холодный путь утащил бы в unavailable всю группу за ним.
    if not jump.proxyjump and not _reachable(jump, s.connect_timeout):
        return down
    try:
        done = run_sync([*ssh_argv(jump, s), _jump_script(group)], s.jump_timeout)
    except (OSError, subprocess.SubprocessError):
        return down
    if done.returncode != 0:
        return down
    seen = pairs(done.stdout)
    return {h.alias: as_availability(seen.get(h.alias)) for h in group}


def measure(hosts: list[Host], s: Settings) -> Statuses:
    """Все пробы разом; время ограничено таймаутами самих проб.

    Returns:
        Статус по каждому алиасу из `hosts`.
    """
    known = {h.alias: h for h in hosts}
    behind: dict[str, list[Host]] = defaultdict(list)
    for host in hosts:
        if host.proxyjump:
            behind[host.proxyjump].append(host)

    statuses: Statuses = {}
    probes: list[Callable[[], Statuses]] = [partial(_probe_direct, h, s) for h in hosts if not h.proxyjump]
    # jump обычно и сам описан в конфиге — тогда он уже разобран, второй `ssh -G`
    # не нужен. Нераспознанный jump делает всю группу за ним недоступной.
    for alias, group in behind.items():
        via = known.get(alias) or resolve(alias, s.ssh_g_timeout)
        if via is None:
            statuses |= dict.fromkeys((h.alias for h in group), "unavailable")
        else:
            probes.append(partial(_probe_via, via, group, s))

    for result in fan_out(lambda probe: probe(), probes):
        statuses |= result
    log.debug("пробы: %s", statuses)
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
