
import asyncio

from careerdesk.features.research.fetcher import (
    _host_is_public,
    _url_allowed,
    extract_page_text,
)
from careerdesk.features.research.materials import (
    FetchedPage,
    bucket_leg_materials,
    build_materials,
    canonicalize_url,
    materials_payload,
    select_fetch_urls,
    sources_metadata,
)
from careerdesk.features.research.providers import (
    ProviderPool,
    QueryOutcome,
    SearchHit,
    SearchProvider,
    SearchProviderError,
)
from careerdesk.features.research.queries import (
    PlannedQuery,
    gap_fill_queries,
    normalize_planned_queries,
    skeleton_plan,
)


def run(coro):
    return asyncio.run(coro)


class FakeProvider(SearchProvider):

    supports_freshness = True

    def __init__(self, name, *, chinese_strength=1, fail=False, freshness=True):
        super().__init__()
        self.name = name
        self.chinese_strength = chinese_strength
        self.supports_freshness = freshness
        self.fail = fail
        self.queries: list[str] = []

    async def _run(self, client, query):
        self.queries.append(query.text)
        if self.fail:
            raise RuntimeError("boom with secret key sk-123")
        return [SearchHit(title=f"{self.name} 命中", url=f"https://example.com/{self.name}",
                          snippet=f"{query.text} 的材料", engine=self.name)]


def test_skeleton_plan_covers_both_legs_and_gap_fill_uses_fallback_terms():
    plan = skeleton_plan("字节跳动", "后端开发")
    assert len(plan.company_queries()) == 4
    assert len(plan.position_queries()) == 6
    assert all(query.key for query in plan.queries)
    news = [query for query in plan.queries if query.kind == "news"]
    assert len(news) == 1 and news[0].section == "recent_news"

    gaps = gap_fill_queries("字节跳动", "后端开发",
                            missing_company_sections=["culture"],
                            missing_position_sections=["experience_highlights"])
    assert [query.section for query in gaps] == ["culture", "experience_highlights"]
    assert all(not query.key for query in gaps)
    assert "员工 评价" in gaps[0].text and "后端开发" in gaps[1].text


def test_normalize_planned_queries_bounds_and_falls_back():
    raw = [PlannedQuery(text=f"字节跳动 查询 {index}", leg="position",
                        section="experience_highlights", key=True)
           for index in range(30)]
    plan = normalize_planned_queries(raw, company="字节跳动", position="后端")
    assert len(plan.queries) == 18
    assert sum(1 for query in plan.queries if query.key) == 6
    assert normalize_planned_queries([], company="A", position="B").planner == "skeleton"


def test_pool_key_query_uses_two_outlets_and_tail_uses_one():
    first = FakeProvider("Tavily", chinese_strength=2)
    second = FakeProvider("Brave")
    pool = ProviderPool([first, second], fallback=None)
    key_query = PlannedQuery(text="公司 面经", leg="company", section="interview_style", key=True)
    tail_query = PlannedQuery(text="公司 业务", leg="company", section="business")
    outcomes = run(pool.run_plan([key_query, tail_query]))
    assert len(outcomes[0].hits) == 2
    assert len(outcomes[1].hits) == 1
    assert first.queries.count("公司 面经") == 1
    assert second.queries.count("公司 面经") == 1


def test_pool_redistributes_to_backup_on_failure_and_sanitizes_error():
    broken = FakeProvider("Tavily", fail=True, chinese_strength=2)
    backup = FakeProvider("Brave")
    pool = ProviderPool([broken, backup], fallback=None)
    query = PlannedQuery(text="公司 近况", leg="company", section="recent_news", kind="news")
    outcome = run(pool.run_plan([query]))[0]
    assert [hit.engine for hit in outcome.hits] == ["Brave"]
    assert outcome.failed_engines == ["Tavily"]


