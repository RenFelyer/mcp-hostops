"""Схемы ответов роутера хостов."""

from pydantic import BaseModel, Field

from ...core.schemas import Availability, Checked, Host


class HostStatus(Host):
    """Хост с последней известной доступностью."""

    status: Availability


class ListHostsResult(Checked):
    """Ответ `list_hosts`."""

    hosts: list[HostStatus]


class CheckResult(BaseModel):
    """Одна строка ответа `check_hosts`."""

    alias: str
    status: Availability
    detail: str = Field(description="причина отказа входа при deep или пояснение к unknown; иначе пусто")
