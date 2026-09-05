"""Parsing ~/.ssh/config: reading aliases without calling ssh."""

import subprocess
from pathlib import Path

import anyio
import pytest

from mcp_hostops.core.errors import UserError
from mcp_hostops.core.schemas import Host
from mcp_hostops.core.utils import hosts as hosts_mod
from mcp_hostops.core.utils.hosts import read_aliases


def test_read_aliases_order_dedup_patterns(tmp_path: Path) -> None:
    # beta gamma — multiple names; *.example.com and with?wild — wildcards (skipped);
    # !neg — negation (skipped); commented-out Host — not a host; repeated alpha
    # — no duplicate; delta with a trailing comment; Host=eq — the `=` form.
    config = tmp_path / "config"
    config.write_text(
        "Host alpha\n"
        "  HostName 10.0.0.1\n"
        "Host beta gamma\n"
        "Host *.example.com\n"
        "Host with?wild\n"
        "Host !neg other\n"
        "# Host commented\n"
        "Host alpha\n"
        "Host delta  # trailing comment\n"
        "Host=eq\n",
        encoding="utf-8",
    )
    assert read_aliases(config) == ["alpha", "beta", "gamma", "other", "delta", "eq"]


def test_read_aliases_empty(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.write_text("# only comments\n", encoding="utf-8")
    assert read_aliases(config) == []


def test_read_aliases_follows_include(tmp_path: Path) -> None:
    inc = tmp_path / "extra"
    inc.write_text("Host inc_a inc_b\n", encoding="utf-8")
    config = tmp_path / "config"
    config.write_text(f"Host main\nInclude {inc}\n", encoding="utf-8")
    assert read_aliases(config) == ["main", "inc_a", "inc_b"]


def test_read_aliases_include_glob(tmp_path: Path) -> None:
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    (conf_d / "a.conf").write_text("Host from_a\n", encoding="utf-8")
    (conf_d / "b.conf").write_text("Host from_b\n", encoding="utf-8")
    config = tmp_path / "config"
    config.write_text(f"Host main\nInclude {conf_d}/*.conf\n", encoding="utf-8")
    assert read_aliases(config) == ["main", "from_a", "from_b"]


def test_read_aliases_include_cycle(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_text(f"Host ha\nInclude {b}\n", encoding="utf-8")
    b.write_text(f"Host hb\nInclude {a}\n", encoding="utf-8")
    assert read_aliases(a) == ["ha", "hb"]  # the cycle didn't loop forever


def test_read_aliases_include_skips_dotfiles_by_wildcard(tmp_path: Path) -> None:
    # ssh's glob(3) doesn't match a dot under a wildcard; `.hidden.conf` is skipped.
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    (conf_d / "a.conf").write_text("Host shown\n", encoding="utf-8")
    (conf_d / ".hidden.conf").write_text("Host hidden\n", encoding="utf-8")
    config = tmp_path / "config"
    config.write_text(f"Include {conf_d}/*.conf\n", encoding="utf-8")
    assert read_aliases(config) == ["shown"]


def test_expand_include_home_and_relative_patterns() -> None:
    # `~` and relative patterns resolve without error; a pattern that matches
    # nothing gives an empty list. Absolute patterns are covered by the tests above.
    assert hosts_mod._expand_include("~/no-such-mcp-hostops-test-*") == []
    assert hosts_mod._expand_include("no-such-mcp-hostops-test-*") == []


def _completed(rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, stdout=out, stderr=err)


def test_resolve_parses_ssh_g(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        hosts_mod, "run_sync", lambda _argv, _t: _completed(0, "hostname 10.0.0.9\nuser bob\nport 2222\n")
    )
    host = hosts_mod.resolve("srv", 1.0)
    assert host is not None
    assert (host.hostname, host.user, host.port) == ("10.0.0.9", "bob", 2222)


def test_resolve_nonzero_exit_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # rc != 0 -> no fields -> a Host without hostname/port fails validation -> None.
    monkeypatch.setattr(hosts_mod, "run_sync", lambda _argv, _t: _completed(255, err="boom"))
    assert hosts_mod.resolve("srv", 1.0) is None


def test_resolve_ssh_failure_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_argv: list[str], _t: float) -> subprocess.CompletedProcess[str]:
        raise OSError("no ssh binary")

    monkeypatch.setattr(hosts_mod, "run_sync", boom)
    assert hosts_mod.resolve("srv", 1.0) is None


def test_resolve_known_filters_to_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hosts_mod, "read_aliases", lambda: ["known"])
    monkeypatch.setattr(hosts_mod, "run_sync", lambda _argv, _t: _completed(0, "hostname 10.0.0.1\nuser u\nport 22\n"))
    got = hosts_mod.resolve_known(["known", "foreign"], 1.0)
    assert list(got) == ["known"]


def test_discover_resolves_all_config_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hosts_mod, "read_aliases", lambda: ["a"])
    monkeypatch.setattr(hosts_mod, "run_sync", lambda _argv, _t: _completed(0, "hostname 10.0.0.1\nuser u\nport 22\n"))
    got = hosts_mod.discover(1.0)
    assert [h.alias for h in got] == ["a"]


def test_require_host_returns_known(monkeypatch: pytest.MonkeyPatch) -> None:
    host = Host(alias="a", hostname="10.0.0.1", user="u", port=22, proxyjump="")
    monkeypatch.setattr(hosts_mod, "resolve_known", lambda _aliases, _t: {"a": host})
    assert anyio.run(hosts_mod.require_host, "a") == host


def test_require_host_unknown_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hosts_mod, "resolve_known", lambda _aliases, _t: {})
    with pytest.raises(UserError, match="not described"):
        anyio.run(hosts_mod.require_host, "ghost")
