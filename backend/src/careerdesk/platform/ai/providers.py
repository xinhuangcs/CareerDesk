"""Model provider registry adapter.

Agentmaker lacks a public enumeration API, so its sole private-registry dependency is
encapsulated here. This module exposes technical metadata only; callers own product policy.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit

from agentmaker.core.llm_clients import _KNOWN_MODELS, _PROFILES


# Built-ins must match disclosed destinations and ignore stale SDK base-URL overrides.
# OpenAI-compatible is the only intentional custom channel and is handled separately.
_PINNED_NATIVE_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com/",
}
# Agentmaker defaults every provider to its cheapest model. OpenAI's cheapest cannot
# satisfy this app's extraction schemas, so the app pins its own default rather than
# offering a provider name that is known to fail. Capacities come from the same registry
# so a pinned model never inherits another model's window.
_PINNED_DEFAULT_MODELS = {
    "openai": "gpt-5.4-mini",
}


def _pinned_default(name: str) -> tuple[str, int, int] | None:
    """Resolve a pinned default model with its registered capacities."""
    model = _PINNED_DEFAULT_MODELS.get(name)
    if model is None:
        return None
    info = _KNOWN_MODELS.get(model)
    if info is None or info.context_window is None or info.max_output_tokens is None:
        raise RuntimeError(
            f"Pinned default model {model!r} has no registered capacities",
        )
    return model, info.context_window, info.max_output_tokens


# Fail at import rather than shipping a pin the registry cannot describe.
for _pinned_provider in _PINNED_DEFAULT_MODELS:
    _pinned_default(_pinned_provider)


def _provider_base_url(name: str, profile) -> str | None:
    return _PINNED_NATIVE_BASE_URLS.get(name, profile.base_url)


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Stable read-only provider metadata for upper layers."""

    name: str
    default_model: str | None
    key_envs: tuple[str, ...]
    base_url: str | None
    supports_vision: bool | None
    context_window: int | None = None
    max_output_tokens: int | None = None

    @property
    def is_local(self) -> bool:
        """Whether the registered endpoint is an unambiguous HTTP loopback URL.

        Substring checks are not a security boundary: hosts such as
        ``localhost.evil.example`` contain the same text, and userinfo can make a
        URL look local while its actual destination is remote.  Strict-offline
        callers therefore accept only the three canonical loopback hostnames and
        reject credentials, query strings, fragments, and malformed ports.
        """
        if not self.base_url:
            return False
        try:
            parsed = urlsplit(self.base_url)
            # Accessing .port is part of validation: urllib raises for malformed
            # or out-of-range values instead of returning an ambiguous endpoint.
            parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme.lower() == "http"
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )


def _spec(name: str, profile) -> ProviderSpec:
    pinned = _pinned_default(name)
    default_model, context_window, max_output_tokens = (
        pinned if pinned is not None
        else (profile.default_model, profile.context_window, profile.max_output_tokens)
    )
    return ProviderSpec(
        name=name,
        default_model=default_model,
        key_envs=tuple(profile.key_envs),
        base_url=_provider_base_url(name, profile),
        supports_vision=profile.supports_vision,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
    )


def provider_specs() -> tuple[ProviderSpec, ...]:
    """Return technical metadata for providers currently registered by Agentmaker."""
    return tuple(_spec(name, profile) for name, profile in _PROFILES.items())


def provider_spec(name: str) -> ProviderSpec | None:
    """Look up a provider by registry name, returning None when unknown."""
    profile = _PROFILES.get(name)
    return None if profile is None else _spec(name, profile)


def provider_model_capabilities(model_string: str | None) -> tuple[int | None, int | None]:
    """Return trusted capacities only when the selected model matches a profile default.

    Agentmaker deliberately treats switched and self-hosted models as unknown:
    provider-level defaults do not describe an arbitrary model behind the same
    protocol.  Keep that exact rule in the metadata-only path used by Settings,
    without constructing a vendor SDK or making a discovery request.
    """
    if not model_string:
        return None, None
    provider, separator, requested_model = model_string.partition(":")
    spec = provider_spec(provider.strip())
    if spec is None:
        return None, None
    explicit_model = requested_model.strip() if separator else ""
    model = explicit_model or spec.default_model
    if not model or model != spec.default_model:
        return None, None
    return spec.context_window, spec.max_output_tokens


def resolve_model_capabilities(
    model_string: str | None,
    *,
    context_window: int | None,
    max_output_tokens: int | None,
) -> tuple[int | None, int | None]:
    """Resolve model-bound overrides before an exact provider-default profile.

    Explicit values describe the selected model and therefore always win. A
    provider profile is safe only for that provider's exact default model;
    switched, self-hosted and compatible models deliberately remain unresolved
    until the user supplies both limits.
    """
    if context_window is not None or max_output_tokens is not None:
        return context_window, max_output_tokens
    return provider_model_capabilities(model_string)


def validate_model_reference(model_string: str) -> None:
    """Validate provider/model metadata without constructing a client."""
    provider, _, model = model_string.partition(":")
    name = provider.strip()
    spec = provider_spec(name)
    if spec is None:
        raise ValueError(f"Unknown LLM provider: {name or '<empty>'}")
    if not model.strip() and spec.default_model is None:
        raise ValueError(f"Provider {name} requires an explicit model name")
