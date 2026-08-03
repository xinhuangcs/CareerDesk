
import asyncio
from threading import Lock

import agentmaker
import openai
import pytest
from agentmaker import LLMConfigError
from agentmaker.core.llm_clients import _PROFILES
from agentmaker.testing import FakeEmbedder
from fastapi import BackgroundTasks

from careerdesk.agentic.memory.conversation import build_conversation_memory
from careerdesk.features.research.service import build_search
from careerdesk.core.config import get_settings
from careerdesk.platform.database import init_db, read_connection
from careerdesk.orchestration.assistant.service import run_chat
from careerdesk.platform.ai import client as llm_client
from careerdesk.platform.ai.client import (
    LLMClientOwnership,
    OutboundAccessDisabled,
    build_llm,
    close_llm_client,
)
from careerdesk.platform.ai.providers import (
    ProviderSpec,
    provider_model_capabilities,
    provider_spec,
    resolve_model_capabilities,
)

TEST_MODEL_CAPABILITIES = {
    "context_window": 131_072,
    "max_output_tokens": 8_192,
}


def _forbidden(*_args, **_kwargs):
    raise AssertionError("outbound client must not be constructed")


def _run(coroutine):
    return asyncio.run(coroutine)


def test_strict_offline_rejects_cloud_before_llm_client_construction(monkeypatch):
    monkeypatch.setattr(llm_client, "LLMClient", _forbidden)

    with pytest.raises(OutboundAccessDisabled, match="严格离线"):
        build_llm("openai:gpt-4o-mini", strict_offline=True)
    with pytest.raises(OutboundAccessDisabled, match="严格离线"):
        build_llm("openai_compatible:custom", strict_offline=True)


@pytest.mark.parametrize(
    ("model", "expected_base_url", "strict_offline"),
    [
        ("ollama:qwen3", "http://127.0.0.1:11434/v1", False),
        ("ollama:qwen3", "http://127.0.0.1:11434/v1", True),
        ("vllm:qwen3", "http://127.0.0.1:8000/v1", False),
        ("vllm:qwen3", "http://127.0.0.1:8000/v1", True),
        ("sglang:qwen3", "http://127.0.0.1:30000/v1", False),
        ("sglang:qwen3", "http://127.0.0.1:30000/v1", True),
    ],
)
def test_local_model_always_owns_proxy_free_non_redirecting_transport(
    monkeypatch,
    model,
    expected_base_url,
    strict_offline,
):
    captures = []

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captures.append(kwargs)

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setenv("HTTP_PROXY", "http://198.51.100.10:8888")
    monkeypatch.setenv("ALL_PROXY", "http://198.51.100.11:8888")

    async def construct_client():
        llm = build_llm(
            model,
            strict_offline=strict_offline,
            **TEST_MODEL_CAPABILITIES,
        )
        sdk_client = llm._adapter._ensure_client()
        assert isinstance(sdk_client, FakeAsyncOpenAI)
        assert llm.base_url == expected_base_url
        http_client = captures[0]["http_client"]
        assert http_client.trust_env is False
        assert http_client.follow_redirects is False
        await http_client.aclose()

    _run(construct_client())
    assert captures[0]["base_url"] == expected_base_url


@pytest.mark.parametrize(
    ("model", "key_name", "pinned_base_url"),
    [
        ("openai:gpt-4o-mini", "OPENAI_API_KEY", "https://api.openai.com/v1"),
        ("anthropic:claude-haiku-4-5-20251001", "ANTHROPIC_API_KEY", "https://api.anthropic.com"),
        ("gemini:gemini-3.1-flash-lite", "GEMINI_API_KEY", "https://generativelanguage.googleapis.com/"),
    ],
)
def test_named_cloud_models_ignore_ambient_base_url_overrides(
    monkeypatch,
    model,
    key_name,
    pinned_base_url,
):
    monkeypatch.setenv(key_name, "hermetic-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://collector.invalid/v1")
    monkeypatch.setenv("LLM_BASE_URL", "https://collector.invalid/v1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://collector.invalid")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://collector.invalid")

    llm = build_llm(model, strict_offline=False, **TEST_MODEL_CAPABILITIES)

    assert llm.base_url == pinned_base_url


