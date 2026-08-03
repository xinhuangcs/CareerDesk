"""Clean, deduplicate, score, bucket, and number retrieval materials.

This deterministic pipeline normalizes URLs, merges syndicated content, scores
site authority and recency, and enforces input budgets. Raw page text remains in
memory and is never persisted.
"""

import re
from dataclasses import dataclass, field, replace
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .fetcher import FetchedPage
from .providers import QueryOutcome

# Per-material body character limit; snippet-only materials are unaffected.
MAX_MATERIAL_TEXT_CHARS = 4_000
# Total character and item bounds for each company/role material bucket.
LEG_CHAR_BUDGET = 26_000
LEG_MATERIAL_LIMIT = 14
# Normalized prefix length used for content-similarity deduplication.
CONTENT_DEDUP_PREFIX_CHARS = 240

# Site weights; official domains receive an additional bucket-time bonus.
DEFAULT_SITE_WEIGHT = 2
SITE_WEIGHTS = {
    "nowcoder.com": 4,
    "1point3acres.com": 4,
    "zhihu.com": 3,
    "juejin.cn": 3,
    "maimai.cn": 3,
    "github.com": 3,
    "36kr.com": 3,
    "leetcode.cn": 3,
    "glassdoor.com": 3,
    "infoq.cn": 3,
    "csdn.net": 2,
    "xiaohongshu.com": 2,
}
_TRACKING_PARAM_PATTERN = re.compile(r"^(utm_|spm|from|ref|share|src)")
_YEAR_PATTERN = re.compile(r"(20\d{2})")


@dataclass
class Material:
    """One material whose one-based index is assigned after bucketing."""

    url: str
    canonical_url: str
    site: str
    title: str
    snippet: str
    text: str
    date_hint: str | None
    engines: list[str] = field(default_factory=list)
    legs: set[str] = field(default_factory=set)
    sections: set[str] = field(default_factory=set)
    index: int = 0

    def best_content(self) -> str:
        if self.text:
            return self.text[:MAX_MATERIAL_TEXT_CHARS]
        return self.snippet

    def content_chars(self) -> int:
        return len(self.best_content()) + len(self.title)