def test_pool_falls_back_to_ddg_only_when_officials_are_empty_or_all_fail():
    fallback = FakeProvider("DuckDuckGo")
    pool = ProviderPool([], fallback=fallback)
    query = PlannedQuery(text="公司 业务", leg="company", section="business", key=True)
    outcome = run(pool.run_plan([query]))[0]
    assert [hit.engine for hit in outcome.hits] == ["DuckDuckGo"]

    healthy = FakeProvider("Tavily")
    unused_fallback = FakeProvider("DuckDuckGo")
    pool = ProviderPool([healthy], fallback=unused_fallback)
    run(pool.run_plan([query]))
    assert unused_fallback.queries == []


def test_pool_deep_mode_broadcasts_every_query():
    providers = [FakeProvider("Tavily"), FakeProvider("Brave"), FakeProvider("Google")]
    pool = ProviderPool(providers, fallback=None, deep=True)
    tail_query = PlannedQuery(text="公司 业务", leg="company", section="business")
    outcome = run(pool.run_plan([tail_query]))[0]
    assert len(outcome.hits) == 3


def test_provider_error_does_not_leak_upstream_details():
    broken = FakeProvider("Tavily", fail=True)

    async def attempt():
        try:
            await broken.search(None, PlannedQuery(text="q", leg="company", section="business"))
        except SearchProviderError as error:
            return str(error)
        raise AssertionError("expected SearchProviderError")

    message = run(attempt())
    assert "sk-123" not in message and "Tavily" in message


def test_fetch_url_and_host_guards_reject_private_targets():
    assert _url_allowed("https://example.com/a")
    assert not _url_allowed("ftp://example.com/a")
    assert not _url_allowed("file:///etc/passwd")
    assert not _host_is_public("localhost")
    assert not _host_is_public("127.0.0.1")
    assert not _host_is_public("10.0.0.8")
    assert not _host_is_public("169.254.1.1")
    assert not _host_is_public("::1")
    assert _host_is_public("1.1.1.1")


def test_extract_page_text_strips_scripts_and_reads_date():
    html = (
        "<html><head><title> 页面 标题 </title>"
        "<meta property=\"article:published_time\" content=\"2026-03-01T08:00:00Z\">"
        "</head><body><nav>导航</nav><script>alert(1)</script>"
        "<h1>正文标题</h1><p>第一段内容。</p><p>第二段内容。</p></body></html>"
    )
    text, title, date_hint = extract_page_text(html)
    assert "alert" not in text and "第一段内容。" in text
    assert title == "页面 标题"
    assert date_hint.startswith("2026-03-01")


def _outcome(query: PlannedQuery, hits: list[SearchHit]) -> QueryOutcome:
    return QueryOutcome(query=query, hits=hits)


def test_materials_dedup_merges_reposts_and_tracking_urls():
    company_query = PlannedQuery(text="q1", leg="company", section="business", key=True)
    position_query = PlannedQuery(text="q2", leg="position", section="experience_highlights")
    duplicate_url_hits = [
        SearchHit(title="A", url="https://blog.example.com/post?utm_source=x", snippet="片段一",
                  engine="Tavily", raw_content="同一篇正文内容 " * 30),
        SearchHit(title="A", url="https://blog.example.com/post/", snippet="片段二", engine="Brave"),
    ]
    repost_hit = [SearchHit(title="A 转载", url="https://mirror.example.net/copy", snippet="片段三",
                            engine="Brave", raw_content="同一篇正文内容 " * 30)]
    materials = build_materials(
        [_outcome(company_query, duplicate_url_hits), _outcome(position_query, repost_hit)],
        pages={},
    )
    assert len(materials) == 1
    merged = materials[0]
    assert merged.legs == {"company", "position"}
    assert set(merged.engines) == {"Tavily", "Brave"}
    assert canonicalize_url("https://blog.example.com/post?utm_source=x") == merged.canonical_url


