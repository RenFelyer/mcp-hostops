"""Service layer for the llms.txt router: source registry, cached downloads, parsing, search.

The registry is the built-in `LLMS_DEFAULT_SOURCES` list (vetted, can't be
removed) plus a user JSON file, `llms_sources_file`, that survives a server
restart. The `default` flag is neither written to nor read from that file, so
that hand-editing it can't make a source un-removable; domains that collide
with a built-in one are skipped when reading the file.

Networking is async (`httpx2.AsyncClient`). One tool call is one `Session`: an
HTTP client with a shared connection pool, so that probing variants and
searching across many sources don't open a new TLS connection per request.
Everything downloaded lands in an on-disk cache with a TTL: the index and
pages are re-read rarely, and `llms-full.txt` can be tens of megabytes and is
never returned in full — only matched sections. Disk I/O and parsing of large
files run in a thread, so the event loop isn't blocked.

The server only fetches https on public names that resolve to public
addresses: the client picks the address, and without this restriction the
tool would be a window into the internal network. The rule is applied by a
hook to every request, including every redirect hop, before it's sent. The
address isn't pinned: a name whose DNS answer changes between the check and
the connection won't be caught by it.

Some sites return 200 with an HTML shell for any path, while their real files
come back as text/plain. So something only counts as a stub when the resource
itself looks like HTML *and* the domain also returns 200 for a junk path next
to it; a text response is a real document no matter what the domain returns
for junk.
"""

import contextlib
import hashlib
import ipaddress
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from itertools import pairwise
from pathlib import Path
from time import time
from types import TracebackType
from typing import Self
from urllib.parse import urljoin, urlsplit

import anyio
import anyio.to_thread
import httpx2
from pydantic import BaseModel, TypeAdapter, ValidationError

from ...core import store
from ...core.config.constants import (
    LLMS_DEFAULT_SOURCES,
    LLMS_FULL_NAME,
    LLMS_INDEX_NAME,
    LLMS_JUNK_NAME,
    LLMS_REDIRECTS,
    LLMS_VARIANTS,
)
from ...core.config.environment import Settings, get_settings
from ...core.errors import UserError
from ...core.schemas import KnownSource
from ...core.template import Link, markdown_index
from ...core.utils.parallel import gather
from .schemas import (
    IndexEntry,
    LlmsIndex,
    Page,
    SearchHit,
    SearchResult,
    SearchScope,
    SourcesResult,
    SourceState,
    SourceStatus,
    SourceVerdict,
    Variant,
)

log = logging.getLogger(__name__)

_LINK = re.compile(r"\[(?P<title>[^\]]*)\]\((?P<url>[^)\s]+)\)\s*:?\s*(?P<desc>.*)")
_HEADING = re.compile(r"#{1,3} ")
_HOSTNAME = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))*$")
_VERDICTS = TypeAdapter(dict[str, SourceVerdict])  # contents of the outcomes file, keyed by domain
_SOURCES = TypeAdapter(list[KnownSource])  # contents of the user sources file


class Fetched(BaseModel):
    """A downloaded resource: status, content type and length, body (empty for HEAD)."""

    status: int
    content_type: str
    content_length: int | None
    body: bytes

    @property
    def ok(self) -> bool:
        """Response status is not an error (below 400)."""
        return self.status < HTTPStatus.BAD_REQUEST

    @property
    def cacheable(self) -> bool:
        """Response is worth remembering: successes and stable failures, but not server errors or 429."""
        return self.status < HTTPStatus.INTERNAL_SERVER_ERROR and self.status != HTTPStatus.TOO_MANY_REQUESTS

    @property
    def text(self) -> str:
        """Body as text; bytes outside UTF-8 are replaced rather than failing the call."""
        return self.body.decode("utf-8", "replace")

    @property
    def is_html(self) -> bool:
        """Looks like HTML by content type or by the start of the body."""
        head = self.body.lstrip()[:15].lower()
        return self.content_type.lower().startswith("text/html") or head.startswith((b"<!doctype", b"<html"))


