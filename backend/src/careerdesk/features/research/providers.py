"""Search adapters and provider pool, preferring official APIs over ddgs fallback.

Providers connect to official endpoints with user-supplied credentials. Upstream
exceptions are reduced to provider-level errors so text that may contain keys or
URLs never reaches materials, cache, or users. Adapter docstrings link to the
authoritative documentation.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

import httpx

from .queries import PlannedQuery

logger = logging.getLogger(__name__)

SEARCH_TIMEOUT_SECONDS = 15
SEARCH_MAX_RESULTS = 8
POOL_CONCURRENCY = 6
# Provider count for key-query cross-checks; deep mode broadcasts every query.
KEY_QUERY_OUTLETS = 2

TAVILY_KEY_ENV = "TAVILY_API_KEY"
BRAVE_KEY_ENV = "BRAVE_API_KEY"
GOOGLE_PSE_KEY_ENV = "GOOGLE_PSE_API_KEY"
GOOGLE_PSE_ENGINE_ENV = "GOOGLE_PSE_ENGINE_ID"
SEARXNG_BASE_URL_ENV = "SEARXNG_BASE_URL"


@dataclass(frozen=True)
class SearchHit:
    """One hit; raw_content may contain provider-supplied page text."""

    title: str
    url: str
    snippet: str
    engine: str
    raw_content: str | None = None


@dataclass
class QueryOutcome:
    """Execution result for one query across the provider pool."""

    query: PlannedQuery
    hits: list[SearchHit] = field(default_factory=list)
    failed_engines: list[str] = field(default_factory=list)


class SearchProviderError(RuntimeError):
    """Sanitized provider failure that carries no upstream details."""

    def __init__(self, engine: str):
        self.engine = engine
        super().__init__(f"search provider unavailable: {engine}")


def _clean_text(value) -> str:
    return " ".join(str(value or "").split())


class SearchProvider:
    """Single-concurrency provider with an optional minimum call interval."""

    name = "base"
    supports_freshness = False
    chinese_strength = 1  # Routing hint for Chinese result quality, from 0 to 2.
    min_interval_seconds = 0.0

    def __init__(self):
        self._gate = asyncio.Lock()
        self._last_call = 0.0

    async def search(self, client: httpx.AsyncClient, query: PlannedQuery) -> list[SearchHit]:
        async with self._gate:
            wait = self._last_call + self.min_interval_seconds - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                hits = await self._run(client, query)
            except Exception as error:  # noqa: BLE001 -- upstream details may contain keys
                logger.warning("search provider %s failed: %s", self.name,
                               type(error).__name__)
                raise SearchProviderError(self.name) from None
            finally:
                self._last_call = time.monotonic()
        return [hit for hit in hits if hit.url and (hit.title or hit.snippet)]

    async def _run(self, client: httpx.AsyncClient, query: PlannedQuery) -> list[SearchHit]:
        raise NotImplementedError


class TavilyProvider(SearchProvider):
    """Official Tavily Search API adapter.

    https://docs.tavily.com/documentation/api-reference/endpoint/search
    """

    name = "Tavily"
    supports_freshness = True
    chinese_strength = 2

    def __init__(self, api_key: str):
        super().__init__()
        self._api_key = api_key

    async def _run(self, client: httpx.AsyncClient, query: PlannedQuery) -> list[SearchHit]:
        payload = {
            "query": query.text,
            "max_results": SEARCH_MAX_RESULTS,
            "search_depth": "basic",
            "include_raw_content": "text",
        }
        if query.country:
            payload["country"] = query.country
        if query.kind == "news":
            payload["topic"] = "news"
            payload["time_range"] = "year"
        response = await client.post(
            "https://api.tavily.com/search",
            json=payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        response.raise_for_status()
        data = response.json()
        return [
            SearchHit(
                title=_clean_text(item.get("title")),
                url=str(item.get("url") or ""),
                snippet=_clean_text(item.get("content")),
                raw_content=(item.get("raw_content") or None),
                engine=self.name,
            )
            for item in (data.get("results") or [])
        ]


class BraveProvider(SearchProvider):
    """Official Brave Web Search API adapter.

    https://api-dashboard.search.brave.com/app/documentation
    """

    name = "Brave"
    supports_freshness = True
    chinese_strength = 1
    min_interval_seconds = 1.1  # Free tier permits one query per second.

    def __init__(self, api_key: str):
        super().__init__()
        self._api_key = api_key

    async def _run(self, client: httpx.AsyncClient, query: PlannedQuery) -> list[SearchHit]:
        params = {"q": query.text, "count": SEARCH_MAX_RESULTS}
        if query.language:
            params["search_lang"] = "zh-hans" if query.language == "zh" else "en"
        if query.country:
            params["country"] = query.country
        if query.kind == "news":
            params["freshness"] = "py"
        response = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers={
                "X-Subscription-Token": self._api_key,
                "Accept": "application/json",
            },
        )
        response.raise_for_status()
        results = ((response.json().get("web") or {}).get("results")) or []
        hits = []
        for item in results:
            snippets = [item.get("description") or ""]
            snippets += item.get("extra_snippets") or []
            hits.append(SearchHit(
                title=_clean_text(item.get("title")),
                url=str(item.get("url") or ""),
                snippet=_clean_text(" ".join(filter(None, snippets))),
                engine=self.name,
            ))
        return hits


class GooglePseProvider(SearchProvider):
    """Google Programmable Search JSON API（https://developers.google.com/custom-search/v1/using_rest）。"""

    name = "Google"
    supports_freshness = True
    chinese_strength = 2

    def __init__(self, api_key: str, engine_id: str):
        super().__init__()
        self._api_key = api_key
        self._engine_id = engine_id

    async def _run(self, client: httpx.AsyncClient, query: PlannedQuery) -> list[SearchHit]:
        params = {
            "key": self._api_key,
            "cx": self._engine_id,
            "q": query.text,
            "num": SEARCH_MAX_RESULTS,
        }
        if query.kind == "news":
            params["dateRestrict"] = "m12"
        if query.language:
            params["lr"] = "lang_zh-CN" if query.language == "zh" else "lang_en"
        if query.country:
            params["gl"] = query.country.lower()
        response = await client.get(
            "https://www.googleapis.com/customsearch/v1", params=params,
        )
        response.raise_for_status()
        return [
            SearchHit(
                title=_clean_text(item.get("title")),
                url=str(item.get("link") or ""),
                snippet=_clean_text(item.get("snippet")),
                engine=self.name,
            )
            for item in (response.json().get("items") or [])
        ]


