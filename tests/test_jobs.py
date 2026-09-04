"""Регресс: snapshot отдаёт прирост вывода и не теряет его при свопе буфера."""

import anyio

from mcp_openssh_connector.core.utils.ssh import Capture, Output
from mcp_openssh_connector.routers.jobs.schemas import JobRef
from mcp_openssh_connector.routers.jobs.services import Job, JobManager


def test_snapshot_returns_delta_and_swaps_buffer() -> None:
    manager = JobManager()
    job = Job(
        ref=JobRef(id="1", host="h", command="c", cwd="/w", status="done"),
        capture=Capture(1000, None),
    )
    job.capture.stdout.feed(b"first")
    manager._jobs["1"] = job

    async def scenario() -> tuple[str, str]:
        snap1 = await manager.snapshot("1", 0.0)
        assert snap1 is not None
        job.capture.stdout.feed(b"second")  # новый вывод после чтения
        snap2 = await manager.snapshot("1", 0.0)
        assert snap2 is not None
        return snap1.stdout, snap2.stdout

    first, second = anyio.run(scenario)
    assert first == "first"
    assert second == "second"  # только прирост, «first» не повторился и не потерялся


def test_output_limit_marks_truncation() -> None:
    out = Output(5)
    out.feed(b"abc")
    out.feed(b"defg")
    assert out.take() == b"abcde"
    assert out.truncated is True
    out.feed(b"xyz")  # после take лимит считается заново
    assert out.take() == b"xyz"


def test_capture_masks_password() -> None:
    capture = Capture(100, "s3cret")
    capture.stdout.feed(b"s3cret\r\nout")
    got = capture.drained()
    assert got.stdout == "***\nout"
    assert got.stdout_truncated is False
