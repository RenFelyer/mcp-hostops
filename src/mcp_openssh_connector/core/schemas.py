"""Общие для всех роутеров типы и вокабуляры.

Здесь живут значения, которые встречаются больше чем в одном роутере или общи для
роутера и инфраструктуры: хост, статус доступности, режим sudo и захваченный
вывод команды.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# Строковый параметр инструмента, для которого пустая строка — ошибка вызова.
NonEmptyStr = Annotated[str, Field(min_length=1)]


class Host(BaseModel):
    """Хост из ~/.ssh/config с параметрами глазами `ssh -G`."""

    model_config = ConfigDict(frozen=True)

    alias: str
    hostname: str
    user: str
    port: int
    proxyjump: str = Field(description="алиас jump-хоста; пусто, если прямой")


class CapturedOutput(BaseModel):
    """Вывод команды с отметками, что потолок буфера был превышен."""

    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


# Доступность хоста: значение статуса в ответах роутера hosts и в пробах.
Availability = Literal["available", "unavailable", "unknown"]
AVAILABLE: Availability = "available"
UNAVAILABLE: Availability = "unavailable"
UNKNOWN: Availability = "unknown"

# Режим sudo в run/start.
SudoMode = Literal["auto", "true", "false"]


def as_availability(value: str) -> Availability:
    """Привести произвольную строку к статусу доступности; чужое — «unknown»."""
    if value == AVAILABLE:
        return AVAILABLE
    if value == UNAVAILABLE:
        return UNAVAILABLE
    return UNKNOWN
