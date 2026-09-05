"""Hosts router response schemas."""

from pydantic import BaseModel, Field

from ...core.schemas import Availability, Checked, Host


class HostStatus(Host):
    """Host with its last known availability."""

    status: Availability


class ListHostsResult(Checked):
    """Response of `list_hosts`."""

    hosts: list[HostStatus]


class CheckResult(BaseModel):
    """One row of the `check_hosts` response."""

    alias: str
    status: Availability
    detail: str = Field(description="login failure reason for deep, or a note on unknown; empty otherwise")
