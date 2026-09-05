"""run_command: output on timeout is preserved, timeout cap is a call error."""

from collections.abc import Iterator, Sequence
from pathlib import Path
from types import TracebackType

import anyio
import pytest

from mcp_hostops.core.config.environment import get_settings
from mcp_hostops.core.errors import UserError
from mcp_hostops.core.schemas import Host
from mcp_hostops.routers.commands import services
from mcp_hostops.routers.commands.schemas import RunResult


class _FakeReceive:
    """Stream: yields chunks, then EOF or hangs (simulates a hung command)."""

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
    """Minimal process: prints a line to stdout and hangs until killed."""

    def __init__(self) -> None:
        self.stdin = _FakeSend()
        self.stdout = _FakeReceive([b"partial-output\n"], hang=True)
        self.stderr = _FakeReceive([], hang=False)
        self.returncode: int | None = None

    async def __aenter__(self) -> "_FakeProcess":
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
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


@pytest.fixture
def fake_ssh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Process and host are faked; runtime dir is temporary, settings reloaded."""

    async def fake_open(_argv: list[str]) -> _FakeProcess:
        return _FakeProcess()

    async def fake_require(alias: str) -> Host:
        return Host(alias=alias, hostname="127.0.0.1", user="u", port=22, proxyjump="")

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(anyio, "open_process", fake_open)
    monkeypatch.setattr(services, "require_host", fake_require)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("fake_ssh")
def test_run_command_keeps_output_on_timeout() -> None:
    async def scenario() -> RunResult:
        return await services.run_command("h", "echo x; sleep 99", "/work", 0.3, "false", None)

    result = anyio.run(scenario)
    assert result.timed_out is True
    assert result.exit_code is None
    assert "partial-output" in result.stdout  # output before the timeout is not lost


@pytest.mark.usefixtures("fake_ssh")
def test_run_command_rejects_timeout_above_cap() -> None:
    async def scenario() -> RunResult:
        return await services.run_command("h", "true", "~", 1e9, "false", None)

    with pytest.raises(UserError, match="cap"):
        anyio.run(scenario)
