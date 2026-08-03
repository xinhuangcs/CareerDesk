"""Build an LLMClient from the AgentSpec-compatible ``provider:model`` format.

Services receive a duck-typed client instead of reading configuration directly.
Production injects clients built here; tests inject ``ScriptedLLM`` so review and
practice logic runs without cost or network access.
"""

import inspect
import logging
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

from agentmaker import LLMClient, LLMConfigError

from ...core.config import resolve_openai_compatible_endpoint
from .providers import provider_spec, resolve_model_capabilities


logger = logging.getLogger(__name__)


STRICT_OFFLINE_MODEL_MESSAGE = (
    "严格离线已开启，当前云端模型配置会保留但不会被调用。"
    "请改用 Ollama、vLLM 或 SGLang 本地模型，或在「模型与隐私」关闭严格离线。"
)
MODEL_CAPABILITY_MESSAGE = (
    "当前型号缺少可信的上下文窗口或最大输出容量。请在「模型与隐私」填写该型号的"
    " context window 与 max output tokens；不要照搬其他型号的参数。"
)


class OutboundAccessDisabled(RuntimeError):
    """The current instance policy explicitly forbids this outbound operation."""


def model_uses_local_provider(model_string: str | None) -> bool:
    """Trust only registered providers whose base URL is explicitly loopback."""
    if not model_string:
        return False
    provider = model_string.partition(":")[0].strip()
    spec = provider_spec(provider)
    return bool(spec and spec.is_local)


def ensure_model_outbound_allowed(model_string: str, *, strict_offline: bool) -> None:
    """Enforce strict offline policy before constructing any provider client."""
    if strict_offline and not model_uses_local_provider(model_string):
        raise OutboundAccessDisabled(STRICT_OFFLINE_MODEL_MESSAGE)


def _canonical_loopback_base_url(base_url: str | None) -> str:
    """Return a DNS-free loopback URL or fail closed on an unsafe registry entry."""
    if not base_url:
        raise OutboundAccessDisabled(STRICT_OFFLINE_MODEL_MESSAGE)
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as error:
        raise OutboundAccessDisabled(STRICT_OFFLINE_MODEL_MESSAGE) from error
    if (
        parsed.scheme.lower() != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise OutboundAccessDisabled(STRICT_OFFLINE_MODEL_MESSAGE)

    # Never resolve ``localhost`` through DNS or /etc/hosts in strict mode.
    hostname = "127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname
    rendered_host = f"[{hostname}]" if hostname == "::1" else hostname
    netloc = f"{rendered_host}:{port}" if port is not None else rendered_host
    return urlunsplit(("http", netloc, parsed.path, "", ""))


@lru_cache(maxsize=1)
def _strict_loopback_adapter_class():
    """Isolate the pinned agentmaker private adapter seam used by local models.

    AgentMaker does not expose an HTTP-client injection point on
    ``LLMClient``.  Keeping the single private dependency here makes any future
    framework drift fail during local client construction rather than silently
    falling back to the SDK's proxy-aware, redirect-following defaults.
    """
    from agentmaker.core.adapters.openai_compat import OpenAIAdapter

    class StrictLoopbackOpenAIAdapter(OpenAIAdapter):
        def _ensure_client(self):
            def make():
                import httpx
                from openai import AsyncOpenAI

                http_client = httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                )
                try:
                    return AsyncOpenAI(
                        api_key=self.api_key,
                        base_url=self.base_url,
                        timeout=self.timeout,
                        http_client=http_client,
                    )
                except BaseException:
                    # Constructor failures must not leak the client we own.
                    # This factory is always called under a running event loop by
                    # agentmaker's adapter cache, so schedule the async close and
                    # consume any best-effort cleanup exception.
                    import asyncio

                    task = asyncio.get_running_loop().create_task(http_client.aclose())
                    task.add_done_callback(
                        lambda finished: finished.cancelled() or finished.exception(),
                    )
                    raise

            return self._async_client_for_loop(make)

    return OpenAIAdapter, StrictLoopbackOpenAIAdapter


def _build_strict_loopback_llm(
    provider: str,
    model: str | None,
    base_url: str,
    *,
    context_window: int | None,
    max_output_tokens: int | None,
) -> LLMClient:
    """Build a loopback LLM whose SDK cannot use proxies or follow redirects."""
    original_class, strict_class = _strict_loopback_adapter_class()
    llm = LLMClient(
        provider,
        model=model,
        base_url=base_url,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
    )
    adapter = getattr(llm, "_adapter", None)
    if type(adapter) is not original_class:
        raise RuntimeError(
            "agentmaker local adapter contract changed; refusing loopback transport fallback",
        )
    llm._adapter = strict_class(  # noqa: SLF001 -- isolated pinned framework seam
        model=adapter.model,
        api_key=adapter.api_key,
        base_url=base_url,
        timeout=adapter.timeout,
        default_temperature=adapter.default_temperature,
        max_tokens_field=adapter.max_tokens_field,
        structured_output=adapter.structured_output,
        provider=adapter.provider,
    )
    return llm


