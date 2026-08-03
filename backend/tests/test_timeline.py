
import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Barrier
from uuid import UUID

import pytest
from tests.support import ScriptedLLM
from fastapi.testclient import TestClient

from careerdesk.core.config import get_settings, local_today
from careerdesk.platform.database import init_db, now_iso, read_connection, transaction
from careerdesk.features.applications import repository as timeline_repository
from careerdesk.features.applications.intake_models import ParsedPosition
from careerdesk.features.applications.public import ApplicationService

TODAY = "2026-07-07"

JD_FRAGMENT = "负责 Agent 方向研发"
BATCH_TEXT = (
    "字节 Seed LLM应用开发（官网投）；JD：负责 Agent 方向研发…… "
    "米哈游 AI工程师，7/6 已投，7/20 截止笔试"
)

BATCH_EXTRACTION = {
    "positions": [
        {"company": "字节", "position": "LLM应用开发", "department": "Seed", "channel": "官网",
         "stage": None, "current_step": None, "applied_date": None, "next_action": None,
         "jd_text": JD_FRAGMENT,
         "jd_source_start": BATCH_TEXT.index(JD_FRAGMENT),
         "jd_source_end": BATCH_TEXT.index(JD_FRAGMENT) + len(JD_FRAGMENT),
         "skills": ["Python", "RAG", "Agent"], "highlights": ["有开源项目加分"]},
        {"company": "米哈游", "position": "AI工程师", "department": None, "channel": None,
         "stage": "applied", "current_step": "完成投递", "applied_date": "2026-07-06",
         "next_action": {"stage": "written_test", "step": "参加笔试", "date": "2026-07-20"},
         "jd_text": None,
         "skills": ["LLM"], "highlights": []},
    ]
}


def scripted(*extractions) -> ScriptedLLM:
    return ScriptedLLM([json.dumps(extraction, ensure_ascii=False) for extraction in extractions])


def run(coroutine):
    return asyncio.run(coroutine)


def test_current_step_is_stored_verbatim_without_round_rewriting(db_path):
    application_id = timeline_repository.create_application_profile(
        db_path,
        "u1",
        company="腾讯",
        position="后端",
        department=None,
        channel=None,
        stage="interviewing",
        current_step="一面复盘后准备二面",
        next_action=None,
        jd_text=None,
    )

    detail = timeline_repository.application_detail(db_path, "u1", application_id)
    assert detail["current_step"] == "一面复盘后准备二面"
    assert detail["timeline_entries"][-1]["to_step"] == "一面复盘后准备二面"


def approve(service: ApplicationService, user_id: str, preview: dict,
            *, exclude_indexes: list[int] | None = None) -> dict:
    operation = service.approve_intake_operation(
        user_id, preview["operation_id"], exclude_indexes=exclude_indexes,
    )
    assert operation["state"] == "completed"
    return operation["result"]


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as test_client:
        yield test_client, str(tmp_path / "careerdesk.db")
    get_settings.cache_clear()


def count(db_path: str, sql: str, *params) -> int:
    with read_connection(db_path) as conn:
        (value,) = conn.execute(sql, params).fetchone()
    return value


def test_parse_proposes_without_writing_then_trusted_approval_writes(db_path):
    proposal_links: list[tuple[str, str]] = []

    def record_proposal(conn, surface: str, operation_id: str) -> None:
        assert conn.in_transaction
        proposal_links.append((surface, operation_id))

    service = ApplicationService(
        db_path,
        scripted(BATCH_EXTRACTION),
        proposal_recorder=record_proposal,
    )
    preview = run(service.parse_batch("u1", BATCH_TEXT, today=TODAY))
    assert preview["status"] == "preview"
    assert str(UUID(preview["operation_id"])) == preview["operation_id"]
    assert len(preview["positions"]) == 2
    assert proposal_links == [("intake", preview["operation_id"])]
    assert all(not item["already_exists"] for item in preview["positions"])
    assert preview["positions"][0]["mode"] == "create"
    assert preview["positions"][0]["flags"] == {
        "invalidate_prep": False,
        "add_applied_entry": False,
        "clear_next_action": False,
    }
    assert preview["positions"][1]["flags"]["add_applied_entry"] is True
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 0
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0
    operation = service.get_intake_operation("u1", preview["operation_id"])
    assert operation is not None and operation["state"] == "pending"

    result = approve(service, "u1", preview)
    assert result["status"] == "ok" and len(result["created"]) == 2 and result["updated"] == []
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 2
    assert count(db_path, "SELECT COUNT(*) FROM companies") == 2
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 1
    with read_connection(db_path) as conn:
        rows = {company: (stage, applied_date) for company, stage, applied_date in
                conn.execute("SELECT company, stage, applied_date FROM applications").fetchall()}
        (processed,) = conn.execute(
            "SELECT processed_time FROM journal WHERE id=?", (preview["journal_id"],)
        ).fetchone()
        unbound = conn.execute(
            "SELECT COUNT(*) FROM applications WHERE company_id IS NULL",
        ).fetchone()[0]
    assert rows["字节"] == ("backlog", None)
    assert rows["米哈游"] == ("applied", "2026-07-06")
    assert unbound == 0
    assert processed is not None


