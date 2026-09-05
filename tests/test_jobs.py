"""Job registry: incremental output, buffer cap, masking, history."""

import anyio
import pytest

from mcp_openssh_connector.core.config.environment import get_settings
from mcp_openssh_connector.core.errors import UserError
from mcp_openssh_connector.core.utils.ssh import Capture, Output
from mcp_openssh_connector.routers.jobs.schemas import JobRef, JobStatus
from mcp_openssh_connector.routers.jobs.services import Job, JobManager


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
