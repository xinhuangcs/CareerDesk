"""Timeline query tool for progress and schedule questions."""

from datetime import date, datetime, timedelta

from agentmaker import Tool, ToolParameter, ToolResponse

from ...core.config import local_today
from ...features.applications import public as applications

_STAGE_LABELS = applications.STAGE_LABELS


class QueryTimelineTool(Tool):
    """Read the board, company progress, and upcoming schedule."""

    supports_parallel = True

    def __init__(self, db_path: str, user_id: str):
        super().__init__(
            "query_timeline",
            "查询求职进展。board=全局看板概览（各阶段计数+少量样例）；"
            "list=按阶段不截断地列出全部投递（带投递日期与已投天数——「把面试中的都列出来」「哪些投了好久没消息」用它）；"
            "company=某公司的投递进展、个人备注与历程（需传 company，简称也能命中全称）；"
            "history=某一精确岗位的有界完整历程；upcoming=未来几天日程；"
            "attention=下一步逾期或长期没有事实且没有下一步的在途岗位；"
            "statistics=按可选阶段、渠道和最近投递天数统计当前快照。",
            origin="careerdesk",
        )
        self._db_path = db_path
        self._user_id = user_id

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("action", "string", "查什么", schema={
                "type": "string", "enum": [
                    "board", "list", "company", "history", "upcoming", "attention", "statistics",
                ],
                "description": "看板/列表/单公司/岗位历程/近期日程/待关注岗位/统计"}),
            ToolParameter("stage", "string", "阶段筛选（list/statistics 用）", required=False,
                          schema={"type": "string", "enum": list(_STAGE_LABELS)}),
            ToolParameter("company", "string", "公司名（company/history 时必传，子串即可）", required=False),
            ToolParameter("position", "string", "岗位名（history 或同公司多岗时用于精确定位）", required=False),
            ToolParameter("channel", "string", "渠道筛选（statistics 用，按保存值匹配）", required=False),
            ToolParameter(
                "days",
                "integer",
                "upcoming=未来窗口（默认7）；attention=无进展阈值（默认30）；statistics=最近投递窗口（省略=全部）",
                required=False,
                schema={"type": "integer", "minimum": 1, "maximum": 365},
            ),
            ToolParameter("limit", "integer", "history 最多返回多少条历程（默认20，最大50）",
                          required=False, schema={"type": "integer", "minimum": 1, "maximum": 50}),
        ]

    def run(self, parameters: dict) -> ToolResponse:
        action = parameters["action"]
        if action == "board":
            return self._board()
        if action == "list":
            return self._list(parameters.get("stage"))
        if action == "company":
            company = parameters.get("company")
            if not company:
                return ToolResponse.error("action=company 需要传 company（公司名）")
            return self._company(company)
        if action == "history":
            company = parameters.get("company")
            if not company:
                return ToolResponse.error("action=history 需要传 company（公司名）")
            raw_limit = parameters.get("limit", 20)
            if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or not 1 <= raw_limit <= 50:
                return ToolResponse.error("limit 必须是 1–50 的整数")
            return self._history(company, parameters.get("position"), raw_limit)
        if action == "upcoming":
            days = self._days(parameters.get("days"), default=7, maximum=60)
            if days is None:
                return ToolResponse.error("upcoming 的 days 必须是 1–60 的整数")
            return self._upcoming(days)
        if action == "attention":
            days = self._days(parameters.get("days"), default=30, maximum=365)
            if days is None:
                return ToolResponse.error("attention 的 days 必须是 1–365 的整数")
            return self._attention(days)
        if action == "statistics":
            days = self._days(parameters.get("days"), default=None, maximum=365)
            if parameters.get("days") is not None and days is None:
                return ToolResponse.error("statistics 的 days 必须是 1–365 的整数")
            return self._statistics(
                days=days,
                stage=parameters.get("stage"),
                channel=parameters.get("channel"),
            )
        return ToolResponse.error(
            "未知 action（可选 board/list/company/history/upcoming/attention/statistics）",
        )

    @staticmethod
    def _days(raw, *, default: int | None, maximum: int) -> int | None:
        if raw is None:
            return default
        if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= maximum:
            return None
        return raw

    def _board(self) -> ToolResponse:
        board = applications.board(self._db_path, self._user_id)
        if board["total"] == 0:
            return ToolResponse.ok("投递记录还是空的，粘贴岗位信息即可开始跟踪。", data=board)
        summary = "；".join(f"{_STAGE_LABELS[stage]} {len(items)} 家"
                            for stage, items in board["columns"].items() if items)
        recent = board["columns"]
        lines = [self._application_line(item)
                 for stage_items in recent.values() for item in stage_items[:3]]
        actions = [{"kind": "open_timeline"}]
        return ToolResponse.ok(
            f"共 {board['total']} 条投递：{summary}。\n" + "\n".join(lines[:10]),
            data={**board, "ui_actions": actions},
        )

    @staticmethod
    def _application_line(item: dict, suffix: str = "") -> str:
        next_action = item.get("next_action") or {}
        return (
            f"- {item['company']}·{item['position']}（{_STAGE_LABELS[item['stage']]}"
            + (f" · {item['current_step']}" if item.get("current_step") else "")
            + (f"，下一步 {next_action['step']}" if next_action.get("step") else "")
            + (f" {next_action['date']}" if next_action.get("date") else "")
            + suffix
            + "）"
        )

    @staticmethod
    def _ui_actions(items: list[dict]) -> list[dict]:
        if len(items) == 1:
            return [{"kind": "open_application", "resource_id": items[0]["id"]}]
        return [{"kind": "open_timeline"}] if items else []

    def _list(self, stage: str | None) -> ToolResponse:
        """List every application by stage with date and elapsed days."""
        board = applications.board(self._db_path, self._user_id)
        items = [item for column_items in board["columns"].values() for item in column_items]
        if stage:
            items = [item for item in items if item["stage"] == stage]
        if not items:
            scope = _STAGE_LABELS.get(stage, stage) if stage else ""
            return ToolResponse.ok(f"没有{scope}的投递记录。", data={"items": []})
        today = local_today()
        lines = []
        for item in items:
            applied = item.get("applied_date")
            idle = ""
            if applied:
                try:
                    days = (today - date.fromisoformat(applied)).days
                    idle = f"，{applied} 投递（{days} 天前）" if days > 0 else f"，{applied} 投递（今天）"
                except ValueError:
                    idle = f"，{applied} 投递"
            lines.append(self._application_line(item, idle))
        scope = _STAGE_LABELS.get(stage, stage) if stage else "全部"
        return ToolResponse.ok(
            f"{scope}投递共 {len(items)} 条：\n" + "\n".join(lines),
            data={
                "items": items,
                "ui_actions": self._ui_actions(items),
            },
        )

    def _company(self, company: str) -> ToolResponse:
        matches = applications.find_applications_by_company(
            self._db_path, self._user_id, company, fuzzy=True,
        )
        if not matches:
            # List names on no match so abbreviation misses do not imply no application.
            board = applications.board(self._db_path, self._user_id)
            names = sorted({item["company"] for column_items in board["columns"].values()
                            for item in column_items})
            hint = ("；你投过：" + "、".join(names)) if names else ""
            return ToolResponse.ok(f"没找到公司名含「{company}」的投递记录{hint}。", data={"matches": []})
        lines = []
        details = []
        for match in matches:
            detail = applications.application_detail(self._db_path, self._user_id, match["id"])
            # Search and detail reads are separate snapshots; concurrent deletion is normal.
            if detail is None:
                continue
            details.append(detail)
            history = " → ".join(
                (entry["occurred_date"] or "?") + " "
                + (entry.get("summary") or entry.get("step") or "进展更新")
                for entry in detail["timeline_entries"][-5:]
            ) or "暂无历程"
            next_action = detail.get("next_action") or {}
            lines.append(f"{detail['company']}·{detail['position']}：{_STAGE_LABELS[detail['stage']]}"
                         + (f" · {detail['current_step']}" if detail.get("current_step") else "")
                         + (f"；下一步 {next_action['step']}" if next_action.get("step") else "")
                         + (f" {next_action['date']}" if next_action.get("date") else "")
                         + (f"。个人备注：{detail['application_note']}" if detail["application_note"] else "")
                         + f"。历程：{history}")
        if not details:
            return ToolResponse.ok(
                f"查询期间「{company}」的匹配岗位已被删除或合并，请以当前看板为准。",
                data={"matches": []},
            )
        return ToolResponse.ok(
            "\n".join(lines),
            data={
                "matches": details,
                "ui_actions": self._ui_actions(details),
            },
        )

    def _history(self, company: str, position: str | None, limit: int) -> ToolResponse:
        if position is not None and (not isinstance(position, str) or not position.strip()):
            return ToolResponse.error("position 必须是非空岗位名")
        matches = applications.find_applications_by_company(
            self._db_path,
            self._user_id,
            company,
            fuzzy=True,
            position=position.strip() if isinstance(position, str) else None,
        )
        if not matches:
            return ToolResponse.ok("没有找到匹配的岗位记录。", data={"application": None})
        if len(matches) > 1:
            options = " / ".join(f"{item['company']}·{item['position']}" for item in matches)
            return ToolResponse.partial(
                f"匹配到多个岗位（{options}），请提供准确 position 后再查历程。",
                data={"matches": matches},
            )
        detail = applications.application_detail(self._db_path, self._user_id, matches[0]["id"])
        if detail is None:
            return ToolResponse.ok("岗位刚被删除或合并，请以当前看板为准。", data={"application": None})
        entries = detail["timeline_entries"][-limit:]
        lines = [
            f"- {entry.get('occurred_date') or '?'}："
            f"{entry.get('summary') or entry.get('step') or '进展更新'}"
            + (f"（{entry['outcome']}）" if entry.get("outcome") else "")
            for entry in entries
        ]
        return ToolResponse.ok(
            f"{detail['company']}·{detail['position']}最近 {len(entries)} 条历程：\n"
            + ("\n".join(lines) if lines else "暂无历程"),
            data={
                "application": {
                    "id": detail["id"],
                    "company": detail["company"],
                    "position": detail["position"],
                    "stage": detail["stage"],
                    "current_step": detail.get("current_step"),
                },
                "entries": entries,
                "truncated": len(detail["timeline_entries"]) > len(entries),
                "ui_actions": [{"kind": "open_application", "resource_id": detail["id"]}],
            },
        )

    def _upcoming(self, days: int) -> ToolResponse:
        today = local_today()
        items = applications.upcoming(self._db_path, self._user_id, today.isoformat(),
                                      (today + timedelta(days=days - 1)).isoformat())
        if not items:
            return ToolResponse.ok(f"未来 {days} 天没有已登记的安排。", data={"items": []})
        lines = [
            f"- {item['next_action']['date']}"
            + (f" {item['next_action']['time']}" if item['next_action'].get('time') else "")
            + f"：{item['company']}·{item['position']} · {item['next_action']['step']}"
            + f"（当前{_STAGE_LABELS[item['stage']]}）"
            for item in items
        ]
        return ToolResponse.ok(
            f"未来 {days} 天的安排：\n" + "\n".join(lines),
            data={
                "items": items,
                "ui_actions": self._ui_actions(items),
            },
        )

    def _attention(self, days: int) -> ToolResponse:
        today = local_today()
        board = applications.board(self._db_path, self._user_id)
        active_stages = {"backlog", "applied", "written_test", "interviewing", "pooled"}
        attention = []
        for item in (item for column in board["columns"].values() for item in column):
            if item["stage"] not in active_stages:
                continue
            next_action = item.get("next_action") or {}
            reason = None
            if next_action.get("date"):
                try:
                    if date.fromisoformat(next_action["date"]) < today:
                        reason = "下一步已逾期"
                except ValueError:
                    reason = "下一步日期无效"
            has_next_action = any(
                next_action.get(key) for key in ("step", "date", "time")
            )
            if reason is None and not has_next_action:
                try:
                    last_activity = datetime.fromisoformat(item["last_activity_time"]).date()
                    age = (today - last_activity).days
                except (TypeError, ValueError):
                    age = days
                if age >= days:
                    reason = f"{age} 天没有新增事实且没有下一步"
            if reason is not None:
                attention.append({**item, "attention_reason": reason})
        if not attention:
            return ToolResponse.ok(
                f"没有下一步逾期或连续 {days} 天无事实且无下一步的在途岗位。",
                data={"items": [], "threshold_days": days},
            )
        lines = [self._application_line(item, f"，{item['attention_reason']}") for item in attention]
        return ToolResponse.ok(
            f"需要关注的岗位共 {len(attention)} 条：\n" + "\n".join(lines),
            data={
                "items": attention,
                "threshold_days": days,
                "ui_actions": self._ui_actions(attention),
            },
        )

    def _statistics(self, *, days: int | None, stage: str | None,
                    channel: str | None) -> ToolResponse:
        if channel is not None and (not isinstance(channel, str) or not channel.strip()):
            return ToolResponse.error("channel 必须是非空字符串")
        today = local_today()
        items = [
            item
            for column in applications.board(self._db_path, self._user_id)["columns"].values()
            for item in column
        ]
        if stage is not None:
            items = [item for item in items if item["stage"] == stage]
        if channel is not None:
            expected = channel.strip().casefold()
            items = [item for item in items if (item.get("channel") or "").casefold() == expected]
        if days is not None:
            start = today - timedelta(days=days - 1)
            filtered = []
            for item in items:
                try:
                    applied = date.fromisoformat(item["applied_date"])
                except (TypeError, ValueError):
                    continue
                if start <= applied <= today:
                    filtered.append(item)
            items = filtered
        by_stage = {
            key: sum(item["stage"] == key for item in items)
            for key in _STAGE_LABELS
            if any(item["stage"] == key for item in items)
        }
        by_channel: dict[str, int] = {}
        for item in items:
            key = item.get("channel") or "未记录"
            by_channel[key] = by_channel.get(key, 0) + 1
        overdue = 0
        with_next_action = 0
        for item in items:
            next_action = item.get("next_action") or {}
            if next_action:
                with_next_action += 1
            try:
                overdue += int(bool(next_action.get("date"))
                               and date.fromisoformat(next_action["date"]) < today)
            except ValueError:
                overdue += 1
        data = {
            "total": len(items),
            "by_stage": by_stage,
            "by_channel": by_channel,
            "with_next_action": with_next_action,
            "overdue_next_action": overdue,
            "filters": {"days": days, "stage": stage, "channel": channel},
            "ui_actions": self._ui_actions(items),
        }
        if days is None and stage is None and channel is None:
            data["funnel"] = applications.statistics(self._db_path, self._user_id)
        stage_text = "、".join(f"{_STAGE_LABELS[key]} {value}" for key, value in by_stage.items()) or "无"
        channel_text = "、".join(f"{key} {value}" for key, value in by_channel.items()) or "无"
        scope = f"最近 {days} 天投递的" if days is not None else "当前"
        return ToolResponse.ok(
            f"{scope}岗位共 {len(items)} 条；阶段：{stage_text}；渠道：{channel_text}；"
            f"有下一步 {with_next_action} 条，其中逾期 {overdue} 条。",
            data=data,
        )