def test_agent_created_state_without_date_still_has_consistent_first_history(db_path):
    extraction = {"positions": [{
        "company": "新公司",
        "position": "平台工程师",
        "department": None,
        "channel": "Agent 导入",
        "stage": "interviewing",
        "current_step": "一面",
        "applied_date": None,
        "next_action": None,
        "jd_text": None,
        "skills": [],
        "highlights": [],
    }]}
    service = ApplicationService(db_path, scripted(extraction))
    preview = run(service.parse_batch("u1", "新公司平台工程师，已经到一面", today=TODAY))

    result = approve(service, "u1", preview)
    application_id = result["created"][0]["id"]
    detail = timeline_repository.application_detail(db_path, "u1", application_id)

    assert detail is not None
    assert (detail["stage"], detail["current_step"]) == ("interviewing", "一面")
    entry = detail["timeline_entries"][0]
    assert (
        entry["step"],
        entry["occurred_date"],
        entry["from_stage"],
        entry["from_step"],
        entry["to_stage"],
        entry["to_step"],
        entry["source"],
    ) == ("一面", None, "backlog", None, "interviewing", "一面", "agent")
    with read_connection(db_path) as conn:
        (current_state_entry_id,) = conn.execute(
            "SELECT current_state_entry_id FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()
    assert current_state_entry_id == entry["id"]


def test_application_note_create_update_read_and_clear(db_path):
    with transaction(db_path) as conn:
        application_id = conn.execute(
            "INSERT INTO applications(user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', '腾讯', '后端', ?, ?)",
            (now_iso(), now_iso()),
        ).lastrowid

    created = timeline_repository.set_application_note(
        db_path, "u1", application_id, "找内推人确认 HC", expected_revision=0,
    )
    assert created["application_note"] == "找内推人确认 HC"
    assert created["revision"] == 1
    detail = timeline_repository.application_detail(db_path, "u1", application_id)
    assert detail["application_note"] == "找内推人确认 HC"
    assert detail["timeline_entries"] == []

    timeline_repository.set_application_note(
        db_path, "u1", application_id, "周五跟进", expected_revision=1,
    )
    assert timeline_repository.application_detail(
        db_path, "u1", application_id,
    )["application_note"] == "周五跟进"
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0

    timeline_repository.set_application_note(
        db_path, "u1", application_id, None, expected_revision=2,
    )
    assert timeline_repository.application_detail(
        db_path, "u1", application_id,
    )["application_note"] is None
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0


def test_parse_stably_merges_duplicate_natural_keys_before_preview(db_path):
    duplicate_text = "重复岗位；JD 正文"
    duplicate = {"positions": [
        {
            "company": " A ", "position": " 工程师 ", "department": None,
            "channel": "官网", "stage": None, "current_step": None,
            "applied_date": None, "next_action": None,
            "jd_text": None, "skills": ["Python", "RAG"], "highlights": ["加分 A"],
        },
        {
            "company": "A", "position": "工程师", "department": "Agent",
            "channel": "内推", "stage": "applied", "current_step": "完成投递",
            "applied_date": "2026-07-13", "next_action": None,
            "jd_text": "JD 正文",
            "jd_source_start": duplicate_text.index("JD 正文"),
            "jd_source_end": duplicate_text.index("JD 正文") + len("JD 正文"),
            "skills": ["RAG", "SQL"],
            "highlights": ["加分 A", "加分 B"],
        },
    ]}
    service = ApplicationService(db_path, scripted(duplicate))

    preview = run(service.parse_batch("u1", duplicate_text, today=TODAY))

    assert [(item["company"], item["position"]) for item in preview["positions"]] == [
        ("A", "工程师"),
    ]
    first = preview["positions"][0]
    assert first["department"] == "Agent" and first["channel"] == "官网"
    assert first["skills"] == ["Python", "RAG", "SQL"]
    assert first["highlights"] == ["加分 A", "加分 B"]

    result = approve(service, "u1", preview)
    assert len(result["created"]) == 1 and result["updated"] == []
    assert len(result["created"]) + len(result["updated"]) == 1
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 1


def test_backlog_upgrades_to_applied_on_dated_repaste(db_path):
    dated = {"positions": [{**BATCH_EXTRACTION["positions"][0], "applied_date": "2026-07-08"}]}
    service = ApplicationService(db_path, scripted(BATCH_EXTRACTION, dated))
    first = run(service.parse_batch("u1", BATCH_TEXT, today=TODAY))
    approve(service, "u1", first)

    second = run(service.parse_batch("u1", BATCH_TEXT, today="2026-07-08"))
    result = approve(service, "u1", second)
    assert len(result["updated"]) == 1 and result["created"] == []
    with read_connection(db_path) as conn:
        stage, applied_date = conn.execute(
            "SELECT stage, applied_date FROM applications WHERE company='字节'").fetchone()
    assert (stage, applied_date) == ("applied", "2026-07-08")
    assert count(db_path, "SELECT COUNT(*) FROM applications WHERE company='字节'") == 1
    with read_connection(db_path) as conn:
        entry = conn.execute(
            "SELECT e.occurred_date, e.journal_id FROM timeline_entries e "
            "JOIN applications a ON a.id = e.application_id AND a.user_id = e.user_id "
            "WHERE e.user_id='u1' AND a.company='字节'"
        ).fetchone()
    assert entry == ("2026-07-08", second["journal_id"])


def test_repaste_merges_position_across_internal_space(db_path):
    spaced = {"positions": [{"company": "字节", "position": "AI 应用工程师",
                             "channel": "官网", "skills": [], "highlights": []}]}
    compact = {"positions": [{"company": "字节", "position": "AI应用工程师",
                              "channel": "官网", "applied_date": "2026-07-08",
                              "skills": [], "highlights": []}]}
    service = ApplicationService(db_path, scripted(spaced, compact))
    first = run(service.parse_batch("u1", "字节 AI 应用工程师", today=TODAY))
    approve(service, "u1", first)

    second = run(service.parse_batch("u1", "字节 AI应用工程师，今天投了", today="2026-07-08"))
    assert second["positions"][0]["mode"] == "update"
    assert second["positions"][0]["already_exists"] is True
    result = approve(service, "u1", second)

    assert len(result["updated"]) == 1 and result["created"] == []
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 1
    with read_connection(db_path) as conn:
        position, stage, applied_date = conn.execute(
            "SELECT position, stage, applied_date FROM applications",
        ).fetchone()
    assert (stage, applied_date) == ("applied", "2026-07-08")
    assert position == "AI 应用工程师"


def test_parse_merges_internal_space_natural_keys_before_preview(db_path):
    duplicate = {"positions": [
        {"company": "字节", "position": "AI 应用工程师", "channel": "官网",
         "skills": ["Python"], "highlights": []},
        {"company": "字节", "position": "AI应用工程师", "department": "Seed",
         "channel": "内推", "skills": ["RAG"], "highlights": []},
    ]}
    service = ApplicationService(db_path, scripted(duplicate))

    preview = run(service.parse_batch("u1", "重复岗位", today=TODAY))
    assert [(p["company"], p["position"]) for p in preview["positions"]] == [("字节", "AI 应用工程师")]

    result = approve(service, "u1", preview)
    assert len(result["created"]) == 1 and result["updated"] == []
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 1


def test_resolve_application_by_name_tolerates_internal_space(db_path):
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications(user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', '字节', 'AI应用工程师', ?, ?)",
            (now_iso(), now_iso()),
        )

    hit = timeline_repository.resolve_application_by_name(db_path, "u1", "字节", "AI 应用工程师")
    assert hit["status"] == "ok" and hit["position"] == "AI应用工程师"

    company_spaced = timeline_repository.resolve_application_by_name(db_path, "u1", "字 节", None)
    assert company_spaced["status"] == "ok"

    miss = timeline_repository.resolve_application_by_name(db_path, "u1", "字节", "后端工程师")
    assert miss["status"] == "not_found"


def test_explicit_applied_stage_without_date_keeps_undated_state_history(db_path):
    payload = {"positions": [{
        "company": "状态测试公司",
        "position": "后端工程师",
        "stage": "applied",
        "applied_date": None,
        "skills": [],
        "highlights": [],
    }]}
    service = ApplicationService(db_path, scripted(payload))

    preview = run(service.parse_batch("u1", "已投状态测试公司的后端岗，日期不记得", today=TODAY))

    assert preview["positions"][0]["stage"] == "applied"
    assert preview["positions"][0]["applied_date"] is None
    assert preview["positions"][0]["flags"]["add_applied_entry"] is False
    approve(service, "u1", preview)
    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT stage, applied_date FROM applications WHERE company='状态测试公司'",
        ).fetchone()
    assert row == ("applied", None)
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 1
    with read_connection(db_path) as conn:
        entry = conn.execute(
            "SELECT occurred_date, from_stage, to_stage, source FROM timeline_entries",
        ).fetchone()
    assert entry == (None, "backlog", "applied", "agent")


