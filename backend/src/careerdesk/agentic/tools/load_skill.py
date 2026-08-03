"""Progressive skill disclosure by trusted catalog ID only."""

from agentmaker import Tool, ToolParameter, ToolResponse

from ..runtime import TrustedSkillCatalog
from ...platform.locale import DEFAULT_OUTPUT_LOCALE, OutputLocale


class LoadSkillTool(Tool):
    """Load one bundled skill on demand without external I/O or side effects."""

    supports_parallel = True

    def __init__(
        self,
        catalog: TrustedSkillCatalog,
        *,
        output_locale: OutputLocale = DEFAULT_OUTPUT_LOCALE,
    ):
        self._output_locale = output_locale
        super().__init__(
            "load_skill",
            (
                "Load a bundled CareerDesk workflow by exact skill ID. Use it for complex or "
                "multi-step work; call the relevant business tool directly for a simple action. "
                "This tool never performs a business write."
                if output_locale == "en"
                else "按技能 ID 加载 CareerDesk 内置的可信工作方法。复杂、多步或容易混淆的任务可先调用；"
                "简单明确的单步请求可直接调用对应业务工具。它不执行任何业务写入。"
            ),
            origin="careerdesk",
        )
        self._catalog = catalog

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                "name",
                "string",
                "Exact skill ID" if self._output_locale == "en" else "要加载的技能 ID",
                schema={
                    "type": "string",
                    "enum": list(self._catalog.names),
                    "description": (
                        "Use an exact ID from the skill catalogue, never a file path."
                        if self._output_locale == "en"
                        else "必须是技能目录中的精确 ID，不能传文件路径"
                    ),
                },
            ),
        ]

    def run(self, parameters: dict) -> ToolResponse:
        name = str(parameters.get("name") or "").strip()
        body = self._catalog.load(name, self._output_locale)
        if body is None:
            return ToolResponse.error(
                f"Unknown or unauthorized skill: {name or '(empty)'}"
                if self._output_locale == "en"
                else f"未知或未授权的技能：{name or '（空）'}"
            )
        return ToolResponse.ok(
            f"Loaded skill '{name}':\n\n{body}"
            if self._output_locale == "en"
            else f"已加载技能「{name}」：\n\n{body}",
            data={"skill": name},
        )
