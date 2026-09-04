"""Схемы и вокабуляр состояний роутера фоновых задач."""

from typing import Literal

from pydantic import BaseModel, Field

from ...core.schemas import CapturedOutput

JobStatus = Literal["running", "done", "killed", "error"]
RUNNING: JobStatus = "running"
DONE: JobStatus = "done"
KILLED: JobStatus = "killed"
ERROR: JobStatus = "error"


class JobRef(BaseModel):
    """Задача без вывода: ответ `start` и строка ответа `jobs`."""

    id: str
    host: str
    command: str
    cwd: str
    status: JobStatus = RUNNING
    exit_code: int | None = None
    error: str = Field(default="", description="причина при status=error; иначе пусто")


class JobSnapshot(JobRef, CapturedOutput):
    """Ответ `job`: состояние плюс прирост вывода."""

    stdout: str = Field(description="прирост вывода с прошлого чтения")
