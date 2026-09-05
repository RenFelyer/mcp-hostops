"""Background jobs router service: job registry and their execution.

A job is a live ssh process inside the server; output accumulates in a buffer
and is handed out on request. Each job is a separate asyncio task (a nursery
can't be held through FastMCP's lifespan: it closes in a different task). Jobs
die together with the server, i.e. within the session. The sudo password goes
to stdin once at the start, because the process is never restarted.

There is one manager per server (`manager`); the router's lifespan cancels its
jobs on shutdown. No more than `job_history` finished jobs are remembered: the
older ones are forgotten together with their unread output.
"""

import asyncio
from functools import partial

import anyio

from ...core.config.environment import Settings, get_settings
from ...core.errors import UserError
from ...core.schemas import SudoMode
from ...core.utils.hosts import require_host
from ...core.utils.ssh import Capture, Invocation, execute, prepare, spawn
from .schemas import JobRef, JobSnapshot


class Job:
    """A live background job: public state plus buffers, task and completion event."""

    def __init__(self, ref: JobRef, capture: Capture) -> None:
        self.ref = ref
        self.capture = capture
        self.task: asyncio.Task[None] | None = None
        self.done = anyio.Event()


class JobManager:
    """Registry of background jobs: each one an independent asyncio task."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._counter = 0

    @property
    def _s(self) -> Settings:
        return get_settings()

    async def start(self, host: str, command: str, cwd: str, sudo_mode: SudoMode) -> JobRef:
        """Start a command in the background and return the job with its assigned id right away.

        Raises:
            UserError: the alias is not in the config or the sudo password is unavailable.
        """
        call = prepare(await require_host(host), command, cwd, sudo_mode, None, self._s)
        self._forget_old()
        self._counter += 1
        job = Job(
            ref=JobRef(id=str(self._counter), host=host, command=command, cwd=cwd),
            capture=Capture(self._s.output_limit, call.password),
        )
        self._jobs[job.ref.id] = job
        job.task = asyncio.create_task(self._run(job, call))
        job.task.add_done_callback(partial(self._finish, job))
        return job.ref

    def _forget_old(self) -> None:
        """Keep no more than `job_history` finished jobs, the most recent ones."""
        finished = [job_id for job_id, job in self._jobs.items() if job.ref.status != "running"]
        for job_id in finished[: max(0, len(finished) - self._s.job_history)]:
            del self._jobs[job_id]

    @staticmethod
    async def _run(job: Job, call: Invocation) -> None:
        async with spawn(call) as proc:
            job.ref.exit_code = await execute(proc, call, job.capture)

    @staticmethod
    def _finish(job: Job, task: asyncio.Task[None]) -> None:
        """Carry the task's outcome over to the job.

        A callback rather than code in `_run`: a task cancelled before its
        first step never executes the body of `_run` at all.
        """
        if task.cancelled():
            job.ref.status = "killed"
        elif (err := task.exception()) is not None:
            job.ref.status = "error"
            job.ref.error = str(err)
        else:
            job.ref.status = "done"
        job.done.set()

    def _get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise UserError(f"job {job_id!r} not found")
        return job

    async def snapshot(self, job_id: str, wait: float) -> JobSnapshot:
        """A snapshot of the job with the incremental output since the last read.

        Args:
            job_id: Identifier from `start`.
            wait: Seconds to wait for completion; 0 — don't wait. Capped at
                `max_wait`.

        Raises:
            UserError: no such job.
        """
        job = self._get(job_id)
        if wait > 0 and job.ref.status == "running":
            with anyio.move_on_after(min(wait, self._s.max_wait)):
                await job.done.wait()
        output = job.capture.drained(final=job.ref.status != "running")
        return JobSnapshot(**job.ref.model_dump(), **output.model_dump())

    def kill(self, job_id: str) -> bool:
        """Kill a job.

        Returns:
            True — it was running and got cancelled; False — no such job, or
            it already finished.
        """
        job = self._jobs.get(job_id)
        if job is None or job.task is None or job.ref.status != "running":
            return False
        job.task.cancel()
        return True

    def listing(self) -> list[JobRef]:
        """All jobs without output: statuses only (for an overview)."""
        return [job.ref for job in self._jobs.values()]

    async def shutdown(self) -> None:
        """Cancel all live jobs and wait for them to finish — on server shutdown."""
        tasks = [job.task for job in self._jobs.values() if job.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


manager = JobManager()
