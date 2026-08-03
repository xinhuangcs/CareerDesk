"""Shared test doubles with explicit, production-like capability metadata."""

from agentmaker.testing import ScriptedLLM as AgentmakerScriptedLLM

TRUSTED_TEST_CONTEXT_WINDOW = 1_000_000


class ScriptedLLM(AgentmakerScriptedLLM):
    """Agentmaker's deterministic fake with a known window by default.

    Production clients receive capabilities from provider profiles or explicit
    settings. Tests that exercise an intentionally unknown model must pass
    ``context_window=None`` explicitly instead of relying on an omitted value.
    """

    def __init__(
        self,
        script=None,
        *,
        model: str = "test",
        provider: str = "test",
        supports_function_calling: bool = True,
        context_window: int | None = TRUSTED_TEST_CONTEXT_WINDOW,
    ) -> None:
        super().__init__(
            script,
            model=model,
            provider=provider,
            supports_function_calling=supports_function_calling,
            context_window=context_window,
        )


__all__ = ["ScriptedLLM", "TRUSTED_TEST_CONTEXT_WINDOW"]