def _validated_explicit_capabilities(
    context_window: int | None,
    max_output_tokens: int | None,
) -> tuple[int | None, int | None]:
    if (context_window is None) != (max_output_tokens is None):
        raise LLMConfigError(MODEL_CAPABILITY_MESSAGE)
    if context_window is None:
        return None, None
    if (
        type(context_window) is not int
        or type(max_output_tokens) is not int
        or context_window < 1_024
        or max_output_tokens < 256
        or max_output_tokens > context_window
    ):
        raise LLMConfigError(MODEL_CAPABILITY_MESSAGE)
    return context_window, max_output_tokens


def _require_resolved_capabilities(llm: LLMClient) -> LLMClient:
    context_window = getattr(llm, "context_window", None)
    max_output_tokens = getattr(llm, "max_output_tokens", None)
    if (
        type(context_window) is not int
        or type(max_output_tokens) is not int
        or context_window < 1_024
        or max_output_tokens < 256
        or max_output_tokens > context_window
    ):
        raise LLMConfigError(MODEL_CAPABILITY_MESSAGE)
    return llm


def build_llm(
    model_string: str,
    *,
    strict_offline: bool,
    context_window: int | None = None,
    max_output_tokens: int | None = None,
) -> LLMClient:
    """Build a client using the ``provider:model`` configuration convention.

    Parsing matches ``AgentSpec._resolve_llm``: an empty right side falls back to
    the provider default, while a bare model name is interpreted as a provider and
    rejected. Construction performs no request; network use begins at chat/stream.
    """
    ensure_model_outbound_allowed(model_string, strict_offline=strict_offline)
    context_window, max_output_tokens = resolve_model_capabilities(
        model_string,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
    )
    context_window, max_output_tokens = _validated_explicit_capabilities(
        context_window,
        max_output_tokens,
    )
    provider, sep, name = model_string.partition(":")
    spec = provider_spec(provider)
    # A bare provider name must build the model this app pins for it, not the one the
    # SDK would pick, so runtime matches the capacities and metadata resolved above.
    default_model = spec.default_model if spec is not None else None
    if spec is not None and spec.is_local:
        # A local model is itself a disclosed trust boundary. In every mode, pin
        # it to loopback and disable system proxies and redirects.
        return _require_resolved_capabilities(_build_strict_loopback_llm(
            provider,
            name or None,
            _canonical_loopback_base_url(spec.base_url),
            context_window=context_window,
            max_output_tokens=max_output_tokens,
        ))
    if strict_offline:
        # Keep the transport assembly independently fail-closed even if the
        # preflight implementation changes later.
        raise OutboundAccessDisabled(STRICT_OFFLINE_MODEL_MESSAGE)
    if provider == "openai_compatible":
        endpoint = resolve_openai_compatible_endpoint()
        if endpoint.status == "missing":
            raise LLMConfigError(
                "OpenAI-compatible endpoint is not configured.",
            )
        if endpoint.status == "invalid":
            raise LLMConfigError(
                "OpenAI-compatible endpoint configuration is invalid.",
            )
        # Pass the validated URL explicitly so runtime matches settings disclosure
        # and the SDK cannot reinterpret environment variables.
        return _require_resolved_capabilities(LLMClient(
            provider,
            model=name or default_model,
            base_url=endpoint.url,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
        ))
    if spec is not None and spec.base_url is not None:
        # Named built-ins always use the endpoint represented by that name.
        # Only openai_compatible has no fixed URL and may consume the explicitly
        # documented custom OPENAI_BASE_URL / LLM_BASE_URL channel.
        return _require_resolved_capabilities(LLMClient(
            provider,
            model=name or default_model,
            base_url=spec.base_url,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
        ))
    llm = (
        LLMClient(
            provider,
            model=name or default_model,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
        )
        if sep
        else LLMClient(
            model_string,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
        )
    )
    return _require_resolved_capabilities(llm)


async def _invoke_closer(close) -> None:
    """Run either a synchronous or asynchronous SDK closer."""
    result = close()
    if inspect.isawaitable(result):
        await result


