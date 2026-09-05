"""Status orchestration: deep skips TCP probes, an unknown alias is unknown."""

import pytest

from mcp_openssh_connector.core import cache
from mcp_openssh_connector.core.config.environment import Settings
from mcp_openssh_connector.core.schemas import Host
from mcp_openssh_connector.routers.hosts import services


def _host(alias: str) -> Host:
    return Host(alias=alias, hostname="10.0.0.1", user="u", port=22, proxyjump="")


def test_check_statuses_deep_skips_tcp_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    hosts = {"a": _host("a"), "b": _host("b")}
    monkeypatch.setattr(services, "resolve_known", lambda _aliases, _timeout: hosts)
    monkeypatch.setattr(services, "measure", lambda *_: pytest.fail("TCP probe not needed for deep"))
    monkeypatch.setattr(services, "deep_check", lambda h, _s: (h.alias == "a", "" if h.alias == "a" else "refused"))

    got = services.check_statuses(["a", "b", "nope"], deep=True)
    assert [(r.alias, r.status, r.detail) for r in got] == [
        ("a", "available", ""),
        ("b", "unavailable", "refused"),
        ("nope", "unknown", "not in ~/.ssh/config"),
    ]


def test_check_statuses_shallow_uses_measure(monkeypatch: pytest.MonkeyPatch) -> None:
    hosts = {"a": _host("a")}
    monkeypatch.setattr(services, "resolve_known", lambda _aliases, _timeout: hosts)
    monkeypatch.setattr(services, "measure", lambda _hosts, _s: {"a": "available"})
    monkeypatch.setattr(services, "deep_check", lambda *_: pytest.fail("login not needed without deep"))

    got = services.check_statuses(["a"], deep=False)
    assert [(r.alias, r.status) for r in got] == [("a", "available")]


def test_list_statuses_fresh_cache_and_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    hosts = [_host("a"), _host("b")]
    monkeypatch.setattr(services, "discover", lambda _timeout: hosts)
    monkeypatch.setattr(cache, "read", lambda _s: (1.0, {"a": "available"}))
    monkeypatch.setattr(services, "measure", lambda *_: pytest.fail("fresh cache — no probes"))
    monkeypatch.setattr(services, "get_settings", Settings)

    got = services.list_statuses(refresh=False)
    assert got.checked_ago == 1.0
    assert [(h.alias, h.status) for h in got.hosts] == [("a", "available"), ("b", "unknown")]
