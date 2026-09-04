"""Сервисы роутера хостов: оркестрация статусов доступности.

Синхронная работа с сетью, кэшом и `ssh -G` собрана здесь, обработчики зовут её
через поток. Разрешение алиаса (`require_host`, `resolve_known`) — общая
инфраструктура (`hosts`), а не часть этого роутера.
"""

from ...core import cache
from ...core.config.environment import get_settings
from ...core.utils.hosts import discover, resolve_known
from ...core.utils.parallel import fan_out
from ...core.utils.probe import Statuses, deep_check, measure
from .schemas import CheckResult, HostStatus, ListHostsResult


def list_statuses(refresh: bool) -> ListHostsResult:
    """Хосты конфига, их статусы и возраст данных.

    Свежий кэш отдаём как есть; протухший или `refresh` — мерим заново и пишем.
    Хост, которого в кэше нет, получает «unknown».
    """
    s = get_settings()
    hosts = discover(s.ssh_g_timeout)
    age, statuses = cache.read(s)
    if refresh or age >= s.cache_ttl:
        statuses = measure(hosts, s)
        cache.write(statuses, s)
        age = 0.0
    return ListHostsResult(
        checked_ago=age,
        hosts=[HostStatus(**h.model_dump(), status=statuses.get(h.alias, "unknown")) for h in hosts],
    )


def check_statuses(aliases: list[str], deep: bool) -> list[CheckResult]:
    """Проба указанных алиасов мимо кэша, по одному результату на алиас.

    Неизвестный алиас (нет в конфиге) — статус «unknown» с пояснением. Без deep
    статус даёт TCP-проба; с deep — только реальный вход, причина отказа в
    деталях, а TCP-проба не нужна: вход отвечает и на её вопрос.
    """
    s = get_settings()
    hosts = resolve_known(aliases, s.ssh_g_timeout)
    statuses: Statuses = {}
    logins: dict[str, tuple[bool, str]] = {}
    if deep:
        logins = dict(zip(hosts, fan_out(lambda h: deep_check(h, s), hosts.values()), strict=True))
    else:
        statuses = measure(list(hosts.values()), s)
    results = []
    for alias in aliases:
        if alias not in hosts:
            results.append(CheckResult(alias=alias, status="unknown", detail="нет в ~/.ssh/config"))
        elif deep:
            ok, reason = logins[alias]
            results.append(CheckResult(alias=alias, status="available" if ok else "unavailable", detail=reason))
        else:
            results.append(CheckResult(alias=alias, status=statuses[alias], detail=""))
    return results
