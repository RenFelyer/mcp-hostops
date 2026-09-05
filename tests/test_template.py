"""Shared markdown template: title/summary/sections, links, trailing bullets."""

from mcp_hostops.core.schemas import Link
from mcp_hostops.core.template import markdown_index


def test_markdown_index_full_shape() -> None:
    md = markdown_index(
        "Ruff",
        "Fast linter.",
        [
            Link(title="Preview", url="https://x/p", description="", section="Concepts"),
            Link(title="Config", url="https://x/c", description="how to configure", section="Configuration"),
        ],
        trailing_heading="Other files on the domain",
        trailing=["llms-full.txt — 100 bytes"],
    )
    assert md == (
        "# Ruff\n"
        "\n> Fast linter.\n"
        "\n## Concepts\n"
        "- [Preview](https://x/p)\n"
        "\n## Configuration\n"
        "- [Config](https://x/c): how to configure\n"
        "\n## Other files on the domain\n"
        "- llms-full.txt — 100 bytes\n"
    )


def test_markdown_index_minimal() -> None:
    # No summary, no sections, no trailing: just the title and a flat bullet.
    assert markdown_index("T", "", [Link(title="a", url="u")]) == "# T\n- [a](u)\n"
