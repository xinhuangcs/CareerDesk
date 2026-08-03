
import asyncio
import json
from uuid import uuid4

import pytest

from tests.support import ScriptedLLM

from careerdesk.platform.database import init_db, read_connection, transaction
from careerdesk.features.applications import operations as application_operations
from careerdesk.platform.database.connection import set_meta
from careerdesk.features.questions.public import question_overview
from careerdesk.features.reviews.public import (
    ReviewService,
    approve_review_operation,
    prepare_review_undo_operation,
)
from careerdesk.agentic.tools.manage_timeline import DeleteApplicationTool, UpdateApplicationTool
from careerdesk.orchestration.maintenance.service import MaintenanceService
from tests.review_record_test_helpers import execute_review_record

TODAY = "2026-07-10"
UPDATE_TURN_ID = "00000000-0000-4000-8000-000000000105"


def scripted(*payloads) -> ScriptedLLM:
    return ScriptedLLM([json.dumps(payload, ensure_ascii=False) for payload in payloads])


def run(coroutine):
    return asyncio.run(coroutine)


def run_update(db_path: str, parameters: dict, *, tool=None):
    active_tool = tool or UpdateApplicationTool(
        db_path, "u1", client_turn_id=str(uuid4()),
    )
    return active_tool.run({"updates": [parameters]})


def completed_update_operation(response):
    return response.data["results"][0]["operation"]


def undo_review(db_path: str, user_id: str, journal_id: int) -> dict:
    proposal = prepare_review_undo_operation(
        db_path,
        user_id,
        journal_id=journal_id,
    )
    return approve_review_operation(
        db_path,
        user_id,
        proposal["operation_id"],
    )["result"]


def applied_extraction(
    company: str,
    position: str,
    occurred_date: str | None = None,
    *,
    project_stage: bool = True,
) -> dict:
    return {
        "company": company,
        "position": position,
        "channel": None,
        "history": {
            "step": "提交申请",
            "date": occurred_date,
            "outcome": None,
            "summary": f"投递了{company}",
        },
        "projected_state": (
            {"stage": "applied", "current_step": "提交申请"}
            if project_stage
            else None
        ),
        "next_action": None,
        "questions": [],
        "mood": None,
        "time_of_day": None,
        "factors": [],
    }


def rows(db_path: str, sql: str, *params) -> list[tuple]:
    with read_connection(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def test_spoken_application_lands_in_one_shot(tmp_path):
    db_path = str(tmp_path / "biz.db")
    init_db(db_path)
    service = ReviewService(db_path, scripted(applied_extraction("字节跳动", "厚道工程师")))

    result = run(execute_review_record(
        service, "u1", "我今天刚申请了字节跳动的厚道工程师岗位", today=TODAY,
    ))
    assert result["state"] == "completed" and result["outcome"] == "applied"
    listed = rows(
        db_path,
        "SELECT company, position, stage, current_step FROM applications",
    )
    assert listed == [("字节跳动", "厚道工程师", "applied", "提交申请")]
    entries = rows(db_path, "SELECT step, source FROM timeline_entries")
    assert entries == [("提交申请", "review")]


def test_past_application_fact_sets_date_without_downgrading_current_stage(tmp_path):
    db_path = str(tmp_path / "biz.db")
    init_db(db_path)
    service = ReviewService(db_path, scripted(applied_extraction("DTO", "学生助理", "2026-07-10")))
    run(execute_review_record(service, "u1", "我今天投了DTO的学生助理", today=TODAY))
    assert rows(db_path, "SELECT stage, applied_date FROM applications") == [
        ("applied", "2026-07-10"),
    ]

    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET stage = 'interviewing', current_step = '一面'",
        )
    service = ReviewService(
        db_path,
        scripted(applied_extraction(
            "DTO", "学生助理", "2026-07-01", project_stage=False,
        )),
    )
    run(execute_review_record(service, "u1", "对了我之前 7 月 1 号就投了DTO", today=TODAY))
    stage, current_step, applied_date = rows(
        db_path,
        "SELECT stage, current_step, applied_date FROM applications",
    )[0]
    assert (stage, current_step) == ("interviewing", "一面")
    assert applied_date == "2026-07-10"


