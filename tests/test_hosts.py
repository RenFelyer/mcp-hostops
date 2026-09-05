"""Parsing ~/.ssh/config: reading aliases without calling ssh."""

from pathlib import Path

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