def test_repaste_replaces_whole_next_action_and_clears_omitted_note(db_path):
    first_payload = {"positions": [{
        **BATCH_EXTRACTION["positions"][1],
        "next_action": {
            "stage": "written_test", "step": "参加笔试", "date": "2026-07-20",
            "note": "旧日期说明",
        },
    }]}
    second_payload = {"positions": [{
        **BATCH_EXTRACTION["positions"][1],
        "next_action": {
            "stage": "written_test", "step": "参加笔试", "date": "2026-07-25",
        },
    }]}
    service = ApplicationService(db_path, scripted(first_payload, second_payload))
    first = run(service.parse_batch("u1", "米哈游岗位", today=TODAY))
    approve(service, "u1", first)
    second = run(service.parse_batch("u1", "米哈游岗位日期更新", today=TODAY))
    assert second["positions"][0]["next_action"] == {
        "stage": "written_test",
        "step": "参加笔试",
        "date": "2026-07-25",
        "time": None,
        "note": None,
    }
    approve(service, "u1", second)
    with read_connection(db_path) as conn:
        next_action = conn.execute(
            "SELECT next_stage, next_step, next_date, next_time, next_note FROM applications"
        ).fetchone()
    assert next_action == ("written_test", "参加笔试", "2026-07-25", None, None)


def test_explicit_operation_id_cannot_approve_superseded_preview(db_path):
    service = ApplicationService(db_path, scripted(BATCH_EXTRACTION, BATCH_EXTRACTION))
    first = run(service.parse_batch("u1", BATCH_TEXT, today=TODAY))
    second = run(service.parse_batch("u1", BATCH_TEXT, today=TODAY))

    assert service.get_intake_operation("u1", first["operation_id"])["state"] == "stale"
    with pytest.raises(timeline_repository.IntakeOperationConflict):
        service.approve_intake_operation("u1", first["operation_id"])

    result = approve(service, "u1", second)
    assert result["status"] == "ok" and len(result["created"]) == 2
    with read_connection(db_path) as conn:
        (first_state, first_derivation) = conn.execute(
            "SELECT state, derivation_json FROM journal WHERE id=?",
            (first["journal_id"],)).fetchone()
        (second_state,) = conn.execute(
            "SELECT state FROM journal WHERE id=?", (second["journal_id"],)).fetchone()
    assert first_state == "superseded" and "superseded_by" in first_derivation
    assert second_state == "applied"
    assert service.list_pending_intake_operations("u1") == []


def test_reverse_llm_completion_cannot_replace_newer_preview(db_path):
    newer_extraction = {"positions": [{
        "company": "新公司", "position": "新岗位", "skills": [], "highlights": [],
    }]}

    async def race():
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        class FirstCallBlockingLLM(ScriptedLLM):
            def __init__(self):
                super().__init__([
                    json.dumps(BATCH_EXTRACTION, ensure_ascii=False),
                    json.dumps(newer_extraction, ensure_ascii=False),
                ])
                self.started = 0

            async def chat(self, messages, *, tools=None, **kwargs):
                self.started += 1
                response = self._next()
                if self.started == 1:
                    first_entered.set()
                    await release_first.wait()
                return response

        service = ApplicationService(db_path, FirstCallBlockingLLM())
        older_task = asyncio.create_task(service.parse_batch("u1", BATCH_TEXT, today=TODAY))
        await first_entered.wait()
        newer = await service.parse_batch("u1", "新粘贴", today=TODAY)
        release_first.set()
        older = await older_task
        return service, older, newer

    service, older, newer = run(race())
    assert older["status"] == "superseded"
    assert newer["status"] == "preview"
    committed = approve(service, "u1", newer)
    assert committed["status"] == "ok"
    with read_connection(db_path) as conn:
        companies = conn.execute("SELECT company FROM applications").fetchall()
        older_derivation = conn.execute(
            "SELECT derivation_json FROM journal WHERE id = ?", (older["journal_id"],)
        ).fetchone()[0]
    assert companies == [("新公司",)]
    assert json.loads(older_derivation) == {"superseded_by": newer["journal_id"]}


def test_empty_and_failed_parse_do_not_leave_pending_batches(db_path):
    service = ApplicationService(db_path, scripted(BATCH_EXTRACTION, {"positions": []}))
    first = run(service.parse_batch("u1", BATCH_TEXT, today=TODAY))
    empty = run(service.parse_batch("u1", "无法拆条", today=TODAY))
    assert first["status"] == "preview" and empty["status"] == "empty"
    assert service.list_pending_intake_operations("u1") == []
    assert count(db_path, "SELECT COUNT(*) FROM journal WHERE state IN ('pending', 'awaiting_user')") == 0

    class FailingLLM(ScriptedLLM):
        async def chat(self, messages, *, tools=None, **kwargs):
            raise RuntimeError("model unavailable")

    prior = run(ApplicationService(db_path, scripted(BATCH_EXTRACTION)).parse_batch(
        "u2", BATCH_TEXT, today=TODAY))
    assert prior["status"] == "preview"
    with pytest.raises(RuntimeError, match="model unavailable"):
        run(ApplicationService(db_path, FailingLLM()).parse_batch("u2", "会失败", today=TODAY))
    with read_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT state, derivation_json FROM journal WHERE user_id='u2' ORDER BY id"
        ).fetchall()
    assert [state for state, _ in rows] == ["superseded", "failed"]
    assert json.loads(rows[0][1])["superseded_by"] > prior["journal_id"]
    assert json.loads(rows[1][1]) == {
        "intake_failure": {"reason": "parse_failed"},
    }
    assert ApplicationService(db_path, scripted()).list_pending_intake_operations("u2") == []


