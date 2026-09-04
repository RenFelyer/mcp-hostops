"""Регресс: jump-скрипт не даёт инъекции через значения конфига."""

import subprocess
from pathlib import Path

from mcp_openssh_connector.core.schemas import Host
from mcp_openssh_connector.core.utils.probe import _jump_script


def _host(hostname: str, alias: str = "x") -> Host:
    return Host(alias=alias, hostname=hostname, user="u", port=22, proxyjump="j")


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
