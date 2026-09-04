"""Пробы: jump-скрипт без инъекций, хост за цепочкой jump-хостов."""

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
    # Выполняем сгенерированный скрипт локально: /dev/tcp к мусорному «хосту»
    # провалится, а инъекция `touch` не должна сработать — hostname экранирован.
    subprocess.run(["bash", "-c", script], capture_output=True, timeout=10, check=False)
    assert not marker.exists()


def test_jump_script_echo_parseable() -> None:
    # Для нормального алиаса вывод «alias status» разбирается по первому пробелу.
    script = _jump_script([_host("10.0.0.1", alias="ok")])
    assert "echo ok available" in script
    assert "echo ok unavailable" in script


def test_probe_via_chained_jump_skips_tcp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Jump-хост сам за своим jump: его порт отсюда не виден, TCP-проба
    # утащила бы всю группу в unavailable; решать должен ssh-вызов.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    def no_tcp(_host: Host, _timeout: float) -> bool:
        pytest.fail("TCP-проба за jump недопустима")

    def fake_ssh(_argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout="inner available\n", stderr="")

    monkeypatch.setattr(probe, "_reachable", no_tcp)
    monkeypatch.setattr(probe, "run_sync", fake_ssh)
    inner = _host("10.0.0.5", alias="inner", proxyjump="mid")
    mid = _host("10.0.0.1", alias="mid", proxyjump="edge")
    assert probe._probe_via(mid, [inner], Settings()) == {"inner": "available"}


def test_probe_via_garbage_output_is_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Вывод скрипта не в словаре статусов — не гадать, а сказать «неизвестно».
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(probe, "_reachable", lambda _host, _timeout: True)

    def fake_ssh(_argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout="motd banner\ninner maybe\n", stderr="")

    monkeypatch.setattr(probe, "run_sync", fake_ssh)
    jump = _host("10.0.0.1", alias="jump", proxyjump="")
    assert probe._probe_via(jump, [_host("10.0.0.5", alias="inner")], Settings()) == {"inner": "unknown"}
