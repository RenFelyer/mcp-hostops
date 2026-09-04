"""Общие для всех роутеров типы и вокабуляры.

Здесь живут значения, которые встречаются больше чем в одном роутере или общи для
роутера и инфраструктуры: хост, статус доступности, режим sudo, захваченный
вывод команды, источник `llms.txt` (его встроенный реестр — в константах) и
пресеты подсказок клиенту для инструментов.
"""

from collections.abc import Mapping, Sequence
from typing import Annotated, Literal

from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

# Строковый параметр инструмента, для которого пустая строка — ошибка вызова.
NonEmptyStr = Annotated[str, Field(min_length=1)]

# Значение из JSON-файла состояния: то, что даёт `json.loads`. Содержимое
# сужается pydantic-моделью или `isinstance` у того, кто его читает.
type Json = Mapping[str, Json] | Sequence[Json] | str | int | float | bool | None

# Доступность хоста: значение статуса в ответах роутера hosts и в пробах.
Availability = Literal["available", "unavailable", "unknown"]

# Режим sudo в run/start.
SudoMode = Literal["auto", "true", "false"]

# Подсказки клиенту, общие для нескольких инструментов; все четыре выставлены
# явно, потому что дефолты MCP (destructive и open_world — true) почти всегда
# неверны. Инструмент с уникальным набором описывает его у себя.
READS_REMOTE = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
READS_LOCAL = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
# Произвольная команда на хосте: может менять и удалять что угодно.
RUNS_REMOTE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)


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


class Checked(BaseModel):
    """Ответ, чьи данные могли быть получены раньше этого вызова."""

    checked_ago: float = Field(description="возраст данных, секунды; 0 — получены этим вызовом")


class KnownSource(BaseModel):
    """Источник `llms.txt`: где индекс и что он покрывает."""

    model_config = ConfigDict(frozen=True)

    domain: str = Field(description="как называть источник в llms_index/llms_search")
    index: str = Field(description="адрес `llms.txt`")
    covers: str
    full: str = Field(default="", description="адрес `llms-full.txt`, если известен")
    full_size: int | None = Field(default=None, description="размер full-файла в байтах, чтобы не читать целиком")
    default: bool = Field(default=False, description="встроенный; удалить нельзя")


def as_availability(value: Json) -> Availability:
    """Привести значение из кэша или вывода пробы к статусу; чужое — «unknown»."""
    if value == "available":
        return "available"
    if value == "unavailable":
        return "unavailable"
    return "unknown"