async def close_llm_client(llm) -> None:
    """Best-effort close every HTTP client owned by one request-scoped LLM.

    AgentMaker lazily caches one vendor SDK client per event loop but does
    not yet expose a public ``LLMClient.aclose`` method.  CareerDesk deliberately
    keeps that version-specific seam here in the platform adapter: feature
    owners only receive this stable helper and never inspect framework internals.

    A future public closer wins automatically.  On the pinned version, clients
    are detached under the adapter lock before closing, which makes repeated
    calls idempotent and prevents a failed closer from retaining the remaining
    pools.  Cleanup errors are logged after all clients have been attempted;
    they must not turn an already-durable job result into a retryable failure.
    """
    if llm is None:
        return

    for name in ("aclose", "close"):
        public_close = getattr(llm, name, None)
        if callable(public_close):
            try:
                await _invoke_closer(public_close)
            except BaseException:  # noqa: BLE001 -- cleanup must not mask the durable outcome
                logger.exception("failed to close LLM client")
            return

    adapter = getattr(llm, "_adapter", None)
    clients = getattr(adapter, "_async_clients", None)
    lock = getattr(adapter, "_async_clients_lock", None)
    if not isinstance(clients, dict) or lock is None:
        # Scripted/test LLMs and future adapters without owned HTTP pools need
        # no cleanup here.  Framework contract drift remains fail-safe: this
        # helper never guesses at unrelated object attributes.
        return

    try:
        with lock:
            owned_clients = list(clients.values())
            clients.clear()
    except BaseException:  # noqa: BLE001 -- a broken cleanup seam must not alter business state
        logger.exception("failed to detach LLM SDK clients for closing")
        return

    failures: list[BaseException] = []
    for sdk_client in owned_clients:
        # google-genai's async transport lives under ``client.aio`` while its
        # direct ``close`` only releases the unused synchronous transport.
        # Other supported SDKs expose an async direct close instead.
        if getattr(llm, "protocol", None) == "gemini":
            try:
                async_view = getattr(sdk_client, "aio", None)
                async_close = getattr(async_view, "aclose", None)
                if callable(async_close):
                    await _invoke_closer(async_close)
            except BaseException as error:  # noqa: BLE001 -- continue with all remaining pools
                failures.append(error)

        direct_close = getattr(sdk_client, "aclose", None)
        if not callable(direct_close):
            direct_close = getattr(sdk_client, "close", None)
        if callable(direct_close):
            try:
                await _invoke_closer(direct_close)
            except BaseException as error:  # noqa: BLE001 -- continue with all remaining pools
                failures.append(error)

    for error in failures:
        logger.error(
            "failed to close an LLM SDK client",
            exc_info=(type(error), error, error.__traceback__),
        )


class LLMClientOwnership:
    """Async scope that closes a client unless ownership moves to a worker.

    Routes construct cloud dependencies before a durable claim, but the client
    must outlive the HTTP request once a background task has accepted it.  The
    explicit transfer point prevents every early return and unexpected
    repository/scheduling exception from becoming a resource leak.
    """

    def __init__(self, llm) -> None:
        self.llm = llm
        self._transferred = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        if not self._transferred:
            await close_llm_client(self.llm)

    def transfer(self) -> None:
        """Move cleanup responsibility to the successfully scheduled worker."""
        self._transferred = True


def no_chain_of_thought_body(llm) -> dict | None:
    """Return the provider-native request body that suppresses chain-of-thought.

    A call that maps text onto a fixed schema gains nothing from reasoning tokens
    and pays for them in latency. DeepSeek is the only supported provider whose
    models think by default, so every other one needs nothing sent.
    """
    if getattr(llm, "provider", None) != "deepseek":
        return None
    return {"thinking": {"type": "disabled"}}


def supports_image_input(
    model_string: str | None,
    *,
    strict_offline: bool,
) -> tuple[bool, str]:
    """Return image-input capability and a user-facing reason when unavailable.

    This follows ``ProviderProfile.supports_vision``. False requests a model
    change; unknown providers are allowed and may fail through the chat boundary.
    """
    if not model_string:
        return False, "图片理解需要模型，请先到「模型与隐私」完成配置。"
    try:
        ensure_model_outbound_allowed(model_string, strict_offline=strict_offline)
    except OutboundAccessDisabled as error:
        return False, str(error)
    # Capability probing reads registry metadata without constructing a cloud SDK.
    provider = model_string.partition(":")[0].strip()
    spec = provider_spec(provider)
    if spec is not None and spec.supports_vision is False:
        return False, (f"当前模型（{model_string}）不支持图片输入。要传图请在「模型与隐私」换成支持视觉的模型，"
                       "例如 OpenAI 的视觉模型或 Anthropic Claude；"
                       "或者把截图里的内容转成文字/PDF 附件发我（这两类我直接能读）。")
    return True, ""
