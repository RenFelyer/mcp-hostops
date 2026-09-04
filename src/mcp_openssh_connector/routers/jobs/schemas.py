"""Схемы и вокабуляр состояний роутера фоновых задач."""

from typing import Literal

from pydantic import BaseModel, Field

from ...core.schemas import CapturedOutput

JobStatus = Literal["running", "done", "killed", "error"]


class JobRef(BaseModel):
    """Задача без вывода: ответ `start` и строка ответа `jobs`."""

    id: str
    host: str
    command: str
    cwd: str
    status: JobStatus = "running"
    exit_code: int | None = Field(default=None, description="код возврата; null — ещё идёт или снята")
    error: str = Field(default="", description="причина при status=error; иначе пусто")


class JobSnapshot(JobRef, CapturedOutput):
    """Ответ `job`: состояние плюс прирост stdout и stderr с прошлого чтения."""
