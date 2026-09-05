"""Server assembly: the tool set, their descriptions and parameters — as intended.

fastmcp takes a tool's description from the docstring text before `Args`, and parameter
descriptions from the `Args` section itself; across routers it doesn't treat a name
collision as an error and just takes the first one. All of this is checked here so that a
regression in a docstring or a new router doesn't go unnoticed.
"""

import ast
import re
import tokenize
from pathlib import Path

import anyio
from fastmcp import Client
from mcp_types import Tool

import mcp_openssh_connector
from mcp_openssh_connector import routers
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
    "llms_add_source",
    "llms_remove_source",
    "llms_index",
    "llms_search",
    "llms_fetch",
    "add_host",
    "remove_host",
    "forget_host",
    "copy_id",
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


def test_every_tool_documented_and_annotated() -> None:
    for tool in _tools():
        assert tool.description, tool.name
        assert tool.title, tool.name
        assert tool.annotations is not None, tool.name
        # All four hints are set explicitly: the MCP defaults (destructive=true,
        # open_world=true) are almost always wrong for our tools.
        for hint in ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint"):
            assert getattr(tool.annotations, hint) is not None, f"{tool.name}.{hint}"
        for name, prop in tool.input_schema.get("properties", {}).items():
            assert prop.get("description"), f"{tool.name}.{name}"


def test_run_and_start_defaults() -> None:
    by_name = {tool.name: tool for tool in _tools()}
    for name in ("run", "start"):
        schema = by_name[name].input_schema
        assert set(schema["required"]) == {"host", "command"}
        assert schema["properties"]["cwd"]["default"] == "~"
        assert schema["properties"]["cwd"]["minLength"] == 1
        sudo = schema["properties"]["sudo"]
        assert sudo["default"] == "auto"
        assert sudo["enum"] == ["auto", "true", "false"]


def test_router_layout() -> None:
    # A router only has schemas, handlers, and services; routers don't know about each other.
    root = Path(routers.__file__).parent
    allowed = {"__init__.py", "handlers.py", "schemas.py", "services.py"}
    for package in (p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        assert {f.name for f in package.glob("*.py")} <= allowed, package.name
        for module in package.glob("*.py"):
            # `from ..x` is a sibling router; only `.` (own) and `...core` are allowed.
            assert not re.search(r"^from \.\.(?!\.)", module.read_text(), re.MULTILINE), module


def _sources() -> list[Path]:
    package = Path(mcp_openssh_connector.__file__).parent
    return [*package.rglob("*.py"), *Path(__file__).parent.glob("*.py")]


def test_no_any_or_object_in_code() -> None:
    # `Any` and `object` are banned in code: JSON uses `pydantic.JsonValue`, everything else needs an exact type.
    for path in _sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Name):
                assert node.id not in {"Any", "object"}, f"{path}:{node.lineno}"
            elif isinstance(node, ast.Attribute):
                assert node.attr not in {"Any", "object"}, f"{path}:{node.lineno}"


def test_no_lint_suppressions_in_code() -> None:
    # A rule is either followed, or disabled in pyproject with a reason.
    banned = ("noqa", "type: ignore", "fmt: skip", "fmt: off", "pyright: ignore", "ty: ignore")
    for path in _sources():
        with path.open(encoding="utf-8") as file:
            for token in tokenize.generate_tokens(file.readline):
                if token.type == tokenize.COMMENT:
                    assert not any(word in token.string for word in banned), f"{path}:{token.start[0]}"