def test_update_tool_rename_survives_metadata_reconcile(tmp_path):
    db_path = str(tmp_path / "biz.db")
    init_db(db_path)
    service = ReviewService(db_path, scripted(applied_extraction("阿里", "通义应用工程师")))
    run(execute_review_record(service, "u1", "投了阿里的通义应用工程师", today=TODAY))

    response = run_update(db_path, {"company": "阿里", "new_position": "前端工程师"})
    assert response.status == "success"
    assert rows(db_path, "SELECT position FROM applications") == [("前端工程师",)]
    edits = rows(
        db_path,
        "SELECT extraction_json FROM journal WHERE kind = 'correction' "
        "AND json_extract(extraction_json, '$.operation_type') = 'application_update'",
    )
    extraction = json.loads(edits[0][0])
    assert len(edits) == 1 and extraction["operation_type"] == "application_update"
    assert extraction["target"]["application_id"] > 0
    assert extraction["effect"]["changed_fields"] == [
        {"field": "position", "before": "通义应用工程师", "after": "前端工程师"},
    ]

    set_meta(db_path, "derive_version:user:u1", 0)
    rebuilt = run(MaintenanceService(db_path).reconcile("u1"))
    assert rebuilt["status"] == "ok" and rebuilt["reconciled"] == 1
    assert rows(db_path, "SELECT company, position FROM applications") == [("阿里", "前端工程师")]


def test_update_tool_disambiguation_and_stage(tmp_path):
    db_path = str(tmp_path / "biz.db")
    init_db(db_path)
    for position in ("AI工程师", "后端工程师"):
        service = ReviewService(db_path, scripted(applied_extraction("字节跳动", position)))
        run(execute_review_record(service, "u1", f"投了字节跳动的{position}", today=TODAY))

    ambiguous = run_update(db_path, {"company": "字节跳动", "new_stage": "pooled"})
    assert ambiguous.status == "partial"

    ok = run_update(
        db_path, {"company": "字节跳动", "position": "AI工程师", "new_stage": "pooled"},
    )
    assert ok.status == "success"
    assert rows(db_path, "SELECT stage FROM applications WHERE position = 'AI工程师'") == [("pooled",)]

    withdrawn = run_update(db_path, {
        "company": "字节跳动", "position": "后端工程师", "new_stage": "withdrawn",
    })
    assert withdrawn.status == "success"
    assert rows(db_path, "SELECT stage FROM applications WHERE position = '后端工程师'") == [("withdrawn",)]
    (edit_json,) = rows(db_path, "SELECT extraction_json FROM journal WHERE kind = 'correction' "
                                 "ORDER BY id DESC LIMIT 1")[0]
    assert json.loads(edit_json)["effect"]["changed_fields"] == [
        {"field": "stage", "before": "applied", "after": "withdrawn"},
    ]
    assert run_update(
        db_path, {"company": "没投过的公司", "new_stage": "pooled"},
    ).status in {"error", "partial"}


def test_update_tool_manages_one_application_note_without_echoing_value(tmp_path):
    db_path = str(tmp_path / "biz.db")
    init_db(db_path)
    run(execute_review_record(
        ReviewService(db_path, scripted(applied_extraction("腾讯", "后端"))),
        "u1",
        "投了腾讯后端",
        today=TODAY,
    ))
    tool = UpdateApplicationTool(db_path, "u1", client_turn_id=UPDATE_TURN_ID)
    created = run_update(db_path, {"company": "腾讯", "append_note": "联系校友 A"}, tool=tool)
    replayed = run_update(db_path, {"company": "腾讯", "append_note": "联系校友 A"}, tool=tool)
    appended = run_update(db_path, {"company": "腾讯", "append_note": "周五跟进"})

    assert created.status == appended.status == "success"
    assert replayed.status == "error"
    assert "联系校友 A" not in created.text and "周五跟进" not in appended.text
    assert completed_update_operation(created)["effect"]["changed_fields"] == [{
        "field": "application_note", "before": None, "after": "联系校友 A",
    }]
    assert rows(db_path, "SELECT application_note FROM applications") == [
        ("联系校友 A\n周五跟进",),
    ]

    replaced = run_update(db_path, {"company": "腾讯", "replacement_note": "仅保留最终结论"})
    assert replaced.status == "success" and "仅保留最终结论" not in replaced.text
    assert rows(db_path, "SELECT application_note FROM applications") == [
        ("仅保留最终结论",),
    ]
    assert run_update(db_path, {
        "company": "腾讯",
        "append_note": "不能混写",
        "new_stage": "interviewing",
    }).status == "error"

    cleared = run_update(db_path, {"company": "腾讯", "clear_note": True})
    assert cleared.status == "success"
    assert rows(db_path, "SELECT application_note FROM applications") == [(None,)]
    undone = application_operations.undo_application_update_operation(
        db_path,
        "u1",
        completed_update_operation(cleared)["operation_id"],
        command_id=uuid4(),
    )
    assert undone["state"] == "undone"
    assert rows(db_path, "SELECT application_note FROM applications") == [
        ("仅保留最终结论",),
    ]


