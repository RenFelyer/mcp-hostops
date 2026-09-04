"""Регресс: run_command сохраняет вывод при таймауте."""

from collections.abc import Sequence

import anyio
import pytest

from mcp_openssh_connector.core.schemas import Host
from mcp_openssh_connector.routers.commands import services
from mcp_openssh_connector.routers.commands.schemas import RunResult


class _FakeReceive:
    """Поток: отдаёт куски, потом EOF либо зависает (симуляция зависшей команды)."""

    def __init__(self, chunks: Sequence[bytes], *, hang: bool) -> None:
        self._chunks = list(chunks)
        self._hang = hang

    def __aiter__(self) -> "_FakeReceive":
        return self

    async def __anext__(self) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        if self._hang:
            await anyio.sleep_forever()
        raise StopAsyncIteration


class _FakeSend:
    async def send(self, data: bytes) -> None:
        pass

    async def aclose(self) -> None:
        pass


class _FakeProcess:
    """Минимальный процесс: печатает строку в stdout и зависает, пока его не убьют."""

    def __init__(self) -> None:
        self.stdin = _FakeSend()
        self.stdout = _FakeReceive([b"partial-output\n"], hang=True)
        self.stderr = _FakeReceive([], hang=False)
        self.returncode: int | None = None

    async def __aenter__(self) -> "_FakeProcess":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.kill()

    async def wait(self) -> int:
        if self.returncode is None:
            await anyio.sleep_forever()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        if self.returncode is None:
            self.returncode = -15

    def kill(self) -> None:
        if self.returncode is None:
            self.returncode = -9


def test_run_command_keeps_output_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_open(_argv: list[str]) -> _FakeProcess:
        return _FakeProcess()

    async def fake_require(alias: str) -> Host:
        return Host(alias=alias, hostname="127.0.0.1", user="u", port=22, proxyjump="")

    monkeypatch.setattr(anyio, "open_process", fake_open)
    monkeypatch.setattr(services, "require_host", fake_require)

    async def scenario() -> RunResult:
        return await services.run_command("h", "echo x; sleep 99", "/work", 0.3, "false", None)

    result = anyio.run(scenario)
    assert result.timed_out is True
    assert result.exit_code is None
    assert "partial-output" in result.stdout  # вывод до таймаута не потерян