def test_cancelled_parse_closes_owned_pending_batch(db_path):
    async def cancel_in_flight():
        entered = asyncio.Event()

        class BlockingLLM(ScriptedLLM):
            async def chat(self, messages, *, tools=None, **kwargs):
                entered.set()
                await asyncio.Event().wait()

        service = ApplicationService(db_path, BlockingLLM())
        task = asyncio.create_task(service.parse_batch("u1", "会被取消", today=TODAY))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(cancel_in_flight())
    with read_connection(db_path) as conn:
        state, derivation = conn.execute(
            "SELECT state, derivation_json FROM journal WHERE user_id = 'u1'",
        ).fetchone()
    assert state == "failed"
    assert json.loads(derivation) == {
        "intake_failure": {"reason": "parse_cancelled"},
    }


def test_operation_enforces_tenant_and_sequential_replay_is_idempotent(db_path):
    service = ApplicationService(db_path, scripted(BATCH_EXTRACTION))
    preview = run(service.parse_batch("u1", BATCH_TEXT, today=TODAY))

    assert service.get_intake_operation("u2", preview["operation_id"]) is None
    with pytest.raises(timeline_repository.IntakeOperationNotFound):
        service.approve_intake_operation("u2", preview["operation_id"])
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 0
    assert count(db_path, "SELECT COUNT(*) FROM journal WHERE processed_time IS NULL") == 1

    committed = service.approve_intake_operation("u1", preview["operation_id"])
    repeated = service.approve_intake_operation("u1", preview["operation_id"], exclude_indexes=[])
    assert committed == repeated
    assert committed["state"] == "completed" and committed["result"]["status"] == "ok"
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 2
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 1


def test_concurrent_double_confirmation_replays_one_result(db_path):
    service = ApplicationService(db_path, scripted(BATCH_EXTRACTION))
    preview = run(service.parse_batch("u1", BATCH_TEXT, today=TODAY))
    barrier = Barrier(2)

    def confirm():
        barrier.wait()
        return service.approve_intake_operation("u1", preview["operation_id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in (pool.submit(confirm), pool.submit(confirm))]
    assert results[0] == results[1]
    assert results[0]["state"] == "completed"
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 2
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 1


def test_business_writes_and_operation_receipt_roll_back_together(db_path):
    service = ApplicationService(db_path, scripted(BATCH_EXTRACTION))
    preview = run(service.parse_batch("u1", BATCH_TEXT, today=TODAY))
    with transaction(db_path) as conn:
        conn.execute(
            "CREATE TRIGGER abort_intake_receipt BEFORE UPDATE OF state ON journal "
            "WHEN OLD.state = 'awaiting_user' AND NEW.state = 'applied' "
            "BEGIN SELECT RAISE(ABORT, 'receipt fail'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="receipt fail"):
        service.approve_intake_operation("u1", preview["operation_id"])

    assert count(db_path, "SELECT COUNT(*) FROM applications") == 0
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0
    with read_connection(db_path) as conn:
        state, derivation = conn.execute(
            "SELECT state, derivation_json FROM journal WHERE id=?",
            (preview["journal_id"],),
        ).fetchone()
    assert (state, derivation) == ("awaiting_user", None)
    assert service.get_intake_operation("u1", preview["operation_id"])["state"] == "pending"


def test_repository_rejects_persisted_duplicate_natural_keys_without_side_effects(db_path):
    journal_id, operation_id = timeline_repository.create_intake_batch(
        db_path, "u1", "损坏预览",
    )
    assert timeline_repository.activate_intake_proposal(
        db_path, "u1", journal_id,
        [ParsedPosition(company="A", position="工程师")],
    )
    with transaction(db_path) as conn:
        extraction = json.loads(conn.execute(
            "SELECT extraction_json FROM journal WHERE id = ?", (journal_id,),
        ).fetchone()[0])
        extraction["positions"].append(extraction["positions"][0])
        conn.execute(
            "UPDATE journal SET extraction_json = ? WHERE id = ?",
            (json.dumps(extraction, ensure_ascii=False), journal_id),
        )

    with pytest.raises(timeline_repository.IntakeOperationConflict, match="失效"):
        timeline_repository.approve_intake_operation(db_path, "u1", operation_id)

    assert count(db_path, "SELECT COUNT(*) FROM applications") == 0
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0
    with read_connection(db_path) as conn:
        state, revision = conn.execute(
            "SELECT state, revision FROM journal WHERE id = ?", (journal_id,),
        ).fetchone()
    assert state == "superseded" and revision == 3
    assert timeline_repository.get_intake_operation(
        db_path, "u1", operation_id,
    )["state"] == "stale"


def test_persisted_update_key_tamper_becomes_terminal_stale_without_writes(db_path):
    payload = {"positions": [{
        "company": "A", "position": "工程师", "department": "平台",
        "skills": ["Python"], "highlights": [],
    }]}
    service = ApplicationService(db_path, scripted(payload, payload))
    first = run(service.parse_batch("u1", "A 工程师", today=TODAY))
    approve(service, "u1", first)
    second = run(service.parse_batch("u1", "A 工程师重贴", today=TODAY))

    with transaction(db_path) as conn:
        extraction = json.loads(conn.execute(
            "SELECT extraction_json FROM journal WHERE id = ?",
            (second["journal_id"],),
        ).fetchone()[0])
        extraction["positions"][0]["source"]["company"] = "被篡改公司"
        extraction["positions"][0]["source"]["position"] = "被篡改岗位"
        extraction["positions"][0]["effect"]["company"] = "被篡改公司"
        extraction["positions"][0]["effect"]["position"] = "被篡改岗位"
        conn.execute(
            "UPDATE journal SET extraction_json = ? WHERE id = ?",
            (json.dumps(extraction, ensure_ascii=False), second["journal_id"]),
        )

    before_entries = count(db_path, "SELECT COUNT(*) FROM timeline_entries")
    with pytest.raises(timeline_repository.IntakeOperationConflict, match="失效"):
        service.approve_intake_operation("u1", second["operation_id"])

    with read_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT company, position, department FROM applications",
        ).fetchall()
        journal_state = conn.execute(
            "SELECT state FROM journal WHERE id = ?", (second["journal_id"],),
        ).fetchone()[0]
    assert rows == [("A", "工程师", "平台")]
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == before_entries
    assert journal_state == "superseded"
    assert service.get_intake_operation("u1", second["operation_id"])["state"] == "stale"


def test_binding_drift_fails_all_rows_before_any_business_write(db_path):
    initial = {"positions": [{
        "company": "A", "position": "工程师", "department": "旧部门",
        "skills": [], "highlights": [],
    }]}
    mixed = {"positions": [
        {"company": "新公司", "position": "新岗位", "skills": [], "highlights": []},
        {"company": "A", "position": "工程师", "department": "新部门",
         "skills": [], "highlights": []},
    ]}
    service = ApplicationService(db_path, scripted(initial, mixed))
    first = run(service.parse_batch("u1", "初始岗位", today=TODAY))
    approve(service, "u1", first)
    second = run(service.parse_batch("u1", "一新一旧", today=TODAY))
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET channel = '并发修改', updated_time = ? "
            "WHERE user_id = 'u1' AND company = 'A'",
            (now_iso(),),
        )

    with pytest.raises(timeline_repository.IntakeOperationConflict, match="失效"):
        service.approve_intake_operation("u1", second["operation_id"])

    with read_connection(db_path) as conn:
        applications = conn.execute(
            "SELECT company, position, department, channel FROM applications",
        ).fetchall()
    assert applications == [("A", "工程师", "旧部门", "并发修改")]
    assert service.get_intake_operation("u1", second["operation_id"])["state"] == "stale"


def test_create_binding_collision_becomes_stale_instead_of_silent_update(db_path):
    payload = {"positions": [{
        "company": "A", "position": "工程师", "skills": [], "highlights": [],
    }]}
    service = ApplicationService(db_path, scripted(payload))
    preview = run(service.parse_batch("u1", "A 工程师", today=TODAY))
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, department, created_time, updated_time) "
            "VALUES ('u1', 'A', '工程师', '并发创建', ?, ?)",
            (now_iso(), now_iso()),
        )

    with pytest.raises(timeline_repository.IntakeOperationConflict, match="失效"):
        service.approve_intake_operation("u1", preview["operation_id"])

    with read_connection(db_path) as conn:
        applications = conn.execute(
            "SELECT company, position, department FROM applications",
        ).fetchall()
    assert applications == [("A", "工程师", "并发创建")]
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0
    assert service.get_intake_operation("u1", preview["operation_id"])["state"] == "stale"


