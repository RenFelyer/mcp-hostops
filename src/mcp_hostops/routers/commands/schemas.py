"""Commands router response schema."""

from pydantic import Field

from ...core.schemas import CapturedOutput


class RunResult(CapturedOutput):
    """Response of `run`."""

    exit_code: int | None = Field(description="return code; null — killed by timeout")
    duration: float = Field(description="duration, seconds")
    timed_out: bool
    sudo_used: bool = Field(description="whether the sudo password was primed")
