"""Схемы ответов роутера хостов."""

from pydantic import BaseModel, Field

from ...core.schemas import Availability, Host


class HostStatus(Host):
    """Хост с последней известной доступностью."""

    status: Availability


class ListHostsResult(BaseModel):
    """Ответ `list_hosts`."""

    checked_ago: float = Field(description="возраст данных о доступности, секунды")
    hosts: list[HostStatus]


class CheckResult(BaseModel):
    """Одна строка ответа `check_hosts`."""

    alias: str
    status: Availability
    detail: str = Field(description="причина отказа или пояснение; пусто при мелкой пробе")
