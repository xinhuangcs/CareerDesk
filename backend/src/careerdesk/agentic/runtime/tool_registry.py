"""CareerDesk ToolRegistry privacy boundary."""

import logging
import re
import traceback

from agentmaker import ToolRegistry, ToolResponse

from ...platform.locale import DEFAULT_OUTPUT_LOCALE, OutputLocale

logger = logging.getLogger(__name__)

_CJK = re.compile(r"[\u3400-\u9fff]")
_ENGLISH_TOOL_DESCRIPTIONS = {
    "load_skill": "Load one bundled CareerDesk workflow by exact skill ID.",
    "record_review": (
        "Prepare confirmation proposals from the system-bound current message when the user narrates "
        "real or newly confirmed job-search progress; call exactly once with no arguments."
    ),
    "parse_jobs": "Parse role or job-description input into an import preview.",
    "update_application": (
        "Atomically apply 1-20 explicit corrections to existing board fields from one updates array, "
        "or prepare one merge preview. Never use it for narrated progress, results, invitations, or appointments."
    ),
    "delete_application": "Prepare a confirmation preview for deleting one or more applications.",
    "manage_review": "Correct or prepare the undo of an existing interview review.",
    "query_timeline": "Read application progress, history, schedules, attention items, or statistics.",
    "query_study": "Read interview questions, knowledge gaps, and study progress.",
    "query_library": "Read the user's résumé and document library metadata.",
    "query_grill": "Read interview-practice statistics, history, sessions, or detailed feedback.",
    "query_prep": "Read company research, role briefing, or résumé-adaptation results.",
    "request_application_prep": "Start explicitly requested company and role research for an existing application.",
    "query_status": "Read recurring or recent self-reported interview-state factors.",
    "preferences": "Read or atomically update explicitly stated long-term job-search preferences.",
    "conversation_search": "Search the user's historical conversations for relevant evidence.",
}


def _english_schema(value, *, tool_name: str | None = None):
    """Remove Chinese-only schema prose while preserving every validation keyword."""
    if isinstance(value, list):
        return [_english_schema(item, tool_name=tool_name) for item in value]
    if not isinstance(value, dict):
        return value
    translated = {}
    for key, item in value.items():
        if key == "description" and isinstance(item, str) and _CJK.search(item):
            if tool_name is not None and tool_name in _ENGLISH_TOOL_DESCRIPTIONS:
                translated[key] = _ENGLISH_TOOL_DESCRIPTIONS[tool_name]
            continue
        translated[key] = _english_schema(item)
    return translated


class CareerDeskToolRegistry(ToolRegistry):
    """Unified privacy boundary for parameter and execution failures.

    AgentMaker may return jsonschema messages containing complete parameters. Preserve
    lookup/schema/confirmation behavior while replacing registered-tool validation and
    uncaught execution failures with fixed redacted text.
    """

    def __init__(
        self,
        *,
        output_locale: OutputLocale = DEFAULT_OUTPUT_LOCALE,
        prompts=None,
    ) -> None:
        super().__init__(prompts=prompts)
        self._output_locale = output_locale

    def _locate(self, name, parameters):
        registered = self.get(name)
        tool, error = super()._locate(name, parameters)
        if registered is not None and error is not None:
            return None, ToolResponse.error(
                "Tool arguments do not match the safety contract. Correct them before retrying."
                if self._output_locale == "en"
                else "工具参数不符合安全契约，请按参数说明修正后再调用。",
            )
        return tool, error

    def _exec_error(self, name: str, error: Exception) -> ToolResponse:
        """Log only exception type and argument-free frame location, never its text."""
        frames = traceback.extract_tb(error.__traceback__)
        location = (
            f"{frames[-1].name}:{frames[-1].lineno}"
            if frames
            else "unknown"
        )
        logger.error(
            "CareerDesk tool '%s' raised during execution: %s at %s",
            name,
            type(error).__name__,
            location,
        )
        return ToolResponse.error(
            (
                "The tool failed internally and no write was confirmed. Query the current state "
                "before deciding whether to retry."
                if self._output_locale == "en"
                else "工具执行发生内部错误，未确认任何写入；请先查询当前状态再决定是否重试。"
            ),
        )

    def get_catalog(self) -> str:
        if self._output_locale != "en":
            return super().get_catalog()
        return "\n".join(
            f"- {tool.name}: {_ENGLISH_TOOL_DESCRIPTIONS.get(tool.name, tool.name.replace('_', ' '))}"
            for tool in self.list_tools()
        )

    def get_tools_description(self, names=None) -> str:
        if self._output_locale != "en":
            return super().get_tools_description(names=names)
        lines = []
        for item in self.to_openai_schema(names=names):
            function = item["function"]
            lines.append(f"- {function['name']}: {function['description']}")
            parameters = function["parameters"]
            required = set(parameters.get("required", []))
            for name, schema in parameters.get("properties", {}).items():
                kind = schema.get("type", "value")
                qualifier = "required" if name in required else "optional"
                lines.append(f"    - {name} ({kind}, {qualifier})")
        return "\n".join(lines)

    def to_openai_schema(self, names=None) -> list[dict]:
        schemas = super().to_openai_schema(names=names)
        if self._output_locale != "en":
            return schemas
        localized = []
        for schema in schemas:
            function = schema.get("function", {})
            name = function.get("name")
            item = _english_schema(schema, tool_name=name if isinstance(name, str) else None)
            if isinstance(name, str):
                item["function"]["description"] = _ENGLISH_TOOL_DESCRIPTIONS.get(
                    name,
                    name.replace("_", " "),
                )
            localized.append(item)
        return localized
