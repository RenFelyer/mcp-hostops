"""Job registry: incremental output, buffer cap, masking, history."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import anyio
import pytest

from mcp_hostops.core.config.environment import Settings, get_settings
from mcp_hostops.core.errors import UserError
from mcp_hostops.core.schemas import Host, SudoMode
from mcp_hostops.core.utils.ssh import Capture, Output
from mcp_hostops.routers.jobs import services as jobs_services
from mcp_hostops.routers.jobs.schemas import JobRef, JobSnapshot, JobStatus
from mcp_hostops.routers.jobs.services import Job, JobManager


def _job(job_id: str, status: JobStatus = "done") -> Job:
    return Job(
        ref=JobRef(id=job_id, host="h", command="c", cwd="/w", status=status),
        capture=Capture(1000, None),
    )


def test_snapshot_returns_delta_and_swaps_buffer() -> None:
    manager = JobManager()
    job = _job("1")
    job.capture.stdout.feed(b"first")
    manager._jobs["1"] = job

    async def scenario() -> tuple[str, str]:
        snap1 = await manager.snapshot("1", 0.0)
        job.capture.stdout.feed(b"second")  # new output after the read
        snap2 = await manager.snapshot("1", 0.0)
        return snap1.stdout, snap2.stdout

    first, second = anyio.run(scenario)
    assert first == "first"
    assert second == "second"  # only the delta, "first" was neither repeated nor lost


def test_snapshot_unknown_job_is_user_error() -> None:
    manager = JobManager()
    with pytest.raises(UserError, match="not found"):
        anyio.run(manager.snapshot, "nope", 0.0)
    assert manager.kill("nope") is False


def test_history_keeps_recent_finished_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "job_history", 2)
    manager = JobManager()
    for job_id in ("1", "2", "3"):
        manager._jobs[job_id] = _job(job_id)
    manager._jobs["4"] = _job("4", status="running")
    manager._forget_old()
    assert [ref.id for ref in manager.listing()] == ["2", "3", "4"]  # the live one always stays


def test_output_limit_marks_truncation() -> None:
    out = Output(5)
    out.feed(b"abc")
    out.feed(b"defg")
    assert out.take() == b"abcde"
    assert out.truncated is True
    out.feed(b"xyz")  # after take, the limit is counted afresh
    assert out.take() == b"xyz"


def test_capture_masks_password() -> None:
    capture = Capture(100, "s3cret")
    capture.stdout.feed(b"s3cret\r\nout")
    got = capture.drained()
    assert got.stdout == "***\nout"
    assert got.stdout_truncated is False


class _FakeCall:
    password: str | None = None


class _FakeProc:
    """Stand-in for the spawned ssh process; the faked execute ignores it."""


def _patch_ssh(monkeypatch: pytest.MonkeyPatch, execute_impl: Callable[..., Awaitable[int]]) -> None:
    """Replace the ssh machinery so a job runs without a real connection."""

    async def fake_require_host(alias: str) -> Host:
        return Host(alias=alias, hostname="h", user="u", port=22, proxyjump="")

    def fake_prepare(
        _host: Host, _command: str, _cwd: str, _sudo_mode: SudoMode, _stdin: str | None, _s: Settings
    ) -> _FakeCall:
        return _FakeCall()

    @asynccontextmanager
    async def fake_spawn(_call: _FakeCall) -> AsyncIterator[_FakeProc]:
        yield _FakeProc()

    monkeypatch.setattr(jobs_services, "require_host", fake_require_host)
    monkeypatch.setattr(jobs_services, "prepare", fake_prepare)
    monkeypatch.setattr(jobs_services, "spawn", fake_spawn)
    monkeypatch.setattr(jobs_services, "execute", execute_impl)


def test_start_runs_to_done(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_execute(_proc: _FakeProc, _call: _FakeCall, _capture: Capture) -> int:
        return 0

    _patch_ssh(monkeypatch, fake_execute)

    async def scenario() -> JobSnapshot:
        mgr = JobManager()
        ref = await mgr.start("h", "echo hi", "/w", "false")
        return await mgr.snapshot(ref.id, wait=5.0)

    snap = anyio.run(scenario)
    assert snap.status == "done"
    assert snap.exit_code == 0


def test_start_records_execute_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(_proc: _FakeProc, _call: _FakeCall, _capture: Capture) -> int:
        raise RuntimeError("ssh blew up")

    _patch_ssh(monkeypatch, boom)

    async def scenario() -> JobSnapshot:
        mgr = JobManager()
        ref = await mgr.start("h", "cmd", "/w", "false")
        return await mgr.snapshot(ref.id, wait=5.0)

    snap = anyio.run(scenario)
    assert snap.status == "error"
    assert "ssh blew up" in (snap.error or "")


def test_snapshot_waits_for_running_job(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()

    async def block(_proc: _FakeProc, _call: _FakeCall, _capture: Capture) -> int:
        started.set()
        await asyncio.Event().wait()
        return 0

    _patch_ssh(monkeypatch, block)

    async def scenario() -> JobStatus:
        mgr = JobManager()
        ref = await mgr.start("h", "sleep", "/w", "false")
        await started.wait()
        snap = await mgr.snapshot(ref.id, wait=0.05)  # still running: waits, then times out
        mgr.kill(ref.id)
        await mgr._jobs[ref.id].done.wait()
        return snap.status

    assert anyio.run(scenario) == "running"


def test_kill_cancels_running_job(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()

    async def block(_proc: _FakeProc, _call: _FakeCall, _capture: Capture) -> int:
        started.set()
        await asyncio.Event().wait()
        return 0

    _patch_ssh(monkeypatch, block)

    async def scenario() -> tuple[bool, JobStatus]:
        mgr = JobManager()
        ref = await mgr.start("h", "sleep", "/w", "false")
        await started.wait()
        killed = mgr.kill(ref.id)
        await mgr._jobs[ref.id].done.wait()
        return killed, mgr._jobs[ref.id].ref.status

    killed, status = anyio.run(scenario)
    assert killed is True
    assert status == "killed"


def test_shutdown_cancels_all(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()

    async def block(_proc: _FakeProc, _call: _FakeCall, _capture: Capture) -> int:
        started.set()
        await asyncio.Event().wait()
        return 0

    _patch_ssh(monkeypatch, block)

    async def scenario() -> JobStatus:
        mgr = JobManager()
        ref = await mgr.start("h", "sleep", "/w", "false")
        await started.wait()
        await mgr.shutdown()
        return mgr._jobs[ref.id].ref.status

    assert anyio.run(scenario) == "killed"
