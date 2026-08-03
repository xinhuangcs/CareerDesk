"""Safely fetch public search results and extract page text.

Only HTTP(S) public addresses are allowed; redirects are revalidated hop by hop, page
size is bounded, JavaScript is never executed, and same-host requests are paced.
Fetched text is ephemeral and never persisted here.
"""

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 10
FETCH_CONCURRENCY = 4
MAX_PAGE_BYTES = 800_000
MAX_PAGE_TEXT_CHARS = 12_000
MAX_REDIRECTS = 3
SAME_HOST_INTERVAL_SECONDS = 0.5
USER_AGENT = "CareerDeskResearch/1.0 (open-source local job-prep assistant)"

_SKIP_CONTENT_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head"})
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "li", "ul", "ol", "table", "tr",
    "h1", "h2", "h3", "h4", "h5", "h6", "br", "blockquote", "pre",
})
_DATE_META_KEYS = frozenset({
    "article:published_time", "article:modified_time", "og:updated_time",
    "date", "pubdate", "publishdate", "publish-date", "dateline",
})
_DATE_PATTERN = re.compile(
    r"(20\d{2})[-/年.](1[0-2]|0?[1-9])(?:[-/月.](3[01]|[12]\d|0?[1-9]))?",
)


@dataclass(frozen=True)
class FetchedPage:
    """One extracted page; date_hint is an optional self-reported publication date."""

    url: str
    title: str
    text: str
    date_hint: str | None


class _TextExtractor(HTMLParser):
    """Stdlib text extractor that skips scripts/styles and collects title/date clues."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self.title = ""
        self.date_hint: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")
        if self.date_hint is None and tag in ("meta", "time"):
            attributes = dict(attrs)
            if tag == "time" and attributes.get("datetime"):
                self.date_hint = str(attributes["datetime"])[:32]
            else:
                key = (attributes.get("property") or attributes.get("name") or "").lower()
                if key in _DATE_META_KEYS and attributes.get("content"):
                    self.date_hint = str(attributes["content"])[:32]

    def handle_endtag(self, tag):
        if tag in _SKIP_CONTENT_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def extract_page_text(html_text: str) -> tuple[str, str, str | None]:
    """Convert HTML to text, title, and date hint; return empty text on failure."""
    parser = _TextExtractor()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:   # noqa: BLE001 - malformed wild HTML counts as no text
        return "", "", None
    text = parser.text()[:MAX_PAGE_TEXT_CHARS]
    date_hint = parser.date_hint
    if date_hint is None:
        matched = _DATE_PATTERN.search(text[:2_000])
        if matched:
            date_hint = matched.group(0)
    return text, " ".join(parser.title.split())[:200], date_hint


def _host_is_public(host: str) -> bool:
    """Resolve a host and require every address to be public; fail closed."""
    if not host or host.lower() in ("localhost",):
        return False
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    if not addresses:
        return False
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            return False
        if not address.is_global or address.is_multicast:
            return False
    return True


def _url_allowed(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


class PageFetcher:
    """Page fetcher with global concurrency and per-host pacing."""

    def __init__(self):
        self._gate = asyncio.Semaphore(FETCH_CONCURRENCY)
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}

    def _host_lock(self, host: str) -> asyncio.Lock:
        return self._host_locks.setdefault(host, asyncio.Lock())

    async def _polite_wait(self, host: str) -> None:
        loop = asyncio.get_running_loop()
        last = self._host_last.get(host, 0.0)
        wait = last + SAME_HOST_INTERVAL_SECONDS - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
        self._host_last[host] = loop.time()

    async def _read_bounded(self, response: httpx.Response) -> bytes | None:
        content_type = (response.headers.get("content-type") or "").lower()
        if content_type and not any(
                marker in content_type for marker in ("text/html", "text/plain",
                                                      "application/xhtml")):
            return None
        collected = bytearray()
        async for chunk in response.aiter_bytes():
            collected += chunk
            if len(collected) > MAX_PAGE_BYTES:
                break
        return bytes(collected[:MAX_PAGE_BYTES])

    async def _fetch_one(self, client: httpx.AsyncClient, url: str) -> FetchedPage | None:
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            if not _url_allowed(current):
                return None
            host = urlsplit(current).hostname or ""
            if not await asyncio.to_thread(_host_is_public, host):
                return None
            async with self._host_lock(host):
                await self._polite_wait(host)
                try:
                    async with client.stream("GET", current) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location:
                                return None
                            # Follow manually and revalidate every redirect against SSRF pivots.
                            current = urljoin(current, location)
                            continue
                        if response.status_code != 200:
                            return None
                        body = await self._read_bounded(response)
                except Exception:   # noqa: BLE001 - one failed page simply yields no material
                    return None
            if body is None:
                return None
            text, title, date_hint = extract_page_text(
                body.decode(encoding="utf-8", errors="replace"),
            )
            if not text.strip():
                return None
            return FetchedPage(url=url, title=title, text=text, date_hint=date_hint)
        return None

    async def fetch_pages(self, urls: list[str]) -> dict[str, FetchedPage]:
        """Fetch a URL batch concurrently, omitting failures without raising."""
        unique = list(dict.fromkeys(url for url in urls if url))
        if not unique:
            return {}
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_SECONDS,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
            trust_env=False,
        ) as client:
            async def bounded(url: str) -> FetchedPage | None:
                async with self._gate:
                    return await self._fetch_one(client, url)

            pages = await asyncio.gather(*(bounded(url) for url in unique))
        return {page.url: page for page in pages if page is not None}