def test_rename_into_existing_requires_trusted_merge(tmp_path):
    db_path = str(tmp_path / "biz.db")
    init_db(db_path)
    for position in ("通义应用工程师", "前端工程师"):
        service = ReviewService(db_path, scripted(applied_extraction("阿里", position)))
        run(execute_review_record(service, "u1", f"投了阿里的{position}", today=TODAY))

    response = run_update(db_path, {
        "company": "阿里", "position": "通义应用工程师", "new_position": "前端工程师",
    })
    assert response.status == "success"
    assert response.data["operation_type"] == "application_merge"
    assert response.data["state"] == "pending"
    assert rows(db_path, "SELECT position FROM applications ORDER BY position") == [
        ("前端工程师",), ("通义应用工程师",),
    ]
    (entry_count,) = rows(db_path, "SELECT COUNT(*) FROM timeline_entries")[0]
    assert entry_count == 2
    assert rows(
        db_path,
        "SELECT state FROM journal WHERE kind='correction' AND operation_id IS NOT NULL "
        "AND state='awaiting_user'",
    ) == [("awaiting_user",)]


def test_cross_company_rename_only_updates_selected_application_provenance(tmp_path):
    db_path = str(tmp_path / "biz.db")
    init_db(db_path)
    for position, text in (("P1", "P1题"), ("P2", "P2题")):
        extraction = applied_extraction("A", position)
        extraction["questions"] = [
            {"text": text, "stuck": False, "knowledge_points": []},
        ]
        run(execute_review_record(
            ReviewService(db_path, scripted(extraction)),
            "u1",
            f"投了A的{position}，问了{text}",
            today=TODAY,
        ))

    response = run_update(db_path, {"company": "A", "position": "P1", "new_company": "B"})
    assert response.status == "success"
    assert rows(
        db_path,
        "SELECT a.company, a.position, o.company FROM review_question_occurrences o "
        "JOIN applications a ON a.id = o.application_id ORDER BY a.position",
    ) == [("B", "P1", "B"), ("A", "P2", "A")]
    assert question_overview(db_path, "u1", company="A")["total"] == 1
    assert question_overview(db_path, "u1", company="B")["total"] == 1


def test_cross_company_merge_collision_only_creates_trusted_preview(tmp_path):
    db_path = str(tmp_path / "biz.db")
    init_db(db_path)
    for company, position, text in (("B", "P2", "目标题"), ("A", "P1", "源题")):
        extraction = applied_extraction(company, position)
        extraction["questions"] = [
            {"text": text, "stuck": False, "knowledge_points": []},
        ]
        run(execute_review_record(
            ReviewService(db_path, scripted(extraction)),
            "u1",
            f"投了{company}的{position}，问了{text}",
            today=TODAY,
        ))
    response = run_update(db_path, {
        "company": "A", "position": "P1", "new_company": "B", "new_position": "P2",
    })
    assert response.status == "success"
    assert response.data["operation_type"] == "application_merge"
    assert rows(
        db_path,
        "SELECT a.company, a.position, o.company FROM review_question_occurrences o "
        "JOIN applications a ON a.id=o.application_id ORDER BY a.company",
    ) == [("A", "P1", "A"), ("B", "P2", "B")]
    assert rows(db_path, "SELECT company, position FROM applications ORDER BY company") == [
        ("A", "P1"), ("B", "P2"),
    ]


def test_pending_merge_is_not_replayed_by_maintenance(tmp_path):
    db_path = str(tmp_path / "biz.db")
    init_db(db_path)
    for company, position in (("B", "P2"), ("A", "P1")):
        run(execute_review_record(
            ReviewService(db_path, scripted(applied_extraction(company, position))),
            "u1",
            f"投了{company}的{position}",
            today=TODAY,
        ))
    response = run_update(db_path, {
        "company": "A", "position": "P1", "new_company": "B", "new_position": "P2",
    })
    assert response.status == "success"
    assert response.data["state"] == "pending"

    set_meta(db_path, "derive_version:user:u1", 0)
    rebuilt = run(MaintenanceService(db_path).reconcile("u1"))
    assert rebuilt["status"] == "ok"
    assert rows(db_path, "SELECT company, position FROM applications ORDER BY company") == [
        ("A", "P1"), ("B", "P2"),
    ]