def test_approval_rejects_empty_selection_and_invalid_exclusion_indexes(db_path):
    service = ApplicationService(db_path, scripted(BATCH_EXTRACTION))
    preview = run(service.parse_batch("u1", BATCH_TEXT, today=TODAY))

    with pytest.raises(timeline_repository.IntakeOperationInvalidSelection):
        service.approve_intake_operation(
            "u1", preview["operation_id"], exclude_indexes=[0, 3],
        )
    with pytest.raises(timeline_repository.IntakeOperationInvalidSelection):
        service.approve_intake_operation(
            "u1", preview["operation_id"], exclude_indexes=[1, 2],
        )
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 0
    assert count(db_path, "SELECT COUNT(*) FROM journal WHERE processed_time IS NULL") == 1

    valid = approve(service, "u1", preview, exclude_indexes=[2])
    assert valid["status"] == "ok" and len(valid["created"]) == 1


def test_approval_replay_canonicalizes_exclusions_but_rejects_changed_command(db_path):
    service = ApplicationService(db_path, scripted(BATCH_EXTRACTION))
    preview = run(service.parse_batch("u1", BATCH_TEXT, today=TODAY))

    first = service.approve_intake_operation(
        "u1", preview["operation_id"], exclude_indexes=[2, 2],
    )
    replay = service.approve_intake_operation(
        "u1", preview["operation_id"], exclude_indexes=[2],
    )
    assert first == replay
    assert first["exclude_indexes"] == [2]
    assert len(first["result"]["created"]) == 1

    with pytest.raises(timeline_repository.IntakeOperationConflict):
        service.approve_intake_operation(
            "u1", preview["operation_id"], exclude_indexes=[],
        )
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 1
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0


def test_reject_is_idempotent_and_cannot_later_be_approved(db_path):
    service = ApplicationService(db_path, scripted(BATCH_EXTRACTION))
    preview = run(service.parse_batch("u1", BATCH_TEXT, today=TODAY))

    rejected = service.reject_intake_operation("u1", preview["operation_id"])
    replay = service.reject_intake_operation("u1", preview["operation_id"])
    assert rejected == replay
    assert rejected["state"] == "rejected"
    assert rejected["exclude_indexes"] is None and rejected["result"] is None
    assert service.list_pending_intake_operations("u1") == []

    with pytest.raises(timeline_repository.IntakeOperationConflict):
        service.approve_intake_operation("u1", preview["operation_id"])
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 0
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0


def test_pending_operation_read_model_is_tenant_scoped(db_path):
    service = ApplicationService(db_path, scripted(BATCH_EXTRACTION))
    preview = run(service.parse_batch("u1", BATCH_TEXT, today=TODAY))

    pending = service.list_pending_intake_operations("u1")
    assert len(pending) == 1
    assert pending[0] == service.get_intake_operation("u1", preview["operation_id"])
    assert pending[0]["state"] == "pending"
    assert pending[0]["positions"] == preview["positions"]
    assert pending[0]["exclude_indexes"] is None and pending[0]["result"] is None
    assert service.list_pending_intake_operations("u2") == []
    assert service.get_intake_operation("u2", preview["operation_id"]) is None