def test_explicit_compatible_provider_keeps_custom_endpoint(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "custom-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "https://explicit-gateway.example/v1")

    llm = build_llm(
        "openai_compatible:custom-model",
        strict_offline=False,
        **TEST_MODEL_CAPABILITIES,
    )

    assert llm.base_url == "https://explicit-gateway.example/v1"


def test_a_bare_provider_builds_the_model_this_app_advertises(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")

    for provider in ("openai", "anthropic", "deepseek"):
        spec = provider_spec(provider)
        built = build_llm(provider, strict_offline=False)
        assert built.model == spec.default_model, provider
        # Capacities must describe the model actually built, never another model's.
        assert (built.context_window, built.max_output_tokens) == (
            spec.context_window, spec.max_output_tokens,
        ), provider
        assert provider_model_capabilities(provider) == (
            built.context_window, built.max_output_tokens,
        ), provider


def test_openai_default_is_pinned_away_from_the_sdk_cheapest_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    built = build_llm("openai", strict_offline=False)

    assert built.model == "gpt-5.4-mini"
    assert built.model != _PROFILES["openai"].default_model
    # An explicit model still wins over the pin.
    assert build_llm(
        "openai:gpt-4.1-nano", strict_offline=False,
    ).model == "gpt-4.1-nano"


def test_switched_model_requires_explicit_capacity_and_default_model_uses_profile(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")

    with pytest.raises(LLMConfigError, match="context window"):
        build_llm("openai:gpt-4o-mini", strict_offline=False)

    configured = build_llm(
        "openai:gpt-4o-mini",
        strict_offline=False,
        **TEST_MODEL_CAPABILITIES,
    )
    assert configured.context_window == TEST_MODEL_CAPABILITIES["context_window"]
    assert configured.max_output_tokens == TEST_MODEL_CAPABILITIES["max_output_tokens"]

    provider_default = build_llm("anthropic", strict_offline=False)
    assert provider_default.context_window > 0
    assert provider_default.max_output_tokens > 0
    explicit_default = build_llm(
        f"anthropic:{provider_default.model}",
        strict_offline=False,
    )
    assert explicit_default.context_window == provider_default.context_window
    assert explicit_default.max_output_tokens == provider_default.max_output_tokens
    assert provider_model_capabilities("anthropic:") == (
        provider_default.context_window,
        provider_default.max_output_tokens,
    )


@pytest.mark.parametrize(
    "model",
    [
        "openai",
        "deepseek",
        "dashscope",
        "moonshot",
        "zhipu",
        "gemini_openai",
        "anthropic",
        "gemini",
    ],
)
def test_capacity_resolver_covers_every_supported_provider_default(model):
    context_window, max_output_tokens = resolve_model_capabilities(
        model,
        context_window=None,
        max_output_tokens=None,
    )

    assert type(context_window) is int and context_window >= 1_024
    assert type(max_output_tokens) is int and 256 <= max_output_tokens <= context_window


@pytest.mark.parametrize(
    "model",
    [
        "openai:gpt-4o-mini",
        "modelscope:qwen3",
        "ollama:qwen3",
        "vllm:qwen3",
        "sglang:qwen3",
        "openai_compatible:custom",
    ],
)
def test_capacity_resolver_never_borrows_defaults_for_custom_models(model):
    assert resolve_model_capabilities(
        model,
        context_window=None,
        max_output_tokens=None,
    ) == (None, None)
    assert resolve_model_capabilities(
        model,
        context_window=131_072,
        max_output_tokens=8_192,
    ) == (131_072, 8_192)


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        (None, "not configured"),
        ("https://user:secret@private.example/v1", "invalid"),
    ],
)
def test_compatible_endpoint_configuration_fails_as_redactable_llm_error(
    monkeypatch,
    endpoint,
    message,
):
    monkeypatch.setenv("LLM_API_KEY", "custom-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    if endpoint is not None:
        monkeypatch.setenv("OPENAI_BASE_URL", endpoint)

    with pytest.raises(LLMConfigError, match=message) as captured:
        build_llm("openai_compatible:custom-model", strict_offline=False)

    assert "secret" not in str(captured.value)
    assert "private.example" not in str(captured.value)


def test_request_scoped_llm_closes_all_sdk_pools_without_waiting_for_gc(caplog):
    open_clients = 50
    closed = []

    class SDKClient:
        def __init__(self, number, *, fails=False):
            self.number = number
            self.fails = fails

        async def close(self):
            nonlocal open_clients
            open_clients -= 1
            closed.append(self.number)
            if self.fails:
                raise RuntimeError("close canary")

    clients = {
        number: SDKClient(number, fails=number == 7)
        for number in range(open_clients)
    }
    adapter = type("Adapter", (), {
        "_async_clients": clients,
        "_async_clients_lock": Lock(),
    })()
    llm = type("RequestLLM", (), {"_adapter": adapter, "protocol": "openai"})()

    _run(close_llm_client(llm))

    assert open_clients == 0
    assert sorted(closed) == list(range(50))
    assert adapter._async_clients == {}
    assert "close canary" in caplog.text
    _run(close_llm_client(llm))
    assert len(closed) == 50


def test_llm_ownership_closes_on_exception_but_transfers_to_background_worker():
    class PublicLLM:
        def __init__(self):
            self.close_calls = 0

        async def aclose(self):
            self.close_calls += 1

    async def exercise():
        abandoned = PublicLLM()
        with pytest.raises(RuntimeError, match="claim failed"):
            async with LLMClientOwnership(abandoned):
                raise RuntimeError("claim failed")

        transferred = PublicLLM()
        async with LLMClientOwnership(transferred) as ownership:
            ownership.transfer()
        return abandoned, transferred

    abandoned, transferred = _run(exercise())
    assert abandoned.close_calls == 1
    assert transferred.close_calls == 0



def test_prep_repository_exception_after_preflight_closes_untransferred_llm(
    tmp_path,
    monkeypatch,
):
    from types import SimpleNamespace

    from careerdesk.orchestration.application_prep import api as prep_api
    from careerdesk.orchestration.application_prep import commands as prep_commands
    from careerdesk.orchestration.application_prep import factory as prep_factory

    class OwnedLLM:
        close_calls = 0

        async def aclose(self):
            self.close_calls += 1

    llm = OwnedLLM()
    settings = SimpleNamespace(
        db_path=str(tmp_path / "careerdesk.db"),
        llm_model="openai:test",
        strict_offline=False,
        web_research_enabled=False,
    )
    monkeypatch.setattr(prep_api, "get_settings", lambda: settings)
    monkeypatch.setattr(
        prep_commands.applications,
        "application_detail",
        lambda *_args: {"prep_status": "none", "prep_retry_after_seconds": None},
    )
    monkeypatch.setattr(prep_factory, "build_prep_llm", lambda _settings: llm)
    monkeypatch.setattr(
        prep_commands.applications,
        "claim_prep_generation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("claim failed")),
    )

    with pytest.raises(RuntimeError, match="claim failed"):
        _run(prep_api.trigger_prep(
            1,
            BackgroundTasks(),
            user_id="u1",
        ))

    assert llm.close_calls == 1


def test_explicit_compatible_provider_uses_documented_primary_endpoint(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "custom-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "HTTPS://Primary.Example/v1/team")
    monkeypatch.setenv("LLM_BASE_URL", "https://ignored.example/v1")

    llm = build_llm(
        "openai_compatible:custom-model",
        strict_offline=False,
        **TEST_MODEL_CAPABILITIES,
    )

    assert llm.base_url == "https://primary.example/v1/team"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:secret@gateway.example/v1",
        "https://gateway.example/v1?token=secret",
        "https://gateway.example/v1#secret",
    ],
)
def test_explicit_compatible_provider_rejects_unsafe_endpoint(monkeypatch, endpoint):
    monkeypatch.setenv("LLM_API_KEY", "custom-key")
    monkeypatch.setenv("OPENAI_BASE_URL", endpoint)
    monkeypatch.setenv("LLM_BASE_URL", "https://ignored.example/v1")

    with pytest.raises(agentmaker.LLMConfigError) as caught:
        build_llm("openai_compatible:custom-model", strict_offline=False)

    assert endpoint not in str(caught.value)
    assert "secret" not in str(caught.value)


def _provider(base_url: str) -> ProviderSpec:
    return ProviderSpec(
        name="test",
        default_model="model",
        key_envs=(),
        base_url=base_url,
        supports_vision=None,
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:8000/v1",
        "http://[::1]:30000/v1",
    ],
)
def test_provider_local_detection_accepts_exact_http_loopback(base_url):
    assert _provider(base_url).is_local is True