def test_delete_tool_stays_deleted_after_metadata_reconcile(tmp_path):
    db_path = str(tmp_path / "biz.db")
    init_db(db_path)
    extraction = applied_extraction("DTO", "学生助理")
    extraction["questions"] = [{"text": "自我介绍一下？", "stuck": False, "knowledge_points": ["自我介绍"]}]
    service = ReviewService(db_path, scripted(extraction))
    run(execute_review_record(
        service, "u1", "投了DTO学生助理，顺带被问了自我介绍", today=TODAY,
    ))

    tool = DeleteApplicationTool(db_path, "u1")
    response = tool.run({"company": "DTO"})
    assert response.status == "success" and "尚未删除" in response.text
    assert rows(db_path, "SELECT COUNT(*) FROM applications")[0] == (1,)
    approved = application_operations.approve_application_delete_operation(
        db_path,
        "u1",
        response.data["operation_id"],
    )
    assert approved["state"] == "completed"
    assert rows(db_path, "SELECT COUNT(*) FROM applications")[0] == (0,)
    assert rows(db_path, "SELECT COUNT(*) FROM timeline_entries")[0] == (0,)
    assert rows(db_path, "SELECT application_id FROM questions") == [(None,)]

    set_meta(db_path, "derive_version:user:u1", 0)
    rebuilt = run(MaintenanceService(db_path).reconcile("u1"))
    assert rebuilt["status"] == "ok" and rebuilt["reconciled"] == 1
    assert rows(db_path, "SELECT COUNT(*) FROM applications")[0] == (0,)


def test_delete_tool_prepares_multiple_exact_targets_in_one_call(tmp_path):
    db_path = str(tmp_path / "biz.db")
    init_db(db_path)
    for company, position in (("A", "P1"), ("B", "P2")):
        run(execute_review_record(
            ReviewService(db_path, scripted(applied_extraction(company, position))),
            "u1",
            f"投了{company}的{position}",
            today=TODAY,
        ))

    response = DeleteApplicationTool(db_path, "u1").run({
        "targets": [
            {"company": "A", "position": "P1"},
            {"company": "B", "position": "P2"},
        ],
    })

    assert response.status == "success"
    assert response.data["operation_type"] == "application_delete_batch"
    assert len(response.data["operations"]) == 2
    assert all(item["state"] == "pending" for item in response.data["operations"])
    assert rows(db_path, "SELECT COUNT(*) FROM applications") == [(2,)]
    assert len(application_operations.list_pending_application_delete_operations(
        db_path, "u1",
    )) == 2


def test_delete_tool_resolves_explicit_all_scope_from_the_authoritative_board(tmp_path):
    db_path = str(tmp_path / "biz.db")
    init_db(db_path)
    for company, position in (("A", "P1"), ("B", "P2")):
        run(execute_review_record(
            ReviewService(db_path, scripted(applied_extraction(company, position))),
            "u1",
            f"投了{company}的{position}",
            today=TODAY,
        ))
    run(execute_review_record(
        ReviewService(db_path, scripted(applied_extraction("其他", "岗位"))),
        "u2",
        "另一个用户投了其他岗位",
        today=TODAY,
    ))

    response = DeleteApplicationTool(db_path, "u1").run({"scope": "all"})

    assert response.status == "success"
    assert response.data["operation_type"] == "application_delete_batch"
    assert {
        (operation["target"]["company"], operation["target"]["position"])
        for operation in response.data["operations"]
    } == {("A", "P1"), ("B", "P2")}
    assert rows(db_path, "SELECT COUNT(*) FROM applications WHERE user_id='u1'") == [(2,)]
    assert len(application_operations.list_pending_application_delete_operations(
        db_path, "u1",
    )) == 2
    assert application_operations.list_pending_application_delete_operations(
        db_path, "u2",
    ) == []


@pytest.mark.parametrize("parameters", [
    {"scope": "all", "company": "A"},
    {"scope": "all", "targets": [{"company": "A", "position": "P1"}]},
    {"scope": "current"},
    {},
])
def test_delete_tool_rejects_ambiguous_selector_modes(tmp_path, parameters):
    db_path = str(tmp_path / "biz.db")
    init_db(db_path)

    response = DeleteApplicationTool(db_path, "u1").run(parameters)

    assert response.status == "error"
    assert application_operations.list_pending_application_delete_operations(
        db_path, "u1",
    ) == []


def test_delete_tool_never_falls_back_from_wrong_explicit_position(tmp_path):
    db_path = str(tmp_path / "biz.db")
    init_db(db_path)
    run(execute_review_record(
        ReviewService(db_path, scripted(applied_extraction("A", "真实岗位"))),
        "u1",
        "投了A的真实岗位",
        today=TODAY,
    ))

    tool = DeleteApplicationTool(db_path, "u1")
    response = tool.run(
        {"company": "A", "position": "不存在岗位"},
    )
    assert response.status == "error"
    exhausted = tool.run({"company": "A", "position": "真实岗位"})
    assert exhausted.status == "error"
    assert exhausted.data == {
        "reason": "request_proposal_write_fence",
        "proposal_type": "application_delete",
    }
    assert rows(db_path, "SELECT company, position FROM applications") == [("A", "真实岗位")]
    assert rows(
        db_path,
        "SELECT COUNT(*) FROM journal WHERE operation_id IS NOT NULL "
        "AND json_extract(extraction_json, '$.operation_type') = 'application_delete'",
    ) == [(0,)]