def make_client(s: Settings, guard: Callable[[httpx2.Request], Awaitable[None]]) -> httpx2.AsyncClient:
    """HTTP client for one tool call; `guard` is called before every request.

    Tests replace this function to swap in a transport, but must keep the
    hook: without it, address verification doesn't run.
    """
    return httpx2.AsyncClient(
        timeout=s.llms_timeout,
        follow_redirects=True,
        max_redirects=LLMS_REDIRECTS,
        headers={"User-Agent": "mcp-hostops (llms.txt reader)"},
        event_hooks={"request": [guard]},
    )


def check_url(url: str) -> str:
    """Address the server is willing to fetch: https and a public hostname.

    Returns:
        The hostname in lowercase — to check what it resolves to.

    Raises:
        UserError: not https, hostname isn't a domain name, localhost or a
            non-public IP.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https":
        raise UserError(f"{url}: server only fetches over https")
    with contextlib.suppress(ValueError):
        if not ipaddress.ip_address(host).is_global:
            raise UserError(f"{url}: non-public address")
    if not _HOSTNAME.match(host) or host == "localhost" or host.endswith(".localhost"):
        raise UserError(f"{url}: address must resolve to a public domain")
    return host


async def resolve_public(host: str) -> None:
    """The name must resolve, and only to public addresses.

    A name like `10-0-0-1.nip.io` or an internal name from a search domain
    passes the shape check but leads into the network; caught here. An IP
    literal is already checked in `check_url`.

    Raises:
        UserError: name doesn't resolve, or at least one of its addresses is
            non-public.
    """
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(host)
        return
    try:
        found = await anyio.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as err:
        raise UserError(f"{host}: name does not resolve ({err})") from err
    for *_, sockaddr in found:
        try:
            public = ipaddress.ip_address(sockaddr[0]).is_global
        except ValueError:
            public = False
        if not public:
            raise UserError(f"{host}: name resolves to a non-public address")


def custom_sources(s: Settings) -> list[KnownSource]:
    """User sources from the file; a missing or corrupt file yields an empty list."""
    data = store.load(s.llms_sources_file) or {}
    builtin = {known.domain for known in LLMS_DEFAULT_SOURCES}
    try:
        items = _SOURCES.validate_python(data.get("sources", []))
    except ValidationError:
        return []
    return [item.model_copy(update={"default": False}) for item in items if item.domain not in builtin]


def _save_sources(items: list[KnownSource], s: Settings) -> None:
    store.save(s.llms_sources_file, {"sources": [i.model_dump(exclude={"default"}) for i in items]})


def all_sources(s: Settings) -> list[KnownSource]:
    """Built-in sources, then user ones; domains aren't duplicated."""
    return [*LLMS_DEFAULT_SOURCES, *custom_sources(s)]


def find_source(domain: str, s: Settings) -> KnownSource | None:
    """Source by domain or index address."""
    return next((known for known in all_sources(s) if domain in (known.domain, known.index)), None)


def _forget_status(s: Settings) -> None:
    """Reset check outcomes: the registry's composition has changed."""
    with contextlib.suppress(OSError):
        s.llms_status_file.unlink()


def remove_source(domain: str, s: Settings) -> KnownSource:
    """Remove a user source; a built-in one can't be removed.

    Raises:
        UserError: the source is built-in or doesn't exist.
        OSError: the sources file couldn't be written.
    """
    found = find_source(domain, s)
    if found is None:
        raise UserError(f"no such source {domain!r}")
    if found.default:
        raise UserError(f"source {domain!r} is built-in and can't be removed")
    _save_sources([i for i in custom_sources(s) if i.domain != found.domain], s)
    _forget_status(s)
    return found


def index_url(source: str, s: Settings) -> str:
    """Index address: from the registry by domain, otherwise from whatever the caller named.

    The caller may name a domain (`docs.astral.sh/uv`), a domain with a path,
    or a full file address.
    """
    if (known := find_source(source, s)) is not None:
        return known.index
    url = source if "://" in source else f"https://{source}"
    if not url.endswith(".txt"):
        url = url.rstrip("/") + "/" + LLMS_INDEX_NAME
    return url


def domain_of(url: str, s: Settings) -> str:
    """Source name for responses: registry domain, or the address's host."""
    if (known := find_source(url, s)) is not None:
        return known.domain
    return urlsplit(url).hostname or url


