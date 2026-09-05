"""Markdown rendering for tools that answer with a document rather than a model.

Router-agnostic: given a title, an optional summary, links grouped by section,
and optional trailing bullets, it produces `llms.txt`-style markdown. Tools whose
payload is a document (a rendered index, a page) return this text; tools whose
payload is data return a pydantic model as usual.
"""

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict


class Link(BaseModel):
    """One entry in a rendered index."""

    model_config = ConfigDict(frozen=True)

    title: str
    url: str
    description: str = ""
    section: str = ""


def markdown_index(
    title: str,
    summary: str,
    links: Sequence[Link],
    *,
    trailing_heading: str = "",
    trailing: Sequence[str] = (),
) -> str:
    """Render `# title`, `> summary`, `## section`, `- [title](url): description`.

    Links keep their given order; each new `section` opens a `##` heading (links
    with an empty section are listed without one). Trailing bullets, when given,
    follow under `trailing_heading`.
    """
    lines = [f"# {title}"]
    if summary:
        lines += ["", f"> {summary}"]
    section = ""
    for link in links:
        if link.section != section:
            section = link.section
            if section:
                lines += ["", f"## {section}"]
        description = f": {link.description}" if link.description else ""
        lines.append(f"- [{link.title}]({link.url}){description}")
    if trailing:
        lines += ["", f"## {trailing_heading}", *(f"- {item}" for item in trailing)]
    return "\n".join(lines) + "\n"
