"""Probes: jump script without injection, a host behind a chain of jump hosts."""

import errno
import socket
import subprocess
from pathlib import Path
from types import TracebackType

import pytest

from mcp_hostops.core.config.environment import Settings
from mcp_hostops.core.schemas import Host
from mcp_hostops.core.utils import probe
from mcp_hostops.core.utils.probe import _jump_script


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


class _OKSock:
    def __enter__(self) -> "_OKSock":
        return self

    def __exit__(
        self, _exc_type: type[BaseException] | None, _exc: BaseException | None, _tb: TracebackType | None
    ) -> None:
        return None


def _direct(alias: str = "d") -> Host:
    return _host("10.0.0.1", alias=alias, proxyjump="")


def _completed(rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, stdout=out, stderr=err)


def test_reachable_open_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda _addr, _timeout: _OKSock())
    assert probe._reachable(_direct(), 1.0) is True


def test_reachable_refused_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_addr: tuple[str, int], _timeout: float) -> _OKSock:
        raise OSError(errno.ECONNREFUSED, "refused")

    monkeypatch.setattr(socket, "create_connection", refuse)
    assert probe._reachable(_direct(), 1.0) is False


def test_reachable_retries_routing_error_then_up(monkeypatch: pytest.MonkeyPatch) -> None:
    # A routing error means "path not up yet" on an overlay — retried, not final.
    attempts = iter([OSError(errno.EHOSTUNREACH, "no route"), None])

    def flaky(_addr: tuple[str, int], _timeout: float) -> _OKSock:
        exc = next(attempts)
        if exc is not None:
            raise exc
        return _OKSock()

    monkeypatch.setattr(socket, "create_connection", flaky)
    monkeypatch.setattr(probe, "sleep", lambda _s: None)
    assert probe._reachable(_direct(), 1.0) is True


def test_probe_direct_maps_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "_reachable", lambda _h, _t: True)
    assert probe._probe_direct(_direct(alias="d"), Settings()) == {"d": "available"}


def test_probe_via_jump_down(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(probe, "_reachable", lambda _h, _t: False)
    jump = _host("10.0.0.1", alias="j", proxyjump="")
    assert probe._probe_via(jump, [_host("10.0.0.5", alias="in")], Settings()) == {"in": "unavailable"}


def test_probe_via_ssh_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(probe, "_reachable", lambda _h, _t: True)

    def boom(_argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        raise OSError("ssh gone")

    monkeypatch.setattr(probe, "run_sync", boom)
    jump = _host("10.0.0.1", alias="j", proxyjump="")
    assert probe._probe_via(jump, [_host("10.0.0.5", alias="in")], Settings()) == {"in": "unavailable"}


def test_probe_via_nonzero_rc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(probe, "_reachable", lambda _h, _t: True)
    monkeypatch.setattr(probe, "run_sync", lambda _argv, _t: _completed(1))
    jump = _host("10.0.0.1", alias="j", proxyjump="")
    assert probe._probe_via(jump, [_host("10.0.0.5", alias="in")], Settings()) == {"in": "unavailable"}


def test_measure_direct_and_jump_groups(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # up — direct; jmp — a jump described in the config (its group is probed via ssh);
    # lost — behind an unresolved jump, so the whole group is unavailable.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(probe, "_reachable", lambda _h, _t: True)
    monkeypatch.setattr(probe, "resolve", lambda _alias, _t: None)
    monkeypatch.setattr(probe, "run_sync", lambda _argv, _t: _completed(0, out="hvia available\n"))
    up = _host("10.0.0.2", alias="up", proxyjump="")
    jmp = _host("10.0.0.1", alias="jmp", proxyjump="")
    hvia = _host("10.0.0.5", alias="hvia", proxyjump="jmp")
    lost = _host("10.0.0.6", alias="lost", proxyjump="ghost")
    got = probe.measure([up, jmp, hvia, lost], Settings())
    assert got["up"] == "available"
    assert got["jmp"] == "available"
    assert got["hvia"] == "available"
    assert got["lost"] == "unavailable"


def test_deep_check_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(probe, "run_sync", lambda _argv, _t: _completed(0))
    assert probe.deep_check(_direct(), Settings()) == (True, "")


def test_deep_check_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    def boom(_argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=timeout)

    monkeypatch.setattr(probe, "run_sync", boom)
    assert probe.deep_check(_direct(), Settings()) == (False, "login timed out")


def test_deep_check_ssh_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    def boom(_argv: list[str], _t: float) -> subprocess.CompletedProcess[str]:
        raise OSError("no ssh")

    monkeypatch.setattr(probe, "run_sync", boom)
    ok, reason = probe.deep_check(_direct(), Settings())
    assert ok is False
    assert "ssh failed to start" in reason


def test_deep_check_denied_reports_last_stderr_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(probe, "run_sync", lambda _argv, _t: _completed(255, err="banner\nPermission denied"))
    assert probe.deep_check(_direct(), Settings()) == (False, "Permission denied")


def test_deep_check_nonzero_without_stderr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(probe, "run_sync", lambda _argv, _t: _completed(7))
    assert probe.deep_check(_direct(), Settings()) == (False, "exit code 7")
