"""Runtime budgets and history compression policy for the career assistant."""

from functools import lru_cache

from agentmaker import RunPolicy

from ....platform.locale import DEFAULT_OUTPUT_LOCALE, OutputLocale


CHAT_RUN_POLICY = RunPolicy(max_tool_calls=24, max_tokens=150_000, deadline_seconds=180)

# Reserve space for the user turn, tool schemas/results, and output.
COMPACT_WINDOW_RATIO = 0.6


@lru_cache(maxsize=2)
def _compactor_prompts(output_locale: OutputLocale):
    if output_locale == "en":
        from agentmaker.prompts import DEFAULT_PROMPTS

        return DEFAULT_PROMPTS

    from agentmaker.prompts.packs import chinese_registry

    return chinese_registry()


def build_history_compactor(
    llm,
    output_locale: OutputLocale = DEFAULT_OUTPUT_LOCALE,
):
    """Scale compression thresholds to the verified model context window."""
    from agentmaker import HistoryCompactor

    window = getattr(llm, "context_window", None)
    if window is None:
        # Framework test doubles may intentionally omit provider metadata.  A
        # production client never reaches this branch because build_llm rejects
        # unknown capacity before the Agent is constructed.  Disabling optional
        # compaction is honest; inventing a trigger would not be.
        return None
    if type(window) is not int or window < 1_024:
        raise ValueError("模型上下文容量缺失或无效")
    trigger = int(window * COMPACT_WINDOW_RATIO)
    return HistoryCompactor(
        llm,
        trigger_tokens=trigger,
        prompts=_compactor_prompts(output_locale),
    )