def test_corrupted_newer_intake_kind_cannot_resurrect_older_preview(db_path):
    older_id, older_operation = timeline_repository.create_intake_batch(
        db_path, "u1", "旧预览",
    )
    assert timeline_repository.activate_intake_proposal(
        db_path,
        "u1",
        older_id,
        [ParsedPosition(company="旧公司", position="旧岗位")],
    )
    newer_id, _ = timeline_repository.create_intake_batch(db_path, "u1", "新意图")
    with transaction(db_path) as conn:
        conn.execute("DROP TRIGGER trg_application_intake_journal_identity_update")
        conn.execute(
            "UPDATE journal SET kind = 'correction' WHERE id = ?",
            (newer_id,),
        )

    for operation in (
        lambda: timeline_repository.list_pending_intake_operations(db_path, "u1"),
        lambda: timeline_repository.get_intake_operation(db_path, "u1", older_operation),
        lambda: timeline_repository.approve_intake_operation(
            db_path, "u1", older_operation,
        ),
    ):
        with pytest.raises(timeline_repository.IntakeOperationConflict, match="owner"):
            operation()

    assert count(db_path, "SELECT COUNT(*) FROM applications") == 0
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0
    with read_connection(db_path) as conn:
        states = conn.execute(
            "SELECT id, state FROM journal WHERE id IN (?, ?) ORDER BY id",
            (older_id, newer_id),
        ).fetchall()
    assert states == [(older_id, "awaiting_user"), (newer_id, "pending")]


def test_ownerless_intake_with_null_operation_id_fails_closed(db_path):
    journal_id, operation_id = timeline_repository.create_intake_batch(
        db_path, "u1", "合法预览",
    )
    assert timeline_repository.activate_intake_proposal(
        db_path,
        "u1",
        journal_id,
        [ParsedPosition(company="合法公司", position="合法岗位")],
    )
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, state, operation_id) "
            "VALUES ('u1', 'jd_batch', '无 owner 的新意图', ?, 'pending', NULL)",
            (now_iso(),),
        )

    for operation in (
        lambda: timeline_repository.list_pending_intake_operations(db_path, "u1"),
        lambda: timeline_repository.get_intake_operation(db_path, "u1", operation_id),
        lambda: timeline_repository.approve_intake_operation(db_path, "u1", operation_id),
        lambda: timeline_repository.create_intake_batch(db_path, "u1", "又一条意图"),
    ):
        with pytest.raises(timeline_repository.IntakeOperationConflict, match="owner.*缺失"):
            operation()

    assert count(db_path, "SELECT COUNT(*) FROM applications") == 0
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0
    assert count(db_path, "SELECT COUNT(*) FROM journal WHERE user_id = 'u1'") == 2
    assert count(
        db_path,
        "SELECT COUNT(*) FROM application_intake_operation_owners WHERE user_id = 'u1'",
    ) == 1


@pytest.mark.parametrize("corruption", ["missing", "misbound"])
def test_corrupted_intake_owner_fails_closed_without_business_writes(db_path, corruption):
    journal_id, operation_id = timeline_repository.create_intake_batch(
        db_path, "u1", "待确认",
    )
    assert timeline_repository.activate_intake_proposal(
        db_path,
        "u1",
        journal_id,
        [ParsedPosition(company="A", position="工程师")],
    )
    with transaction(db_path) as conn:
        if corruption == "missing":
            conn.execute("DROP TRIGGER trg_application_intake_owner_immutable_delete")
            conn.execute(
                "DELETE FROM application_intake_operation_owners WHERE journal_id = ?",
                (journal_id,),
            )
        else:
            conn.execute("DROP TRIGGER trg_application_intake_owner_immutable_update")
            conn.execute(
                "UPDATE application_intake_operation_owners SET user_id = 'u2' "
                "WHERE journal_id = ?",
                (journal_id,),
            )

    for operation in (
        lambda: timeline_repository.list_pending_intake_operations(db_path, "u1"),
        lambda: timeline_repository.get_intake_operation(db_path, "u1", operation_id),
        lambda: timeline_repository.approve_intake_operation(db_path, "u1", operation_id),
        lambda: timeline_repository.reject_intake_operation(db_path, "u1", operation_id),
    ):
        with pytest.raises(timeline_repository.IntakeOperationConflict, match="owner"):
            operation()

    assert count(db_path, "SELECT COUNT(*) FROM applications") == 0
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0
    with read_connection(db_path) as conn:
        state, revision = conn.execute(
            "SELECT state, revision FROM journal WHERE id = ?",
            (journal_id,),
        ).fetchone()
    assert (state, revision) == ("awaiting_user", 1)


def test_intake_owner_integrity_gate_never_projects_proposal_blob(db_path):
    journal_id, _ = timeline_repository.create_intake_batch(db_path, "u1", "预览")
    assert timeline_repository.activate_intake_proposal(
        db_path,
        "u1",
        journal_id,
        [ParsedPosition(
            company="A", position="工程师", jd_text="x" * 50_000,
            jd_source_start=0, jd_source_end=50_000,
        )],
    )
    reads: list[tuple[str | None, str | None]] = []

    def authorize(action, table, column, _database, _trigger):
        if action == sqlite3.SQLITE_READ:
            reads.append((table, column))
        return sqlite3.SQLITE_OK

    with read_connection(db_path) as conn:
        conn.set_authorizer(authorize)
        timeline_repository._assert_intake_owner_integrity(conn, "u1")
        conn.set_authorizer(None)

    assert ("journal", "extraction_json") not in reads
    assert ("journal", "derivation_json") not in reads
    assert ("journal", "content") not in reads


def test_intake_operation_http_contract(client):
    test_client, db_path = client
    service = ApplicationService(db_path, scripted(BATCH_EXTRACTION, BATCH_EXTRACTION))
    first = run(service.parse_batch("me", BATCH_TEXT, today=TODAY))
    base = f"/api/timeline/intake-operations/{first['operation_id']}"

    pending = test_client.get("/api/timeline/intake-operations/pending")
    assert pending.status_code == 200
    assert [item["operation_id"] for item in pending.json()["operations"]] == [
        first["operation_id"],
    ]
    detail = test_client.get(base)
    assert detail.status_code == 200 and detail.json()["state"] == "pending"
    assert test_client.get(base, headers={"Remote-User": "other"}).status_code == 404
    cross_tenant = test_client.post(
        f"{base}/approve",
        headers={"Remote-User": "other"},
        json={"exclude_indexes": []},
    )
    assert cross_tenant.status_code == 404
    cross_tenant_reject = test_client.post(
        f"{base}/reject",
        headers={"Remote-User": "other"},
        json={},
    )
    assert cross_tenant_reject.status_code == 404
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 0

    for invalid_indexes in ([0, 3], [True], [1.5]):
        invalid = test_client.post(
            f"{base}/approve", json={"exclude_indexes": invalid_indexes},
        )
        assert invalid.status_code == 422
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 0

    completed = test_client.post(f"{base}/approve", json={"exclude_indexes": []})
    replay = test_client.post(f"{base}/approve", json={"exclude_indexes": []})
    assert completed.status_code == replay.status_code == 200
    assert completed.json() == replay.json()
    assert completed.json()["state"] == "completed"
    changed = test_client.post(f"{base}/approve", json={"exclude_indexes": [2]})
    assert changed.status_code == 409
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 2
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 1

    second = run(service.parse_batch("me", BATCH_TEXT, today=TODAY))
    second_base = f"/api/timeline/intake-operations/{second['operation_id']}"
    rejected = test_client.post(f"{second_base}/reject", json={})
    rejected_replay = test_client.post(f"{second_base}/reject", json={})
    assert rejected.status_code == rejected_replay.status_code == 200
    assert rejected.json() == rejected_replay.json()
    assert rejected.json()["state"] == "rejected"
    assert test_client.post(f"{second_base}/approve", json={"exclude_indexes": []}).status_code == 409

    unknown = "00000000-0000-4000-8000-000000000000"
    assert test_client.get(f"/api/timeline/intake-operations/{unknown}").status_code == 404