class SearxngProvider(SearchProvider):
    """User-hosted SearXNG JSON API adapter.

    The user's instance must enable JSON in settings.yml and remains entirely
    under the user's control. https://docs.searxng.org/dev/search_api.html
    """

    name = "SearXNG"
    supports_freshness = True
    chinese_strength = 1

    def __init__(self, base_url: str):
        super().__init__()
        self._base_url = base_url.rstrip("/")

    async def _run(self, client: httpx.AsyncClient, query: PlannedQuery) -> list[SearchHit]:
        params = {"q": query.text, "format": "json"}
        if query.language:
            params["language"] = "zh-CN" if query.language == "zh" else "en"
        if query.kind == "news":
            params["time_range"] = "year"
        response = await client.get(f"{self._base_url}/search", params=params)
        response.raise_for_status()
        return [
            SearchHit(
                title=_clean_text(item.get("title")),
                url=str(item.get("url") or ""),
                snippet=_clean_text(item.get("content")),
                engine=self.name,
            )
            for item in (response.json().get("results") or [])[:SEARCH_MAX_RESULTS]
        ]


class DdgsProvider(SearchProvider):
    """Unofficial ddgs fallback used only without official providers."""

    name = "DuckDuckGo"
    chinese_strength = 0

    async def _run(self, client: httpx.AsyncClient, query: PlannedQuery) -> list[SearchHit]:
        return await asyncio.to_thread(self._search_sync, query.text, query.language)

    def _search_sync(self, text: str, language: str | None = None) -> list[SearchHit]:
        from ddgs import DDGS

        with DDGS() as ddgs:
            # ddgs silently maps unknown backends to auto. Verify the pinned parser
            # resolves only DuckDuckGo and fail before I/O if its private seam drifts.
            engines = ddgs._get_engines("text", "duckduckgo")  # noqa: SLF001
            if len(engines) != 1 or getattr(engines[0], "name", None) != "duckduckgo":
                raise RuntimeError("DuckDuckGo 搜索适配器契约已变化")
            kwargs = {"max_results": SEARCH_MAX_RESULTS, "backend": "duckduckgo"}
            if language is not None:
                kwargs["region"] = "cn-zh" if language == "zh" else "wt-wt"
            rows = ddgs.text(text, **kwargs)
        return [
            SearchHit(
                title=_clean_text(row.get("title")),
                url=str(row.get("href") or ""),
                snippet=_clean_text(row.get("body")),
                engine=self.name,
            )
            for row in rows
        ]


