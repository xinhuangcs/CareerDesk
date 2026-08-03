"""Provider-native chain-of-thought suppression for deterministic extraction."""

from types import SimpleNamespace

from careerdesk.platform.ai.client import no_chain_of_thought_body


def test_deepseek_thinking_is_disabled_explicitly():
    body = no_chain_of_thought_body(SimpleNamespace(provider="deepseek"))

    assert body == {"thinking": {"type": "disabled"}}


def test_providers_without_default_thinking_send_nothing():
    for provider in ("anthropic", "openai", "gemini", "ollama", "test"):
        assert no_chain_of_thought_body(SimpleNamespace(provider=provider)) is None


def test_a_client_without_provider_metadata_sends_nothing():
    assert no_chain_of_thought_body(object()) is None
