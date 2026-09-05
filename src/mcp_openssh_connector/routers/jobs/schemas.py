"""Background jobs router schemas and status vocabulary."""

from typing import Literal

from pydantic import BaseModel, Field

from ...core.schemas import CapturedOutput

JobStatus = Literal["running", "done", "killed", "error"]


class JobRef(BaseModel):
    """Job without output: response of `start` and a row of `jobs`."""

    id: str
    host: str
    command: str
    cwd: str
    status: JobStatus = "running"
    exit_code: int | None = Field(default=None, description="return code; null — still running or killed")
    error: str = Field(default="", description="reason when status=error; empty otherwise")


class JobSnapshot(JobRef, CapturedOutput):
    """Response of `job`: status plus incremental stdout and stderr since the last read."""
