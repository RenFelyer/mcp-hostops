"""Сервис роутера фоновых задач: реестр задач и их выполнение.

Задача — живой ssh-процесс внутри сервера; вывод копится в буфере и отдаётся по
запросу. Каждая задача — отдельный asyncio-таск (nursery через lifespan FastMCP
держать нельзя: он закрывается в другой задаче). Задачи умирают вместе с
сервером, то есть в пределах сессии. Пароль sudo уходит в stdin один раз в
начале, потому что процесс не перезапускается.

Менеджер один на сервер (`manager`); lifespan роутера снимает его задачи при
остановке.
"""

import asyncio
from functools import partial

import anyio
from pydantic import BaseModel, ConfigDict, Field

from ...core.config import get_settings
from ...core.schemas import SudoMode
from ...core.utils.hosts import require_host
from ...core.utils.ssh import Capture, Invocation, prepare, pump
from .schemas import DONE, ERROR, KILLED, RUNNING, JobRef, JobSnapshot


class Job(BaseModel):
    """Живая фоновая задача: публичное состояние плюс рантайм.

    Не схема ответа — держит буферы, asyncio-таск и событие завершения, поэтому
    `arbitrary_types_allowed`.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ref: JobRef
    capture: Capture
    task: asyncio.Task[None] | None = None
    done: anyio.Event = Field(default_factory=anyio.Event)


class JobManager:
    """Реестр фоновых задач: каждая — самостоятельный asyncio-таск."""

    def __init__(self) -> None:
        self._s = get_settings()
        self._jobs: dict[str, Job] = {}
        self._counter = 0

    async def start(self, host: str, command: str, cwd: str, sudo_mode: SudoMode) -> JobRef:
        """Запустить команду в фоне и сразу вернуть задачу с присвоенным id.

        Raises:
            UserError: алиаса нет в конфиге или пароль sudo недоступен.
        """
        call = prepare(await require_host(host), command, cwd, sudo_mode, None, self._s)
        self._counter += 1
        job = Job(
            ref=JobRef(id=str(self._counter), host=host, command=command, cwd=cwd),
            capture=Capture(self._s.output_limit, call.password),
        )
        self._jobs[job.ref.id] = job
        job.task = asyncio.create_task(self._run(job, call))
        job.task.add_done_callback(partial(self._finish, job))
        return job.ref

    @staticmethod
    async def _run(job: Job, call: Invocation) -> None:
        async with await anyio.open_process(call.argv) as proc:
            try:
                await pump(proc, call.stdin, job.capture)
                job.ref.exit_code = await proc.wait()
            finally:
                # Отмена таска — asyncio-шная, не anyio: закрытие процесса само
                # его не убьёт, а дождётся, — убиваем явно.
                if proc.returncode is None:
                    proc.kill()

    @staticmethod
    def _finish(job: Job, task: asyncio.Task[None]) -> None:
        """Перенести итог таска в задачу.

        Callback, а не код в `_run`: таск, отменённый до первого шага, тело
        `_run` не исполняет вовсе.
        """
        if task.cancelled():
            job.ref.status = KILLED
        elif (err := task.exception()) is not None:
            job.ref.status = ERROR
            job.ref.error = str(err)
        else:
            job.ref.status = DONE
        job.done.set()

    async def snapshot(self, job_id: str, wait: float) -> JobSnapshot | None:
        """Слепок задачи с приростом вывода с прошлого чтения.

        Args:
            job_id: Идентификатор из `start`.
            wait: Секунды ожидания завершения; 0 — не ждать. Сверху ограничено
                `max_wait`.

        Returns:
            Слепок или None, если такой задачи нет.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if wait > 0 and job.ref.status == RUNNING:
            with anyio.move_on_after(min(wait, self._s.max_wait)):
                await job.done.wait()
        return JobSnapshot(**job.ref.model_dump(), **job.capture.drained().model_dump())

    def kill(self, job_id: str) -> bool:
        """Снять задачу.

        Returns:
            True — была запущена и получила отмену; False — нет такой или уже
            завершилась.
        """
        job = self._jobs.get(job_id)
        if job is None or job.task is None or job.ref.status != RUNNING:
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