def _cache_paths(method: str, url: str, s: Settings) -> tuple[Path, Path]:
    host = urlsplit(url).hostname or "unknown"
    key = hashlib.sha256(f"{method} {url}".encode()).hexdigest()[:24]
    base = s.llms_cache_dir / host / key
    return base.with_suffix(".body"), base.with_suffix(".meta")


def _cached(method: str, url: str, s: Settings) -> Fetched | None:
    """Read from the cache if an entry exists and isn't older than the TTL; otherwise None."""
    body_path, meta_path = _cache_paths(method, url, s)
    try:
        if time() - meta_path.stat().st_mtime > s.llms_cache_ttl:
            return None
        return Fetched.model_validate({**(store.load(meta_path) or {}), "body": body_path.read_bytes()})
    except (OSError, ValidationError):
        return None


def _store(method: str, url: str, fetched: Fetched, s: Settings) -> None:
    """Write to the cache; body before meta, so fresh meta never points at a stale body."""
    body_path, meta_path = _cache_paths(method, url, s)
    with contextlib.suppress(OSError):
        store.write_bytes(body_path, fetched.body)
        store.save(meta_path, fetched.model_dump(exclude={"body"}))


def parse_index(text: str, base_url: str) -> LlmsIndex:
    """Parse an `llms.txt`: title, summary, links grouped by section.

    Format: `# Title`, `> summary`, `## Section`, lines `- [name](url):
    description`. Relative addresses are resolved against `base_url`. File
    variants aren't checked here — that's network I/O, see `Session.variants`.
    """
    title = summary = section = full_url = ""
    entries: list[IndexEntry] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("# ") and not title:
            title = line[2:].strip()
        elif line.startswith("> ") and not summary:
            summary = line[2:].strip()
        elif line.startswith("## "):
            section = line[3:].strip()
        elif line.startswith(("-", "*")) and (m := _LINK.search(line)):
            url = urljoin(base_url, m["url"])
            if url.endswith(LLMS_FULL_NAME):
                full_url = url
            entries.append(
                IndexEntry(title=m["title"].strip(), url=url, description=m["desc"].strip(), section=section)
            )
    return LlmsIndex(url=base_url, title=title, summary=summary, entries=entries, full_url=full_url)


def render_index(index: LlmsIndex) -> str:
    """Render an index as compact `llms.txt`-style markdown — the tool's response.

    Maps the index onto the shared markdown template (`core.template`): links as
    the source has them, then the other files found on the domain, with sizes.
    """
    links = [
        Link(title=entry.title, url=entry.url, description=entry.description, section=entry.section)
        for entry in index.entries
    ]
    trailing: list[str] = []
    for variant in index.variants:
        if variant.name == LLMS_INDEX_NAME:
            continue
        size = f" — {variant.size} bytes" if variant.size is not None else ""
        note = " (read via llms_search scope=full)" if variant.name == LLMS_FULL_NAME else ""
        trailing.append(f"{variant.name}{size}{note}")
    return markdown_index(
        index.title, index.summary, links, trailing_heading="Other files on the domain", trailing=trailing
    )


def render_search(result: SearchResult) -> str:
    """Render search matches as markdown: hits grouped by domain, skipped sources listed.

    Uses the shared index template — a match is a link (title, url) whose
    description is the excerpt collapsed to one line, so long full-file excerpts
    stay valid list items; the real page is read via llms_fetch.
    """
    links = [
        Link(
            title=hit.title,
            url=hit.url,
            description=" ".join(hit.text.split()) + (" …" if hit.truncated else ""),
            section=hit.domain,
        )
        for hit in result.hits
    ]
    searched = ", ".join(result.searched) or "none"
    summary = f"{result.total} match(es), {result.scope} scope; searched: {searched}"
    if result.total > len(result.hits):
        summary += f" (showing first {len(result.hits)})"
    return markdown_index(
        f"Search: {result.query}", summary, links, trailing_heading="Skipped", trailing=result.skipped
    )