def test_duplicate_paste_flags_and_updates_not_duplicates(db_path):
    service = ApplicationService(db_path, scripted(BATCH_EXTRACTION, BATCH_EXTRACTION))
    first = run(service.parse_batch("u1", BATCH_TEXT, today=TODAY))
    approve(service, "u1", first)

    second = run(service.parse_batch("u1", BATCH_TEXT, today=TODAY))
    assert all(item["already_exists"] for item in second["positions"])
    result = approve(service, "u1", second)
    assert result["created"] == [] and len(result["updated"]) == 2
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 2


def test_board_and_detail_endpoints(client):
    test_client, db_path = client
    service = ApplicationService(db_path, scripted(BATCH_EXTRACTION))
    preview = run(service.parse_batch("me", BATCH_TEXT, today=TODAY))
    approve(service, "me", preview)

    board = test_client.get("/api/timeline/board").json()
    assert board["total"] == 2
    assert len(board["columns"]["backlog"]) == 1
    assert len(board["columns"]["applied"]) == 1
    assert board["columns"]["backlog"][0]["channel"] == "官网"
    assert "jd_text" not in board["columns"]["backlog"][0]

    application_id = board["columns"]["applied"][0]["id"]
    detail = test_client.get(f"/api/timeline/applications/{application_id}").json()
    assert detail["id"] == application_id
    assert len(detail["timeline_entries"]) == 1
    assert detail["timeline_entries"][0]["to_stage"] == "applied"
    assert detail["timeline_entries"][0]["step"] == "完成投递"
    assert "jd_text" in detail and "prep" in detail

    assert test_client.get("/api/timeline/applications/99999").status_code == 404


def test_statistics_uses_reached_stages_and_current_outcomes(client):
    test_client, db_path = client
    timestamp = now_iso()
    with transaction(db_path) as conn:
        application_ids = {}
        for company, stage, applied_date in (
            ("待定厂", "backlog", None),
            ("投递厂", "applied", "2026-07-01"),
            ("已挂厂", "rejected", "2026-07-02"),
            ("Offer厂", "offer", "2026-07-03"),
            ("泡池厂", "pooled", "2026-07-04"),
        ):
            application_ids[company] = conn.execute(
                "INSERT INTO applications(user_id, company, position, stage, applied_date, "
                "created_time, updated_time) VALUES ('me', ?, '岗位', ?, ?, ?, ?)",
                (company, stage, applied_date, timestamp, timestamp),
            ).lastrowid
        for company, transitions in {
            "投递厂": [("backlog", "applied")],
            "已挂厂": [("applied", "interviewing"), ("interviewing", "rejected")],
            "Offer厂": [
                ("applied", "written_test"),
                ("written_test", "interviewing"),
                ("interviewing", "offer"),
            ],
            "泡池厂": [("applied", "pooled")],
        }.items():
            for from_stage, to_stage in transitions:
                conn.execute(
                    "INSERT INTO timeline_entries(user_id, application_id, from_stage, "
                    "to_stage, source, created_time) VALUES ('me', ?, ?, ?, 'manual', ?)",
                    (application_ids[company], from_stage, to_stage, timestamp),
                )

    response = test_client.get("/api/timeline/statistics")
    assert response.status_code == 200
    assert response.json() == {
        "total_positions": 5,
        "submitted": 4,
        "active_processes": 2,
        "offers": 1,
        "rejected": 1,
        "withdrawn": 0,
        "pooled": 1,
        "interview_conversion_percent": 50.0,
        "offer_conversion_percent": 25.0,
        "funnel": {
            "submitted": 4,
            "written_test": 1,
            "interviewing": 2,
            "offer": 1,
            "rejected": 1,
        },
    }


def test_upcoming_endpoint_filters_by_window(client):
    test_client, db_path = client
    in_window = (local_today() + timedelta(days=6)).isoformat()
    out_window = (local_today() + timedelta(days=7)).isoformat()
    batch = {"positions": [
        {"company": "A厂", "position": "岗1", "next_action": {
            "stage": "applied", "step": "投递", "date": in_window,
        }, "skills": [], "highlights": []},
        {"company": "B厂", "position": "岗2", "next_action": {
            "stage": "applied", "step": "投递", "date": out_window,
        }, "skills": [], "highlights": []},
    ]}
    service = ApplicationService(db_path, scripted(batch))
    preview = run(service.parse_batch("me", "两条岗位", today=TODAY))
    approve(service, "me", preview)

    upcoming = test_client.get("/api/timeline/upcoming?days=7").json()
    assert [item["company"] for item in upcoming["items"]] == ["A厂"]


def test_upcoming_excludes_closed_and_pooled_positions(client):
    test_client, db_path = client
    soon = (date.today() + timedelta(days=2)).isoformat()
    ids = {}
    for company, stage, next_action in (
        ("活跃厂", "interviewing", {
            "stage": "interviewing", "step": "二面", "date": soon,
        }),
        ("已挂厂", "rejected", None),
        ("放弃厂", "withdrawn", None),
        ("泡池厂", "pooled", {
            "stage": "interviewing", "step": "等待重启面试", "date": soon,
        }),
    ):
        ids[company] = timeline_repository.create_application_profile(
            db_path,
            "me",
            company=company,
            position="岗位",
            department=None,
            channel=None,
            stage=stage,
            current_step=None,
            next_action=next_action,
            jd_text=None,
        )

    upcoming = test_client.get("/api/timeline/upcoming?days=7").json()
    assert [item["company"] for item in upcoming["items"]] == ["活跃厂"]
    pooled = timeline_repository.application_detail(db_path, "me", ids["泡池厂"])
    assert pooled["next_action"]["step"] == "等待重启面试"
    assert timeline_repository.application_detail(
        db_path, "me", ids["已挂厂"],
    )["next_action"] is None


