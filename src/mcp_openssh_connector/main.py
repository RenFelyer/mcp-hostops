"""Точка входа: сервер по stdio. uvloop — если установлен (extra), иначе asyncio."""

try:
    from uvloop import run as run_loop
except ImportError:
    from asyncio import run as run_loop

from .core.server import mcp


def main() -> None:
    """Запустить сервер по stdio и работать до закрытия транспорта."""
    run_loop(mcp.run_async(transport="stdio", show_banner=False))
