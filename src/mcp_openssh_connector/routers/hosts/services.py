"""Сервисы роутера хостов: оркестрация статусов доступности.

Синхронная работа с сетью, кэшом и `ssh -G` собрана здесь, обработчики зовут её
через поток. Разрешение алиаса (`require_host`, `host_detail`) — общая
инфраструктура (`hosts`), а не часть этого роутера.
"""

from ...core.cache import read as cache_read
from ...core.cache import write as cache_write
from ...core.config import get_settings
from ...core.schemas import AVAILABLE, UNAVAILABLE, UNKNOWN, as_availability
from ...core.utils.hosts import discover, read_aliases, resolve
from ...core.utils.probe import deep_check, measure
from .schemas import CheckResult, HostStatus, ListHostsResult


def list_statuses(refresh: bool) -> ListHostsResult:
    """Хосты конфига, их статусы и возраст данных.

    Свежий кэш отдаём как есть; протухший или `refresh` — мерим заново и пишем.
    Хост, которого в кэше нет, получает «unknown».
    """
    s = get_settings()
    hosts = discover(s.ssh_g_timeout)
    age, cached = cache_read(s)
    if refresh or age >= s.cache_ttl:
        statuses = measure(hosts, s)
        cache_write(statuses, s)
        age = 0.0
    else:
        statuses = {h.alias: as_availability(cached.get(h.alias, "")) for h in hosts}
    return ListHostsResult(
        checked_ago=age,
        hosts=[HostStatus(**h.model_dump(), status=statuses[h.alias]) for h in hosts],
    )


def check_statuses(aliases: list[str], deep: bool) -> list[CheckResult]:
    """Проба указанных алиасов мимо кэша, по одному результату на алиас.

    Неизвестный алиас (нет в конфиге) — статус «unknown» с пояснением. Мелкая
    проба даёт статус доступности; deep добавляет реальный вход и в детали кладёт
    причину отказа.
    """
    s = get_settings()
    known = read_aliases()
    hosts = {alias: resolve(alias, s.ssh_g_timeout) for alias in dict.fromkeys(aliases) if alias in known}
    statuses = measure([h for h in hosts.values() if h], s)
    results = []
    for alias in aliases:
        host = hosts.get(alias)
        if host is None:
            results.append(CheckResult(alias=alias, status=UNKNOWN, detail="нет в ~/.ssh/config"))
            continue
        status, detail = statuses[alias], ""
        if deep:
            ok, reason = deep_check(host, s)
            status = AVAILABLE if ok else UNAVAILABLE
            detail = "вход подтверждён" if ok else reason
        results.append(CheckResult(alias=alias, status=status, detail=detail))
    return results