def test_http_drag_complete_and_terminal_flow_uses_one_authoritative_revision(client):
    test_client, _ = client
    created_response = test_client.post(
        "/api/timeline/applications",
        json={
            "company": "流程厂",
            "position": "Agent 工程师",
            "stage": "applied",
            "current_step": "简历筛选",
            "next_action": {
                "stage": "interviewing",
                "step": "一面",
                "date": (date.today() + timedelta(days=2)).isoformat(),
                "time": "14:00",
                "note": "腾讯会议",
            },
        },
    )
    assert created_response.status_code == 201
    created = created_response.json()
    application_id = created["id"]
    assert created["revision"] == 0

    pooled_response = test_client.put(
        f"/api/timeline/applications/{application_id}/stage",
        json={"expected_revision": 0, "stage": "pooled", "origin": "board_drag"},
    )
    assert pooled_response.status_code == 200
    pooled = pooled_response.json()
    assert (pooled["stage"], pooled["current_step"], pooled["revision"]) == (
        "pooled", "简历筛选", 1,
    )
    assert pooled["next_action"]["step"] == "一面"
    assert pooled["timeline_entries"][-1]["source"] == "drag"

    resumed = test_client.put(
        f"/api/timeline/applications/{application_id}/stage",
        json={"expected_revision": 1, "stage": "interviewing", "origin": "board_drag"},
    ).json()
    assert resumed["current_step"] == "简历筛选"
    assert resumed["next_action"]["step"] == "一面"

    completed_response = test_client.post(
        f"/api/timeline/applications/{application_id}/complete-next-action",
        json={
            "expected_revision": 2,
            "occurred_date": date.today().isoformat(),
            "outcome": "passed",
            "summary": "一面通过",
            "next_action": {
                "stage": "interviewing",
                "step": "二面",
                "date": (date.today() + timedelta(days=5)).isoformat(),
            },
        },
    )
    assert completed_response.status_code == 200
    completed = completed_response.json()
    assert (completed["stage"], completed["current_step"], completed["revision"]) == (
        "interviewing", "一面", 3,
    )
    assert completed["next_action"]["step"] == "二面"

    closed = test_client.put(
        f"/api/timeline/applications/{application_id}/stage",
        json={"expected_revision": 3, "stage": "rejected", "origin": "board_drag"},
    ).json()
    assert (closed["stage"], closed["current_step"], closed["revision"]) == (
        "rejected", "一面", 4,
    )
    assert closed["next_action"] is None

    stale = test_client.put(
        f"/api/timeline/applications/{application_id}/next-action",
        json={
            "expected_revision": 3,
            "next_action": {
                "stage": "interviewing",
                "step": "旧窗口写入",
                "date": None,
            },
        },
    )
    assert stale.status_code == 409
    authoritative = test_client.get(
        f"/api/timeline/applications/{application_id}",
    ).json()
    assert authoritative["revision"] == 4
    assert authoritative["stage"] == "rejected"
    assert authoritative["next_action"] is None


def test_board_orders_columns_by_priority_then_created_time(client):
    test_client, db_path = client
    now = now_iso()
    with transaction(db_path) as conn:
        progressed_id = conn.execute(
            "INSERT INTO applications(user_id, company, position, stage, priority, created_time, "
            "updated_time) VALUES ('me', '高优先级厂', '岗位', 'applied', 'high', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        ).lastrowid
        touched_id = conn.execute(
            "INSERT INTO applications(user_id, company, position, stage, created_time, "
                "updated_time) VALUES ('me', '较新未设置厂', '岗位', 'applied', "
            "'2026-01-02T00:00:00+00:00', '2999-01-01T00:00:00+00:00')",
        ).lastrowid
        conn.execute(
            "INSERT INTO timeline_entries(user_id, application_id, step, summary, "
            "from_stage, to_stage, source, created_time) "
            "VALUES ('me', ?, '跟进', '刚有进展', 'applied', 'applied', 'manual', ?)",
            (progressed_id, now),
        )

    column = test_client.get("/api/timeline/board").json()["columns"]["applied"]
    assert [item["id"] for item in column] == [progressed_id, touched_id]
    assert column[0]["last_activity_time"] == now
    assert column[1]["last_activity_time"] == "2026-01-02T00:00:00+00:00"


def test_board_activity_ignores_note_edits(client):
    test_client, db_path = client
    with transaction(db_path) as conn:
        application_id = conn.execute(
            "INSERT INTO applications(user_id, company, position, stage, created_time, "
            "updated_time) VALUES ('me', '停滞厂', '岗位', 'applied', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        ).lastrowid

    saved = test_client.put(
        f"/api/timeline/applications/{application_id}/note",
        json={"note": "内推人：张三", "expected_revision": 0},
    )
    assert saved.status_code == 200

    item = test_client.get("/api/timeline/board").json()["columns"]["applied"][0]
    assert item["id"] == application_id
    assert item["last_activity_time"] == "2026-01-01T00:00:00+00:00"


def test_identity_required_when_no_fallback(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setenv("APP_DEV_FAKE_USER", "")
    get_settings.cache_clear()
    assert test_client.get("/api/timeline/board").status_code == 401
    assert test_client.get("/api/timeline/board", headers={"Remote-User": "me"}).status_code == 200


def test_repaste_without_jd_keeps_existing_parse(db_path):
    bare = {"positions": [{"company": "字节", "position": "LLM应用开发", "department": None,
                           "channel": None, "jd_text": None, "applied_date": "2026-07-08",
                           "next_action": None, "skills": [], "highlights": []}]}
    service = ApplicationService(db_path, scripted(BATCH_EXTRACTION, bare))
    first = run(service.parse_batch("u1", BATCH_TEXT, today=TODAY))
    approve(service, "u1", first)
    second = run(service.parse_batch("u1", "字节那条今天投掉了", today="2026-07-08"))
    approve(service, "u1", second)
    with read_connection(db_path) as conn:
        (jd_parsed,) = conn.execute(
            "SELECT jd_parsed_json FROM applications WHERE company='字节'").fetchone()
    assert json.loads(jd_parsed)["skills"] == ["Python", "RAG", "Agent"]