def canonicalize_url(url: str) -> str:
    """Normalize a URL for same-page deduplication and remove tracking fields."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    if not parts.scheme or not parts.hostname:
        return url.strip()
    query = urlencode([
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _TRACKING_PARAM_PATTERN.match(key.lower())
    ])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def site_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _site_weight(site: str, anchor_domain: str | None) -> int:
    if anchor_domain and site and (site == anchor_domain or site.endswith(f".{anchor_domain}")):
        return 6
    for suffix, weight in SITE_WEIGHTS.items():
        if site == suffix or site.endswith(f".{suffix}"):
            return weight
    return DEFAULT_SITE_WEIGHT


def _freshness_bonus(date_hint: str | None, *, today: str) -> int:
    if not date_hint:
        return 0
    matched = _YEAR_PATTERN.search(date_hint)
    if not matched:
        return 0
    age = date.fromisoformat(today).year - int(matched.group(1))
    if age <= 0:
        return 2
    if age == 1:
        return 1
    return -1 if age >= 3 else 0


def _normalized_prefix(text: str) -> str:
    return "".join(text.split()).casefold()[:CONTENT_DEDUP_PREFIX_CHARS]


def build_materials(outcomes: list[QueryOutcome], pages: dict[str, FetchedPage]) -> list[Material]:
    """Merge hits and fetched pages into deduplicated, unnumbered materials."""
    by_url: dict[str, Material] = {}
    for outcome in outcomes:
        for hit in outcome.hits:
            canonical = canonicalize_url(hit.url)
            page = pages.get(hit.url)
            text = ""
            date_hint = None
            title = hit.title
            if page is not None:
                text = page.text
                date_hint = page.date_hint
                title = title or page.title
            elif hit.raw_content:
                text = " ".join(hit.raw_content.split())
            material = by_url.get(canonical)
            if material is None:
                by_url[canonical] = Material(
                    url=hit.url, canonical_url=canonical, site=site_of(hit.url),
                    title=title, snippet=hit.snippet, text=text, date_hint=date_hint,
                    engines=[hit.engine], legs={outcome.query.leg},
                    sections={outcome.query.section},
                )
                continue
            if hit.engine not in material.engines:
                material.engines.append(hit.engine)
            material.legs.add(outcome.query.leg)
            material.sections.add(outcome.query.section)
            if len(text) > len(material.text):
                material.text = text
            if date_hint and not material.date_hint:
                material.date_hint = date_hint
            if len(hit.snippet) > len(material.snippet):
                material.snippet = hit.snippet
            if title and not material.title:
                material.title = title

    # For syndicated copies with the same normalized prefix, retain the richer
    # item and merge provider labels.
    by_content: dict[str, Material] = {}
    unique: list[Material] = []
    for material in by_url.values():
        prefix = _normalized_prefix(material.best_content())
        if not prefix:
            unique.append(material)
            continue
        existing = by_content.get(prefix)
        if existing is None:
            by_content[prefix] = material
            unique.append(material)
            continue
        existing.legs |= material.legs
        existing.sections |= material.sections
        for engine in material.engines:
            if engine not in existing.engines:
                existing.engines.append(engine)
        if len(material.text) > len(existing.text):
            existing.text, existing.title = material.text, existing.title or material.title
            existing.url, existing.canonical_url = material.url, material.canonical_url
            existing.site = material.site
    return unique


def select_fetch_urls(outcomes: list[QueryOutcome], *, limit: int) -> list[str]:
    """Select high-value bodies to fetch, skipping hits with provider content."""
    ranked: list[tuple[tuple, str]] = []
    seen: set[str] = set()
    for outcome in outcomes:
        for position, hit in enumerate(outcome.hits):
            if hit.raw_content:
                continue
            canonical = canonicalize_url(hit.url)
            if canonical in seen:
                continue
            seen.add(canonical)
            weight = _site_weight(site_of(hit.url), None)
            ranked.append(((not outcome.query.key, -weight, position), hit.url))
    ranked.sort(key=lambda item: item[0])
    return [url for _key, url in ranked[:limit]]


def bucket_leg_materials(materials: list[Material], *, leg: str, anchor_domain: str | None,
                         today: str) -> tuple[list[Material], int]:
    """Score and bucket one leg to its budget, then assign one-based indices.

    Returns selected materials and the number excluded by the budget.
    """
    candidates = [material for material in materials if leg in material.legs]

    def score(material: Material) -> float:
        value = _site_weight(material.site, anchor_domain)
        value += 2 if material.text else 0
        value += _freshness_bonus(material.date_hint, today=today)
        value += len(material.sections) * 0.1
        return value

    candidates.sort(key=score, reverse=True)
    chosen: list[Material] = []
    used_chars = 0
    dropped = 0
    for material in candidates:
        chars = material.content_chars()
        if len(chosen) >= LEG_MATERIAL_LIMIT or used_chars + chars > LEG_CHAR_BUDGET:
            dropped += 1
            continue
        used_chars += chars
        chosen.append(material)
    # Return independently numbered copies because one material may enter both
    # legs; in-place indices would corrupt citations in the first bucket.
    selected = [
        replace(material, index=position)
        for position, material in enumerate(chosen, start=1)
    ]
    return selected, dropped


def materials_payload(materials: list[Material]) -> list[dict]:
    """Project materials into numbered composition input with source and body."""
    return [
        {
            "source_index": material.index,
            "url": material.url,
            "site": material.site,
            "title": material.title,
            "date": material.date_hint or "未知",
            "content": material.best_content(),
        }
        for material in materials
    ]


def sources_metadata(materials: list[Material]) -> list[dict]:
    """Project persistable source metadata without page bodies."""
    return [
        {
            "index": material.index,
            "url": material.url,
            "site": material.site,
            "title": material.title,
            "date": material.date_hint,
            "engines": material.engines,
        }
        for material in materials
    ]
