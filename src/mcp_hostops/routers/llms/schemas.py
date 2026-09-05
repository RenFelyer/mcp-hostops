"""Response schemas for the llms.txt router."""

from typing import Literal

from pydantic import BaseModel, Field

from ...core.schemas import Checked, KnownSource

# Where to search: in the table of contents or in the whole documentation as one file.
SearchScope = Literal["index", "full"]

# Outcome of a source check: the index responds with text; doesn't respond; HTML stub.
SourceState = Literal["ok", "unavailable", "stub"]


class SourceVerdict(BaseModel):
    """Outcome of a source check; stored in this shape in the outcomes file."""

    state: SourceState
    detail: str = Field(description="response code or reason; empty when ok")


class SourceStatus(KnownSource, SourceVerdict):
    """A source with the outcome of its last check."""


class SourcesResult(Checked):
    """Response of `llms_list_sources`: the checked registry and known variant names."""

    sources: list[SourceStatus]
    variants: list[str] = Field(description="file names that llms_index looks for on a domain")


class IndexEntry(BaseModel):
    """A link from the `llms.txt` table of contents."""

    title: str
    url: str = Field(description="absolute address; pass to llms_fetch as-is")
    description: str
    section: str = Field(description="index section heading; empty outside sections")


class Variant(BaseModel):
    """A file found next to the index."""

    name: str
    size: int | None = Field(description="bytes per Content-Length; null when the server didn't report one")


class LlmsIndex(BaseModel):
    """Response of `llms_index`: the parsed table of contents plus what else lives on the domain."""

    url: str = Field(description="address of the index itself")
    title: str
    summary: str
    entries: list[IndexEntry]
    full_url: str = Field(description="address of `llms-full.txt`, if the index names it")
    variants: list[Variant] = Field(default_factory=list, description="files found next to the index")


class SearchHit(BaseModel):
    """One `llms_search` match."""

    domain: str
    title: str = Field(description="index entry title or full-file section heading")
    url: str
    text: str = Field(description="entry description or section excerpt")
    truncated: bool = Field(description="excerpt trimmed to the cap")


class SearchResult(BaseModel):
    """Response of `llms_search`."""

    query: str
    scope: SearchScope
    searched: list[str] = Field(description="domains that were searched")
    skipped: list[str] = Field(description="domains skipped due to an error, with the reason")
    total: int = Field(description="total matches; hits holds no more than the cap")
    hits: list[SearchHit]


class Page(BaseModel):
    """Response of `llms_fetch`: a page chunk starting at offset."""

    url: str
    content_type: str
    length: int = Field(description="total characters on the page")
    offset: int
    text: str
    next_offset: int | None = Field(description="where to read from next; null means the end")