@pytest.mark.parametrize(
    "base_url",
    [
        "https://localhost:11434/v1",
        "http://localhost.evil.example/v1",
        "http://127.0.0.1.evil.example/v1",
        "http://localhost@evil.example/v1",
        "http://user:pass@localhost:11434/v1",
        "http://localhost:11434/v1?relay=remote",
        "http://localhost:11434/v1#remote",
        "http://localhost:not-a-port/v1",
        "http://[::1%25en0]:30000/v1",
    ],
)
def test_provider_local_detection_rejects_ambiguous_or_remote_urls(base_url):
    assert _provider(base_url).is_local is False


def test_loopback_url_canonicalization_uses_ip_literals_without_dns():
    assert llm_client._canonical_loopback_base_url(
        "http://localhost:11434/v1",
    ) == "http://127.0.0.1:11434/v1"
    assert llm_client._canonical_loopback_base_url(
        "http://[::1]:30000/v1",
    ) == "http://[::1]:30000/v1"


def test_strict_local_adapter_contract_drift_fails_closed(monkeypatch):
    class DriftedClient:
        _adapter = object()

    monkeypatch.setattr(llm_client, "LLMClient", lambda *_args, **_kwargs: DriftedClient())

    with pytest.raises(RuntimeError, match="adapter contract changed"):
        build_llm(
            "ollama:qwen3",
            strict_offline=True,
            **TEST_MODEL_CAPABILITIES,
        )











