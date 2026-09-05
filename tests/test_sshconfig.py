"""Router for managing ~/.ssh/config: managed file, known_hosts, copy_id — no network.

`ssh -G`, ssh-copy-id and sshpass are substituted: we check block parsing and
assembly, wiring via Include, known_hosts cleanup with a real ssh-keygen, and copy_id's argv.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from mcp_hostops.core.config.environment import Settings
from mcp_hostops.core.errors import UserError
from mcp_hostops.core.schemas import Host
from mcp_hostops.core.utils.hosts import read_aliases
from mcp_hostops.routers.sshconfig import services
from mcp_hostops.routers.sshconfig.schemas import ManagedHost


def _settings(tmp: Path) -> Settings:
    return Settings(
        ssh_config_file=tmp / "config",
        managed_config_file=tmp / "config.d" / "mcp.conf",
        known_hosts_file=tmp / "known_hosts",
        secret_dir=tmp,
    )


def _use(s: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "get_settings", lambda: s)


def _fake_resolve(alias: str, _timeout: float) -> Host:
    return Host(alias=alias, hostname="10.0.0.5", user="admin", port=22, proxyjump="")


# ── pure block logic ────────────────────────────────────────────────────────────


def test_render_block_canonical() -> None:
    spec = ManagedHost(
        alias="box", hostname="h", user="u", port=2222, identity_file="~/.ssh/k", extra={"ForwardAgent": "yes"}
    )
    assert services._render_block(spec) == (
        "Host box\n    HostName h\n    User u\n    Port 2222\n    IdentityFile ~/.ssh/k\n    ForwardAgent yes\n"
    )


def test_render_block_omits_empty_optional() -> None:
    # User, IdentityFile, ProxyJump are empty — their lines are absent; Port is always present.
    assert services._render_block(ManagedHost(alias="a", hostname="h")) == "Host a\n    HostName h\n    Port 22\n"


def test_parse_blocks_roundtrip() -> None:
    text = "# header\n\nHost a\n    HostName h1\n    Port 22\n\nHost=b\n    HostName h2\n"
    blocks = services._parse_blocks(text)
    assert [aliases for aliases, _ in blocks] == [["a"], ["b"]]
    assert blocks[0][1] == "Host a\n    HostName h1\n    Port 22\n"


def test_check_alias_rejects_bad() -> None:
    for bad in ("", "with space", "*.example", "-lead", "!neg", "has#hash"):
        with pytest.raises(UserError):
            services._check_alias(bad)
    services._check_alias("ok_host.1")  # no exception


# ── add_host ─────────────────────────────────────────────────────────────────────


def test_add_host_creates_managed_and_include(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path)
    _use(s, monkeypatch)
    monkeypatch.setattr(services, "resolve", _fake_resolve)

    first = services.add_host(ManagedHost(alias="box", hostname="10.0.0.5", user="admin"))
    assert first.include_added is True
    assert first.host is not None
    assert first.host.hostname == "10.0.0.5"
    assert s.managed_config_file.stat().st_mode & 0o777 == 0o600
    assert "Host box\n    HostName 10.0.0.5\n    User admin\n    Port 22\n" in s.managed_config_file.read_text()
    assert f"Include {s.managed_config_file}" in s.ssh_config_file.read_text()
    assert "box" in read_aliases(s.ssh_config_file)  # visible via Include

    second = services.add_host(ManagedHost(alias="box2", hostname="h2"))
    assert second.include_added is False  # Include was already there
    assert s.ssh_config_file.read_text().count("Include") == 1


def test_add_host_replaces_own_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path)
    _use(s, monkeypatch)
    monkeypatch.setattr(services, "resolve", _fake_resolve)
    services.add_host(ManagedHost(alias="box", hostname="old"))
    services.add_host(ManagedHost(alias="box", hostname="new"))
    text = s.managed_config_file.read_text()
    assert text.count("Host box") == 1
    assert "HostName new" in text
    assert "HostName old" not in text


def test_add_host_refuses_manual_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path)
    _use(s, monkeypatch)
    monkeypatch.setattr(services, "resolve", _fake_resolve)
    s.ssh_config_file.parent.mkdir(parents=True, exist_ok=True)
    s.ssh_config_file.write_text("Host manual\n    HostName 1.2.3.4\n", encoding="utf-8")
    with pytest.raises(UserError, match="manually"):
        services.add_host(ManagedHost(alias="manual", hostname="y"))


def test_add_host_rejects_empty_hostname(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use(_settings(tmp_path), monkeypatch)
    with pytest.raises(UserError, match="hostname"):
        services.add_host(ManagedHost(alias="box", hostname="   "))


# ── remove_host ──────────────────────────────────────────────────────────────────


def test_remove_host_drops_block_and_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path)
    _use(s, monkeypatch)
    monkeypatch.setattr(services, "resolve", _fake_resolve)
    monkeypatch.setattr(services, "_forget", lambda _host, _s: 3)
    services.add_host(ManagedHost(alias="box", hostname="10.0.0.5"))
    secret = s.secret_file("box")
    secret.write_text("pw", encoding="utf-8")

    res = services.remove_host("box", forget_known=True, drop_secret=True)
    assert res.known_hosts_removed == 3
    assert res.secret_removed is True
    assert not secret.exists()
    assert "Host box" not in s.managed_config_file.read_text()

    with pytest.raises(UserError, match="managed"):
        services.remove_host("box", forget_known=False, drop_secret=False)


def test_remove_host_keeps_known_when_flag_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path)
    _use(s, monkeypatch)
    monkeypatch.setattr(services, "resolve", _fake_resolve)
    monkeypatch.setattr(services, "_forget", lambda *_: pytest.fail("known_hosts must not be touched without the flag"))
    services.add_host(ManagedHost(alias="box", hostname="10.0.0.5"))
    res = services.remove_host("box", forget_known=False, drop_secret=False)
    assert res.known_hosts_removed == 0
    assert res.secret_removed is False


# ── forget_host and known_hosts ───────────────────────────────────────────────────


def _known_hosts(tmp: Path, names: list[str]) -> Path:
    """known_hosts with a real entry for each name (the key is generated by ssh-keygen)."""
    key = tmp / "seed"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    keytype, blob, *_ = key.with_suffix(".pub").read_text().split()
    kh = tmp / "known_hosts"
    kh.write_text("".join(f"{name} {keytype} {blob}\n" for name in names), encoding="utf-8")
    return kh


def test_keygen_remove_counts_lines(tmp_path: Path) -> None:
    kh = _known_hosts(tmp_path, ["10.0.0.5", "keep.example"])
    assert services._keygen_remove(["10.0.0.5"], kh) == 1
    assert "keep.example" in kh.read_text()
    assert "10.0.0.5" not in kh.read_text()


def test_keygen_remove_missing_file(tmp_path: Path) -> None:
    assert services._keygen_remove(["x"], tmp_path / "absent") == 0


def test_forget_host_by_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path)
    _use(s, monkeypatch)
    monkeypatch.setattr(
        services, "resolve", lambda a, _t: Host(alias=a, hostname="10.0.0.5", user="u", port=22, proxyjump="")
    )
    _known_hosts(tmp_path, ["10.0.0.5", "other"])  # writes to s.known_hosts_file
    res = services.forget_host("box")
    assert res.target == "10.0.0.5"
    assert res.removed == 1


# ── copy_id ──────────────────────────────────────────────────────────────────────


def _config_with(alias: str, s: Settings) -> None:
    s.ssh_config_file.parent.mkdir(parents=True, exist_ok=True)
    s.ssh_config_file.write_text(f"Host {alias}\n    HostName h\n", encoding="utf-8")


def test_copy_id_builds_argv_and_masks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path)
    _use(s, monkeypatch)
    _config_with("box", s)
    secret = s.secret_file("box")
    secret.write_text("s3cret\n", encoding="utf-8")
    secret.chmod(0o600)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    seen: list[str] = []

    def fake_run(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        seen.extend(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="Number of keys added: 1\ns3cret\n", stderr="")

    monkeypatch.setattr(services, "run_sync", fake_run)
    res = services.copy_id("box", "")
    assert seen == ["sshpass", "-f", str(secret), "ssh-copy-id", "-o", "StrictHostKeyChecking=accept-new", "box"]
    assert res.ok is True
    assert res.detail == "***"  # line equal to the password is masked


def test_copy_id_identity_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path)
    _use(s, monkeypatch)
    _config_with("box", s)
    s.secret_file("box").write_text("pw", encoding="utf-8")
    s.secret_file("box").chmod(0o600)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    seen: list[str] = []

    def fake_run(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        seen.extend(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(services, "run_sync", fake_run)
    services.copy_id("box", "~/.ssh/id_ed25519.pub")
    assert "-i" in seen
    assert "~/.ssh/id_ed25519.pub" in seen


def test_copy_id_unknown_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path)
    _use(s, monkeypatch)
    _config_with("box", s)
    with pytest.raises(UserError, match="not described"):
        services.copy_id("nope", "")


def test_copy_id_requires_sshpass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path)
    _use(s, monkeypatch)
    _config_with("box", s)
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "sshpass" else f"/usr/bin/{name}")
    with pytest.raises(UserError, match="sshpass"):
        services.copy_id("box", "")
