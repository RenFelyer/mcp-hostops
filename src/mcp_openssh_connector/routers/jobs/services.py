"""Сервис роутера фоновых задач: реестр задач и их выполнение.

Задача — живой ssh-процесс внутри сервера; вывод копится в буфере и отдаётся по
запросу. Каждая задача — отдельный asyncio-таск (nursery через lifespan FastMCP
держать нельзя: он закрывается в другой задаче). Задачи умирают вместе с
сервером, то есть в пределах сессии. Пароль sudo уходит в stdin один раз в
начале, потому что процесс не перезапускается.

Менеджер один на сервер (`manager`); lifespan роутера снимает его задачи при
остановке. Завершённых задач помнится не больше `job_history`: старые
забываются вместе с непрочитанным выводом.
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
    """Живая фоновая задача: публичное состояние плюс буферы, таск и событие конца."""

    def __init__(self, ref: JobRef, capture: Capture) -> None:
        self.ref = ref
        self.capture = capture
        self.task: asyncio.Task[None] | None = None
        self.done = anyio.Event()


class JobManager:
    """Реестр фоновых задач: каждая — самостоятельный asyncio-таск."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._counter = 0

    @property
    def _s(self) -> Settings:
        return get_settings()

    async def start(self, host: str, command: str, cwd: str, sudo_mode: SudoMode) -> JobRef:
        """Запустить команду в фоне и сразу вернуть задачу с присвоенным id.

        Raises:
            UserError: алиаса нет в конфиге или пароль sudo недоступен.
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
        """Оставить не больше `job_history` завершённых задач, самых свежих."""
        finished = [job_id for job_id, job in self._jobs.items() if job.ref.status != "running"]
        for job_id in finished[: max(0, len(finished) - self._s.job_history)]:
            del self._jobs[job_id]

    @staticmethod
    async def _run(job: Job, call: Invocation) -> None:
        async with spawn(call) as proc:
            job.ref.exit_code = await execute(proc, call, job.capture)

    @staticmethod
    def _finish(job: Job, task: asyncio.Task[None]) -> None:
        """Перенести итог таска в задачу.

        Callback, а не код в `_run`: таск, отменённый до первого шага, тело
        `_run` не исполняет вовсе.
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
            raise UserError(f"задача {job_id!r} не найдена")
        return job

    async def snapshot(self, job_id: str, wait: float) -> JobSnapshot:
        """Слепок задачи с приростом вывода с прошлого чтения.

        Args:
            job_id: Идентификатор из `start`.
            wait: Секунды ожидания завершения; 0 — не ждать. Сверху ограничено
                `max_wait`.

        Raises:
            UserError: такой задачи нет.
        """
        job = self._get(job_id)
        if wait > 0 and job.ref.status == "running":
            with anyio.move_on_after(min(wait, self._s.max_wait)):
                await job.done.wait()
        output = job.capture.drained(final=job.ref.status != "running")
        return JobSnapshot(**job.ref.model_dump(), **output.model_dump())

    def kill(self, job_id: str) -> bool:
        """Снять задачу.

        Returns:
            True — была запущена и получила отмену; False — нет такой или уже
            завершилась.
        """
        job = self._jobs.get(job_id)
        if job is None or job.task is None or job.ref.status != "running":
            return False
        job.task.cancel()
        return True

    def listing(self) -> list[JobRef]:
        """Все задачи без вывода: только состояния (для обзора)."""
        return [job.ref for job in self._jobs.values()]

    async def shutdown(self) -> None:
        """Снять все живые задачи и дождаться их завершения — при остановке сервера."""
        tasks = [job.task for job in self._jobs.values() if job.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


manager = JobManager()
