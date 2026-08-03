"""Resume library query tool."""

from agentmaker import Tool, ToolParameter, ToolResponse

from ...features.resumes import public as resumes

# Bound visible segments per resume; newer versions persist all text and annotate a subset.
_MAX_LINES = 8
_MAX_LINE_CHARS = 500
_MAX_INVENTORY_ITEMS = 50
_DEFAULT_RESUME_ITEMS = 5
_MAX_RESUME_ITEMS = 20


def _excerpt(text: str) -> str:
    return text if len(text) <= _MAX_LINE_CHARS else f"{text[:_MAX_LINE_CHARS]}…"


def _resume_tag(item: dict) -> str:
    if item["binding"] != "application":
        return "通用版"
    company = item.get("application_company")
    position = item.get("application_position")
    return f"岗位专属-{company}-{position}" if company and position else "岗位专属-岗位已删除"


class QueryLibraryTool(Tool):
    """Read resume versions and bounded visible text from the library."""

    supports_parallel = True

    def __init__(self, db_path: str, user_id: str):
        super().__init__(
            "query_library",
            "查询资料库。resume_inventory=只列简历数量、版本名和版本类型；"
            "resumes=按名称查看简历可见文字，未指定名称时只返回最近版本。"
            "用户只问「我有几份/哪些简历」时必须用 resume_inventory。",
            origin="careerdesk",
        )
        self._db_path = db_path
        self._user_id = user_id

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("action", "string", "查什么", schema={
                "type": "string", "enum": ["resume_inventory", "resumes"],
                "description": "resume_inventory=简历数量/版本名；resumes=简历内容"}),
            ToolParameter("name", "string", "精确简历版本名（仅 resumes）", required=False),
            ToolParameter("limit", "integer", "最多返回几份简历（仅 resumes，1–20）",
                          required=False, default=_DEFAULT_RESUME_ITEMS),
        ]

    def run(self, parameters: dict) -> ToolResponse:
        action = parameters["action"]
        if action == "resume_inventory":
            return self._resume_inventory()
        if action == "resumes":
            return self._resumes(parameters.get("name"), parameters.get("limit"))
        return ToolResponse.error(
            f"未知 action：{action}（只支持 resume_inventory / resumes）"
        )

    def _resume_inventory(self) -> ToolResponse:
        """List versions without source text to keep contacts out of chat history."""
        items = resumes.list_resume_summaries(self._db_path, self._user_id)
        if not items:
            return ToolResponse.ok(
                "资料管理中还没有简历（到资料管理页上传一份 PDF/DOCX 即可）。",
                data={"count": 0, "items": [], "truncated": False,
                      "ui_actions": [{"kind": "open_library"}]},
            )
        inventory = []
        for item in items[:_MAX_INVENTORY_ITEMS]:
            tag = _resume_tag(item)
            inventory.append({"name": item["name"], "tag": tag})
        names = "、".join(f"「{item['name']}」（{item['tag']}）" for item in inventory)
        remainder = len(items) - len(inventory)
        suffix = f"；另有 {remainder} 份未展开" if remainder else ""
        return ToolResponse.ok(
            f"你有 {len(items)} 份简历：{names}{suffix}。",
            data={"count": len(items), "items": inventory, "truncated": remainder > 0,
                  "ui_actions": [{"kind": "open_library"}]},
        )

    def _resumes(self, name: str | None, raw_limit) -> ToolResponse:
        """Read one explicit version within bounds, never every full resume at once."""
        limit = max(1, min(int(raw_limit or _DEFAULT_RESUME_ITEMS), _MAX_RESUME_ITEMS))
        summaries = resumes.list_resume_summaries(self._db_path, self._user_id)
        selected = ([item for item in summaries if item["name"] == name]
                    if name else summaries[:limit])
        if not selected:
            if name:
                return ToolResponse.ok(
                    f"没有找到名为「{name}」的未归档简历。",
                    data={"items": [], "truncated": False,
                          "ui_actions": [{"kind": "open_library"}]},
                )
            return ToolResponse.ok("资料管理中还没有简历（到资料管理页上传一份 PDF/DOCX 即可）。",
                                   data={"items": [], "truncated": False,
                                         "ui_actions": [{"kind": "open_library"}]})
        blocks = []
        result_items = []
        for summary in selected:
            item = resumes.get_resume(self._db_path, self._user_id, summary["id"])
            if item is None or item.get("archived"):
                continue
            tag = _resume_tag(summary)
            lines = item.get("lines") or []
            annotated = sum(bool(line.get("knowledge_points")) for line in lines if isinstance(line, dict))
            annotation_text = f" · {annotated} 段技术要点" if annotated else ""
            block = [f"- 「{item['name']}」（{tag}）· {len(lines)} 段可见文字{annotation_text}"]
            excerpts = [{"text": _excerpt(line["text"]),
                         "knowledge_points": list(line.get("knowledge_points") or [])}
                        for line in lines[:_MAX_LINES] if isinstance(line, dict)
                        and isinstance(line.get("text"), str)]
            block += [f"    · {line['text']}" for line in excerpts]
            if len(lines) > _MAX_LINES:
                block.append(f"    · …还有 {len(lines) - _MAX_LINES} 条")
            blocks.append("\n".join(block))
            result_items.append({
                "id": item["id"], "name": item["name"], "tag": tag,
                "line_count": len(lines), "lines": excerpts,
            })
        truncated = not name and len(summaries) > len(selected)
        prefix = f"已返回 {len(result_items)} 份简历"
        if truncated:
            prefix += f"（共 {len(summaries)} 份；可按名称继续查询）"
        return ToolResponse.ok(
            f"{prefix}：\n" + "\n".join(blocks),
            data={
                "items": result_items,
                "truncated": truncated,
                "ui_actions": [{
                    "kind": "open_resume", "resource_id": result_items[0]["id"],
                }] if name and len(result_items) == 1 else [{"kind": "open_library"}],
            },
        )