def render_page(page: Page) -> str:
    """Render a fetched page as markdown: the text as-is, framed only when chunked.

    A whole page is returned untouched (it is already a document). A chunk gets
    a one-line header with its character range and, unless it's the last, a note
    with the offset to read from next.
    """
    if page.offset == 0 and page.next_offset is None:
        return page.text if page.text.endswith("\n") else page.text + "\n"
    header = f"_{page.url} — chars {page.offset}–{page.offset + len(page.text)} of {page.length}_\n\n"
    footer = "" if page.next_offset is None else f"\n\n_next: llms_fetch with offset={page.next_offset}_"
    return f"{header}{page.text}{footer}\n"


def sections(text: str) -> list[tuple[str, str]]:
    """Split markdown by headings up to level three.

    `# …` lines inside a fenced code block (``` or ~~~) don't count as
    headings: otherwise a comment in a bash example would split a section in
    half.

    Returns:
        Pairs of (heading, section text including the heading); text before
        the first heading forms a section with an empty heading.
    """
    starts: list[int] = []
    fenced = False
    pos = 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and _HEADING.match(line):
            starts.append(pos)
        pos += len(line)
    result = []
    for begin, end in pairwise([0, *starts, len(text)]):
        chunk = text[begin:end].strip()
        if not chunk:
            continue
        first = chunk.split("\n", 1)[0]
        heading = first.lstrip("#").strip() if first.startswith("#") else ""
        result.append((heading, chunk))
    return result


def _matches(text: str, words: list[str]) -> bool:
    lowered = text.lower()
    return all(word in lowered for word in words)


def matching_sections(text: str, words: list[str], limit: int) -> list[tuple[str, str, bool]]:
    """Sections containing all the words: heading, excerpt up to `limit` characters, whether trimmed.

    A pure function for use in a thread: on a full file of tens of megabytes,
    splitting and searching would tie up the event loop for seconds.
    """
    return [(heading, chunk[:limit], len(chunk) > limit) for heading, chunk in sections(text) if _matches(chunk, words)]