def test_conversation_embedding_key_alone_keeps_local_fts_search(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "credential-without-consent")
    monkeypatch.setattr(agentmaker, "OpenAIEmbedder", _forbidden)
    closers = []

    store, conversation = build_conversation_memory(
        str(tmp_path / "conversation.db"),
        embedding_enabled=False,
        resource_closers=closers,
    )
    try:
        assert store is conversation and conversation is not None
    finally:
        for close in reversed(closers):
            close()


def test_conversation_embedding_requires_both_consent_and_key(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "hermetic-key")
    monkeypatch.setattr(agentmaker, "OpenAIEmbedder", lambda **_kwargs: FakeEmbedder())
    closers = []

    store, conversation = build_conversation_memory(
        str(tmp_path / "conversation.db"),
        embedding_enabled=True,
        resource_closers=closers,
    )
    try:
        assert store is conversation and conversation is not None
    finally:
        for close in reversed(closers):
            close()


@pytest.mark.parametrize("builder", ["conversation"])
def test_openai_embeddings_ignore_ambient_base_url_override(
    tmp_path,
    monkeypatch,
    builder,
):
    monkeypatch.setenv("OPENAI_API_KEY", "hermetic-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://collector.invalid/v1")

    captured = []

    def fake_embedder(**kwargs):
        captured.append(kwargs)
        return FakeEmbedder()

    monkeypatch.setattr(agentmaker, "OpenAIEmbedder", fake_embedder)
    closers = []
    _store, conversation = build_conversation_memory(
        str(tmp_path / "conversation.db"),
        embedding_enabled=True,
        resource_closers=closers,
    )
    assert conversation is not None
    for close in reversed(closers):
        close()

    assert captured == [{
        "model": "text-embedding-3-small",
        "base_url": "https://api.openai.com/v1",
    }]


