"""Probes: jump script without injection, a host behind a chain of jump hosts."""

import subprocess
from pathlib import Path

import pytest

from mcp_openssh_connector.core.config.environment import Settings
from mcp_openssh_connector.core.schemas import Host
from mcp_openssh_connector.core.utils import probe
from mcp_openssh_connector.core.utils.probe import _jump_script


def _host(hostname: str, alias: str = "x", proxyjump: str = "j") -> Host:
    return Host(alias=alias, hostname=hostname, user="u", port=22, proxyjump=proxyjump)


def test_jump_script_no_injection(tmp_path: Path) -> None:
    marker = tmp_path / "PWNED"
    script = _jump_script([_host(f"1.2.3.4; touch {marker}")])
    # Run the generated script locally: /dev/tcp to the garbage "host" will
    # fail, and the `touch` injection must not fire — hostname is escaped.
    subprocess.run(["bash", "-c", script], capture_output=True, timeout=10, check=False)
    assert not marker.exists()


def test_jump_script_echo_parseable() -> None:
    # For a normal alias, the "alias status" output is split on the first space.
    script = _jump_script([_host("10.0.0.1", alias="ok")])
    assert "echo ok available" in script
    assert "echo ok unavailable" in script


def test_probe_via_chained_jump_skips_tcp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The jump host is itself behind its own jump: its port isn't visible from
    # here, a TCP probe would drag the whole group into unavailable; the ssh
    # call must decide instead.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    def no_tcp(_host: Host, _timeout: float) -> bool:
        pytest.fail("a TCP probe behind a jump is not allowed")

    def fake_ssh(_argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout="inner available\n", stderr="")

    monkeypatch.setattr(probe, "_reachable", no_tcp)
    monkeypatch.setattr(probe, "run_sync", fake_ssh)
    inner = _host("10.0.0.5", alias="inner", proxyjump="mid")
    mid = _host("10.0.0.1", alias="mid", proxyjump="edge")
    assert probe._probe_via(mid, [inner], Settings()) == {"inner": "available"}


def test_probe_via_garbage_output_is_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Script output not in the status dict — don't guess, say "unknown".
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(probe, "_reachable", lambda _host, _timeout: True)

    def fake_ssh(_argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout="motd banner\ninner maybe\n", stderr="")

    monkeypatch.setattr(probe, "run_sync", fake_ssh)
    jump = _host("10.0.0.1", alias="jump", proxyjump="")
    assert probe._probe_via(jump, [_host("10.0.0.5", alias="inner")], Settings()) == {"inner": "unknown"}