class Session:
    """One tool call: settings plus an HTTP client with a shared connection pool."""

    def __init__(self, s: Settings | None = None) -> None:
        self.s = s or get_settings()
        self._client = make_client(self.s, self._guard)
        self._public: set[str] = set()

    async def __aenter__(self) -> Self:
        """Open the connection pool."""
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        """Close the connection pool."""
        await self._client.aclose()

    async def _guard(self, request: httpx2.Request) -> None:
        """Check the address before sending every request, including redirects."""
        host = check_url(str(request.url))
        if host not in self._public:
            await resolve_public(host)
            self._public.add(host)

    async def fetch(self, url: str, method: str = "GET", *, cache: bool = True) -> Fetched:
        """Download a resource, with caching and a size cap.

        The response status is returned rather than turned into an error:
        the domain check needs 404 too. HEAD doesn't pull the body: it's used
        to check whether files exist. `cache=False` bypasses the on-disk
        cache: that's how liveness is checked right now. The address and every
        redirect hop is checked by `_guard` before it's sent.

        Raises:
            UserError: address isn't https or is non-public, the network is
                unreachable, a timeout occurs, the redirect chain is too long,
                or the body exceeds `llms_max_bytes`.
        """
        s = self.s
        check_url(url)  # such an address wouldn't be in the cache anyway, but checking is cheaper than arguing about it
        if cache and (hit := await anyio.to_thread.run_sync(_cached, method, url, s)) is not None:
            return hit
        body = bytearray()
        try:
            async with self._client.stream(method, url) as response:
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > s.llms_max_bytes:
                        raise UserError(f"{url}: exceeds the cap of {s.llms_max_bytes} bytes")
                length = response.headers.get("content-length")
                fetched = Fetched(
                    status=response.status_code,
                    content_type=response.headers.get("content-type", ""),
                    content_length=int(length) if length and length.isdigit() else None,
                    body=bytes(body),
                )
        except httpx2.HTTPError as err:
            raise UserError(f"failed to download {url}: {err}") from err
        log.debug("%s %s -> %s, %d bytes", method, url, fetched.status, len(fetched.body))
        if cache and fetched.cacheable:
            await anyio.to_thread.run_sync(_store, method, url, fetched, s)
        return fetched

    async def is_stub(self, url: str, *, cache: bool) -> bool:
        """Whether the domain returns success for a junk path next to `url` (HEAD)."""
        junk = await self.fetch(urljoin(url, LLMS_JUNK_NAME), "HEAD", cache=cache)
        return junk.ok

    async def fetch_ok(self, url: str) -> Fetched:
        """Download a document; a failed status or an SPA stub is a call error.

        HTML in the response isn't a verdict by itself: an honest domain can
        return an HTML page. The verdict is HTML plus a successful response on
        a junk path next to it.

        Raises:
            UserError: network error, response status isn't 2xx/3xx, or an
                SPA stub.
        """
        fetched = await self.fetch(url)
        if not fetched.ok:
            raise UserError(f"{url}: HTTP {fetched.status}")
        if fetched.is_html and await self.is_stub(url, cache=True):
            raise UserError(f"{url}: got an HTML shell, and the domain returns success for any path — an SPA stub")
        return fetched

    async def variants(self, index: str) -> list[Variant]:
        """Which of `LLMS_VARIANTS` exist next to the index (via HEAD, in parallel).

        A domain that returns 200 for anything serves an HTML shell for a
        missing file, so "exists" means a successful status and a
        non-HTML content type.
        """

        async def probe(name: str) -> Variant | None:
            try:
                head = await self.fetch(urljoin(index, name), "HEAD")
            except UserError:
                return None
            if not head.ok or head.is_html:
                return None
            return Variant(name=name, size=head.content_length)

        return [found for found in await gather(probe, LLMS_VARIANTS) if found is not None]

    async def index(self, source: str) -> LlmsIndex:
        """A source's index without variants: download and parse."""
        url = index_url(source, self.s)
        return parse_index((await self.fetch_ok(url)).text, url)

    async def load_index(self, source: str) -> LlmsIndex:
        """A source's index together with the file variants next to it."""
        index = await self.index(source)
        index.variants = await self.variants(index.url)
        return index

    async def check(self, known: KnownSource) -> SourceStatus:
        """Whether a source is alive right now: HEAD on the index bypassing the cache, plus a junk probe on HTML."""
        state: SourceState = "ok"
        detail = ""
        try:
            head = await self.fetch(known.index, "HEAD", cache=False)
            if not head.ok:
                state, detail = "unavailable", f"HTTP {head.status}"
            elif head.is_html:
                state = "stub" if await self.is_stub(known.index, cache=False) else "unavailable"
                detail = "HTML shell instead of the index"
        except UserError as err:
            state, detail = "unavailable", str(err)
        return SourceStatus(**known.model_dump(), state=state, detail=detail)

    async def verify_sources(self, *, refresh: bool = False) -> SourcesResult:
        """All sources with their check outcome; outcomes live in the runtime dir until the TTL expires.

        The runtime dir lives until the machine reboots but survives a server
        restart: sources aren't re-polled across sessions. All of them are
        polled at once, bypassing the download cache.
        """
        s = self.s
        known = all_sources(s)
        age, saved = store.load_stamped(s.llms_status_file)
        if not refresh and age < s.llms_status_ttl:
            # The saved data might have been written by a different registry
            # composition, or be corrupt: re-check instead of failing.
            with contextlib.suppress(KeyError, ValidationError):
                rows = _VERDICTS.validate_python(saved.get("sources"))
                statuses = [SourceStatus(**item.model_dump(), **rows[item.domain].model_dump()) for item in known]
                return SourcesResult(checked_ago=age, sources=statuses, variants=list(LLMS_VARIANTS))
        statuses = await gather(self.check, known)
        with contextlib.suppress(OSError):
            store.save_stamped(
                s.llms_status_file,
                {"sources": {st.domain: st.model_dump(include={"state", "detail"}) for st in statuses}},
            )
        return SourcesResult(checked_ago=0.0, sources=statuses, variants=list(LLMS_VARIANTS))

    async def _full_size(self, index_url: str) -> int | None:
        """Size of `llms-full.txt` next to the index (a single HEAD); None means no such file."""
        try:
            head = await self.fetch(urljoin(index_url, LLMS_FULL_NAME), "HEAD")
        except UserError:
            return None
        return head.content_length if head.ok and not head.is_html else None

    async def add_source(self, domain: str, covers: str, index: str | None) -> SourceStatus:
        """Check a source over the network, add it to the user file, and return it.

        The index is downloaded in full: it must be text with at least one
        link. The size of `llms-full.txt` next to it is found with a single
        HEAD; other variants (small, ctx…) aren't probed — llms_index shows
        those.

        Raises:
            UserError: domain already exists, there's no index, it's a stub,
                or it has no links.
            OSError: the sources file couldn't be written.
        """
        s = self.s
        if find_source(domain, s) is not None:
            raise UserError(f"source {domain!r} already exists")
        parsed = await self.index(index or domain)
        if not parsed.entries:
            raise UserError(f"{parsed.url}: index has no links at all — this isn't an llms.txt")
        known = KnownSource(domain=domain, index=parsed.url, covers=covers, full_size=await self._full_size(parsed.url))
        _save_sources([*custom_sources(s), known], s)
        _forget_status(s)
        return SourceStatus(**known.model_dump(), state="ok", detail="")

    def _index_hits(self, index: LlmsIndex, words: list[str]) -> list[SearchHit]:
        domain = domain_of(index.url, self.s)
        return [
            SearchHit(domain=domain, title=entry.title, url=entry.url, text=entry.description, truncated=False)
            for entry in index.entries
            if _matches(f"{entry.title} {entry.description} {entry.url}", words)
        ]

    async def _full_hits(self, index: LlmsIndex, words: list[str]) -> list[SearchHit]:
        full_url = index.full_url or urljoin(index.url, LLMS_FULL_NAME)
        fetched = await self.fetch_ok(full_url)
        domain = domain_of(index.url, self.s)
        found = await anyio.to_thread.run_sync(matching_sections, fetched.text, words, self.s.llms_hit_chars)
        return [
            SearchHit(domain=domain, title=heading, url=full_url, text=text, truncated=truncated)
            for heading, text, truncated in found
        ]

    async def search(self, query: str, source: str | None, scope: SearchScope) -> SearchResult:
        """Find the query words (all of them, case-insensitive).

        Without `source` — over the tables of contents of every registry
        source that the last check found alive, in parallel; the rest, and
        any that fail along the way, are listed in `skipped`.

        Raises:
            UserError: empty query, `full` without a source, or the named
                source is dishonest or has no file.
        """
        words = query.lower().split()
        if not words:
            raise UserError("empty query")
        if source is None and scope == "full":
            raise UserError("searching full requires a specified source")

        async def one(target: str) -> list[SearchHit]:
            index = await self.index(target)
            return self._index_hits(index, words) if scope == "index" else await self._full_hits(index, words)

        async def guarded(target: str) -> list[SearchHit] | str:
            try:
                return await one(target)
            except UserError as err:
                return f"{target}: {err}"

        skipped: list[str] = []
        outcomes: list[list[SearchHit] | str]
        if source is not None:
            targets = [source]
            outcomes = [await one(source)]
        else:
            verified = (await self.verify_sources()).sources
            targets = [st.domain for st in verified if st.state == "ok"]
            skipped += [f"{st.domain}: {st.detail}" for st in verified if st.state != "ok"]
            outcomes = await gather(guarded, targets)

        hits: list[SearchHit] = []
        searched: list[str] = []
        for target, outcome in zip(targets, outcomes, strict=True):
            if isinstance(outcome, str):
                skipped.append(outcome)
            else:
                searched.append(target)
                hits += outcome
        return SearchResult(
            query=query,
            scope=scope,
            searched=searched,
            skipped=skipped,
            total=len(hits),
            hits=hits[: self.s.llms_max_hits],
        )

    async def fetch_page(self, url: str, offset: int) -> Page:
        """A page by its index address, a chunk of `llms_page_chars` starting at `offset`.

        Raises:
            UserError: it's `llms-full.txt` (read only via search), the domain
                is dishonest, the page doesn't exist, or the network is
                unreachable.
        """
        if urlsplit(url).path.rsplit("/", 1)[-1] == LLMS_FULL_NAME:
            raise UserError(f"{url}: {LLMS_FULL_NAME} isn't served in full — use llms_search with scope=full")
        fetched = await self.fetch_ok(url)
        text = fetched.text
        end = offset + self.s.llms_page_chars
        return Page(
            url=url,
            content_type=fetched.content_type,
            length=len(text),
            offset=offset,
            text=text[offset:end],
            next_offset=end if end < len(text) else None,
        )