def test_web_research_disabled_does_not_construct_search_tool(monkeypatch):
    monkeypatch.setattr(agentmaker, "SearchTool", _forbidden)

    assert build_search(enabled=False) is None


def test_web_research_outlets_are_the_declared_allowlist(monkeypatch):
    import serpapi

    monkeypatch.setattr(serpapi, "Client", _forbidden)
    for name in ("TAVILY_API_KEY", "BRAVE_API_KEY", "GOOGLE_PSE_API_KEY",
                 "GOOGLE_PSE_ENGINE_ID", "SEARXNG_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SERPAPI_API_KEY", "must-not-be-used")

    assert build_search(enabled=False) is None

    fallback_only = build_search(enabled=True)
    assert fallback_only.describe() == ["DuckDuckGo（兜底）"]

    no_outlets = build_search(enabled=True, ddg_fallback=False)
    assert no_outlets is not None and not no_outlets.has_outlets

    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.setenv("BRAVE_API_KEY", "brave-key")
    monkeypatch.setenv("GOOGLE_PSE_API_KEY", "google-key")
    monkeypatch.setenv("GOOGLE_PSE_ENGINE_ID", "engine-id")
    monkeypatch.setenv("SEARXNG_BASE_URL", "https://searx.internal.example")
    pool = build_search(enabled=True)
    assert pool.describe() == [
        "Tavily", "Brave", "Google", "SearXNG", "DuckDuckGo（兜底）",
    ]


def test_ddg_fallback_stays_locked_to_duckduckgo_backend(monkeypatch):
    import asyncio

    import ddgs as ddgs_module

    from careerdesk.features.research.providers import DdgsProvider
    from careerdesk.features.research.queries import PlannedQuery

    captured = {}

    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def _get_engines(category, backend):
            assert (category, backend) == ("text", "duckduckgo")

            class Engine:
                name = "duckduckgo"

            return [Engine()]

        @staticmethod
        def text(query, **kwargs):
            captured.update(query=query, **kwargs)
            return [{"title": "one", "body": "two", "href": "https://example.com"}]

    monkeypatch.setattr(ddgs_module, "DDGS", FakeDDGS)
    provider = DdgsProvider()
    hits = asyncio.run(provider.search(
        None, PlannedQuery(text="fixed route", leg="company", section="business")))

    assert captured == {"query": "fixed route", "max_results": 8, "backend": "duckduckgo"}
    assert hits[0].url == "https://example.com" and hits[0].engine == "DuckDuckGo"



def test_strict_offline_rejects_assistant_before_turn_claim_or_llm_client(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "openai:gpt-4o-mini")
    monkeypatch.setenv("APP_STRICT_OFFLINE", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "configured-but-dormant")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    monkeypatch.setattr(llm_client, "LLMClient", _forbidden)
    turn_id = "78cf43b6-f1a9-4a5e-b4b3-85a925e90599"

    async def collect():
        return [event async for event in run_chat(
            "帮我检查进度",
            "strict-offline-session",
            "u1",
            client_turn_id=turn_id,
        )]

    try:
        events = _run(collect())
        assert [(event.event, event.data["code"]) for event in events] == [
            ("error", "strict_offline"),
        ]
        with read_connection(settings.db_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM assistant_turns WHERE client_turn_id = ?",
                (turn_id,),
            ).fetchone()[0] == 0
    finally:
        get_settings.cache_clear()
