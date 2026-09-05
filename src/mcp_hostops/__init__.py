"""MCP server for working with remote hosts over OpenSSH."""


def main() -> None:
    """Run the server over stdio and serve until the transport closes.

    Imports stay inside the function so importing the package stays cheap and
    side-effect-free (importing the server builds and mounts the routers).
    uvloop if installed (extra), otherwise asyncio.
    """
    try:
        from uvloop import run as run_loop
    except ImportError:
        from asyncio import run as run_loop

    from .core.config import logger
    from .core.server import mcp

    logger.setup()
    run_loop(mcp.run_async(transport="stdio", show_banner=False))
