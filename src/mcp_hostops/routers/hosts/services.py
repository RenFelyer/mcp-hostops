"""Hosts router services: availability status orchestration.

Synchronous work with the network, cache and `ssh -G` lives here; handlers call
it through a thread. Alias resolution (`require_host`, `resolve_known`) is
shared infrastructure (`hosts`), not part of this router.
"""

import contextlib

from pydantic import TypeAdapter, ValidationError

from ...core import store
from ...core.config.environment import Settings, get_settings
from ...core.schemas import Availability
from ...core.utils.hosts import discover, resolve_known
from ...core.utils.parallel import fan_out
from ...core.utils.probe import Statuses, deep_check, measure
from .schemas import CheckResult, HostStatus, ListHostsResult

# The availability cache lives in the runtime tier: an optimization, remeasured on any miss.
_CACHE = "hosts-status.json"
_HOSTS = TypeAdapter(dict[str, Availability])


def _read_cache(s: Settings) -> tuple[float, dict[str, Availability]]:
    age, data = store.read_stamped(s, "runtime", _CACHE)
    try:
        return age, _HOSTS.validate_python(data.get("hosts"))
    except ValidationError:
        return float("inf"), {}


def list_statuses(refresh: bool) -> ListHostsResult:
    """Config hosts, their statuses and the data's age.

    A fresh cache is returned as is; a stale one, or `refresh`, is re-measured
    and written back. A host missing from the cache gets "unknown".
    """
    s = get_settings()
    hosts = discover(s.ssh_g_timeout)
    age, statuses = _read_cache(s)
    if refresh or age >= s.cache_ttl:
        statuses = measure(hosts, s)
        with contextlib.suppress(OSError):
            store.write_stamped(s, "runtime", _CACHE, {"hosts": dict(statuses)})
        age = 0.0
    return ListHostsResult(
        checked_ago=age,
        hosts=[HostStatus(**h.model_dump(), status=statuses.get(h.alias, "unknown")) for h in hosts],
    )


def check_statuses(aliases: list[str], deep: bool) -> list[CheckResult]:
    """Probe the given aliases bypassing the cache, one result per alias.

    An unknown alias (not in the config) gets an "unknown" status with a note.
    Without deep, a TCP probe gives the status; with deep, only an actual login
    is done, failure reason in detail — a TCP probe is unneeded, since the
    login answers its question too.
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
            results.append(CheckResult(alias=alias, status="unknown", detail="not in ~/.ssh/config"))
        elif deep:
            ok, reason = logins[alias]
            results.append(CheckResult(alias=alias, status="available" if ok else "unavailable", detail=reason))
        else:
            results.append(CheckResult(alias=alias, status=statuses[alias], detail=""))
    return results
