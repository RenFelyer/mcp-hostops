"""Сборка сервера: набор инструментов, их описания и параметры — как задумано.

fastmcp берёт описание инструмента из текста докстринга до `Args`, а описания
параметров — из самой секции `Args`; между роутерами совпадение имён он не
считает ошибкой и берёт первое. Всё это проверяется здесь, чтобы регресс в
докстринге или новый роутер не прошли незамеченными.
"""

import anyio
from fastmcp import Client
from mcp_types import Tool

from mcp_openssh_connector.core.server import mcp

EXPECTED = {
    "list_hosts",
    "check_hosts",
    "host_info",
    "run",
    "start",
    "job",
    "kill",
    "jobs",
    "llms_sources",
    "llms_index",
    "llms_search",
    "llms_fetch",
}


def _tools() -> list[Tool]:
    async def scenario() -> list[Tool]:
        async with Client(mcp) as client:
            tools: list[Tool] = await client.list_tools()
            return tools

    return anyio.run(scenario)


def test_tool_names_unique_and_expected() -> None:
    names = [tool.name for tool in _tools()]
    assert len(names) == len(set(names))
    assert set(names) == EXPECTED


def test_every_tool_documented() -> None:
    for tool in _tools():
        assert tool.description, tool.name
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is not None, tool.name
        assert tool.annotations.open_world_hint is not None, tool.name
        for name, prop in tool.input_schema.get("properties", {}).items():
            assert prop.get("description"), f"{tool.name}.{name}"


def test_run_and_start_defaults() -> None:
    by_name = {tool.name: tool for tool in _tools()}
    for name in ("run", "start"):
        schema = by_name[name].input_schema
        assert set(schema["required"]) == {"host", "command"}
        assert schema["properties"]["cwd"]["default"] == "~"
        assert schema["properties"]["cwd"]["minLength"] == 1
