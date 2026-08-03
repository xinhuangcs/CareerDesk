"""OpenAI embedder assembly for semantic conversation retrieval."""

from .providers import provider_spec


CONVERSATION_EMBEDDING_MODEL = "text-embedding-3-small"


def build_openai_embedder():
    """Build a conversation embedder fixed to the official OpenAI destination."""
    from agentmaker import OpenAIEmbedder

    spec = provider_spec("openai")
    if spec is None or spec.base_url != "https://api.openai.com/v1":
        raise RuntimeError("OpenAI embedding endpoint contract changed; refusing outbound call")
    return OpenAIEmbedder(model=CONVERSATION_EMBEDDING_MODEL, base_url=spec.base_url)