def test_bucket_assigns_indexes_and_respects_budget():
    query = PlannedQuery(text="q", leg="company", section="business")
    hits = [SearchHit(title=f"标题{index}", url=f"https://site{index}.com/a",
                      snippet="摘要" * 10, engine="Tavily", raw_content=f"正文{index} " * 500)
            for index in range(20)]
    materials = build_materials([_outcome(query, hits)], pages={})
    selected, dropped = bucket_leg_materials(materials, leg="company", anchor_domain=None,
                                             today="2026-07-18")
    assert selected and dropped > 0
    assert [material.index for material in selected] == list(range(1, len(selected) + 1))
    payload = materials_payload(selected)
    assert payload[0]["source_index"] == 1 and "content" in payload[0]
    metadata = sources_metadata(selected)
    assert metadata[0]["url"].startswith("https://") and "content" not in metadata[0]


def test_bucket_prefers_anchor_domain_and_fresh_materials():
    query = PlannedQuery(text="q", leg="company", section="business")
    hits = [
        SearchHit(title="官网", url="https://corp.example.com/about", snippet="官网介绍",
                  engine="Tavily"),
        SearchHit(title="旧闻", url="https://random.blog/old", snippet="三年前的报道",
                  engine="Tavily"),
    ]
    materials = build_materials([_outcome(query, hits)], pages={
        "https://corp.example.com/about": FetchedPage(
            url="https://corp.example.com/about", title="官网", text="官网正文",
            date_hint="2026-05"),
        "https://random.blog/old": FetchedPage(
            url="https://random.blog/old", title="旧闻", text="旧正文", date_hint="2022-01"),
    })
    selected, _dropped = bucket_leg_materials(materials, leg="company",
                                              anchor_domain="corp.example.com",
                                              today="2026-07-18")
    assert selected[0].site == "corp.example.com"


def test_bucketing_one_leg_does_not_renumber_the_other_legs_sources():
    company_query = PlannedQuery(text="q1", leg="company", section="business", key=True)
    position_query = PlannedQuery(text="q2", leg="position", section="experience_highlights")
    shared_hit = SearchHit(title="共享", url="https://shared.example.com/a", snippet="s",
                           engine="Tavily", raw_content="共享正文 " * 50)
    company_only = SearchHit(title="仅公司", url="https://corp.example.com/b", snippet="s",
                             engine="Tavily", raw_content="公司正文 " * 80)
    materials = build_materials([
        _outcome(company_query, [company_only, shared_hit]),
        _outcome(position_query, [shared_hit]),
    ], pages={})

    company_selected, _ = bucket_leg_materials(materials, leg="company",
                                               anchor_domain=None, today="2026-07-18")
    company_indexes = {material.url: material.index for material in company_selected}
    position_selected, _ = bucket_leg_materials(materials, leg="position",
                                                anchor_domain=None, today="2026-07-18")

    assert {material.url: material.index for material in company_selected} == company_indexes
    assert position_selected[0].url == "https://shared.example.com/a"
    assert position_selected[0].index == 1
    assert sources_metadata(company_selected)[0]["index"] == 1


def test_select_fetch_urls_skips_raw_content_and_ranks_key_queries_first():
    key_query = PlannedQuery(text="q1", leg="company", section="business", key=True)
    tail_query = PlannedQuery(text="q2", leg="company", section="culture")
    outcomes = [
        _outcome(tail_query, [SearchHit(title="尾", url="https://tail.example.com/a",
                                        snippet="s", engine="Brave")]),
        _outcome(key_query, [
            SearchHit(title="有正文", url="https://done.example.com/a", snippet="s",
                      engine="Tavily", raw_content="已带正文"),
            SearchHit(title="关键", url="https://nowcoder.com/discuss/1", snippet="s",
                      engine="Tavily"),
        ]),
    ]
    urls = select_fetch_urls(outcomes, limit=2)
    assert urls[0] == "https://nowcoder.com/discuss/1"
    assert "https://done.example.com/a" not in urls
