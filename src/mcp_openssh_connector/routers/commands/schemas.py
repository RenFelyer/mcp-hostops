"""Схема ответа роутера команд."""

from pydantic import Field

from ...core.schemas import CapturedOutput


class RunResult(CapturedOutput):
    """Ответ `run`."""

    exit_code: int | None = Field(description="код возврата; null — убито по таймауту")
    duration: float = Field(description="длительность, секунды")
    timed_out: bool
    sudo_used: bool = Field(description="был ли прайминг пароля sudo")
