"""Entry point: server over stdio. uvloop if installed (extra), otherwise asyncio."""

try:
    from uvloop import run as run_loop
except ImportError:
    from asyncio import run as run_loop

from .core.config import logger
from .core.server import mcp


def main() -> None:
    """Run the server over stdio and serve until the transport closes."""
    logger.setup()
    run_loop(mcp.run_async(transport="stdio", show_banner=False))