class ProviderPool:
    """Cross-check key queries, route long tail, rebalance failures, broadcast deep."""

    def __init__(self, providers: list[SearchProvider], *, fallback: SearchProvider | None,
                 deep: bool = False):
        self._providers = providers
        self._fallback = fallback
        self._deep = deep
        self._round_robin = 0

    @property
    def has_outlets(self) -> bool:
        return bool(self._providers) or self._fallback is not None

    def describe(self) -> list[str]:
        names = [provider.name for provider in self._providers]
        if self._fallback is not None:
            names.append(f"{self._fallback.name}（兜底）")
        return names

    def _ranked(self, query: PlannedQuery) -> list[SearchProvider]:
        def score(provider: SearchProvider) -> tuple:
            freshness = provider.supports_freshness if query.kind == "news" else False
            chinese = provider.chinese_strength if query.language != "en" else 1
            return (not freshness, -chinese)

        ordered = sorted(self._providers, key=score)
        if not ordered:
            return []
        # Rotate equal-score providers so long-tail traffic does not drain one quota.
        self._round_robin = (self._round_robin + 1) % len(ordered)
        rotation = ordered[self._round_robin:] + ordered[:self._round_robin]
        rotation.sort(key=score)
        return rotation

    def _assign(self, query: PlannedQuery) -> list[SearchProvider]:
        if self._deep:
            return list(self._providers) or ([self._fallback] if self._fallback else [])
        ranked = self._ranked(query)
        if not ranked:
            return [self._fallback] if self._fallback else []
        count = KEY_QUERY_OUTLETS if query.key else 1
        return ranked[:count]

    async def _run_query(self, client: httpx.AsyncClient, query: PlannedQuery,
                         gate: asyncio.Semaphore) -> QueryOutcome:
        outcome = QueryOutcome(query=query)
        assigned = self._assign(query)
        backups = [provider for provider in self._providers if provider not in assigned]
        if self._fallback is not None:
            backups.append(self._fallback)
        for provider in assigned:
            async with gate:
                try:
                    outcome.hits += await provider.search(client, query)
                except SearchProviderError as error:
                    outcome.failed_engines.append(error.engine)
        while not outcome.hits and backups:
            provider = backups.pop(0)
            async with gate:
                try:
                    outcome.hits += await provider.search(client, query)
                except SearchProviderError as error:
                    outcome.failed_engines.append(error.engine)
        return outcome

    async def run_plan(self, queries: list[PlannedQuery]) -> list[QueryOutcome]:
        """Run a query plan concurrently without one failure breaking the whole."""
        if not self.has_outlets:
            return [QueryOutcome(query=query) for query in queries]
        gate = asyncio.Semaphore(POOL_CONCURRENCY)
        async with httpx.AsyncClient(
            timeout=SEARCH_TIMEOUT_SECONDS,
            follow_redirects=True,
        ) as client:
            return list(await asyncio.gather(*(
                self._run_query(client, query, gate) for query in queries
            )))


def build_provider_pool(*, enabled: bool, deep: bool = False,
                        ddg_fallback: bool = True) -> ProviderPool | None:
    """Build configured providers, or none when web research is unauthorized.

    ``deep`` broadcasts every query to every provider, using roughly triple the
    quota. ``ddg_fallback`` independently controls the unofficial fallback.
    """
    if not enabled:
        return None
    providers: list[SearchProvider] = []
    tavily_key = (os.getenv(TAVILY_KEY_ENV) or "").strip()
    if tavily_key:
        providers.append(TavilyProvider(tavily_key))
    brave_key = (os.getenv(BRAVE_KEY_ENV) or "").strip()
    if brave_key:
        providers.append(BraveProvider(brave_key))
    google_key = (os.getenv(GOOGLE_PSE_KEY_ENV) or "").strip()
    google_engine = (os.getenv(GOOGLE_PSE_ENGINE_ENV) or "").strip()
    if google_key and google_engine:
        providers.append(GooglePseProvider(google_key, google_engine))
    searxng_url = (os.getenv(SEARXNG_BASE_URL_ENV) or "").strip()
    if searxng_url.startswith(("http://", "https://")):
        providers.append(SearxngProvider(searxng_url))
    fallback = DdgsProvider() if ddg_fallback else None
    # Return an empty enabled pool when no outlet exists so callers can distinguish
    # unavailable providers from missing authorization.
    return ProviderPool(providers, fallback=fallback, deep=deep)
