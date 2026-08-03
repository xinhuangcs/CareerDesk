"""Journal/Reviews tenant, revision, occurrence and atomic-command regressions."""

import asyncio
import json
import sqlite3
from uuid import uuid4

import pytest
from tests.support import ScriptedLLM
from pydantic import ValidationError

from careerdesk.platform.database import init_db, now_iso, read_connection, transaction
from careerdesk.features.applications.public import execute_application_update_operation
from careerdesk.features.journal.public import append_review, get_entry
from careerdesk.features.questions.repository import question_overview
from careerdesk.features.reviews.public import (
    ReviewConflict,
    ReviewExtractionUnavailable,
    ReviewRecordOperationConflict,
    ReviewRecordOperationNotFound,
    ReviewService,
    approve_review_operation,
    execute_review_timeline_entry_edit_operation,
    prepare_review_undo_operation,
)
from careerdesk.features.reviews.ai_models import (
    MAX_COMPANY_CHARS,
    MAX_FACTOR_CHARS,
    MAX_KNOWLEDGE_POINT_CHARS,
    MAX_QUESTION_CHARS,
    MAX_QUESTION_KNOWLEDGE_POINTS,
    MAX_REVIEW_FACTORS,
    MAX_REVIEW_BATCH_ITEMS,
    MAX_REVIEW_QUESTIONS,
    MAX_REVIEW_TOTAL_TEXT_CHARS,
    ReviewBatchExtraction,
    ReviewBatchIdentityManifest,
    ReviewExtraction,
)
from tests.review_record_test_helpers import derive_review_for_test
from tests.review_record_test_helpers import execute_review_record

TODAY = "2026-07-07"


def undo_review(db_path: str, user_id: str, journal_id: int) -> dict:
    proposal = prepare_review_undo_operation(
        db_path,
        user_id,
        journal_id=journal_id,
    )
    if proposal.get("status") == "not_found":
        return {"status": "error"}
    return approve_review_operation(
        db_path,
        user_id,
        proposal["operation_id"],
    )["result"]


def extraction(
    company: str = "字节",
    *,
    step: str = "一面",
    question: str | None = None,
) -> dict:
    return {
        "company": company,
        "position": "后端",
        "channel": None,
        "history": {
            "step": step,
            "date": TODAY,
            "outcome": None,
            "summary": f"{company}{step}",
        },
        "projected_state": {"stage": "interviewing", "current_step": step},
        "next_action": None,
        "questions": ([{"text": question, "stuck": False, "knowledge_points": []}]
                      if question else []),
        "mood": None,
        "time_of_day": None,
        "factors": [],
    }


def scripted(*payloads) -> ScriptedLLM:
    return ScriptedLLM([json.dumps(payload, ensure_ascii=False) for payload in payloads])


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "reviews-v10.db")
    init_db(path)
    return path


def record(db_path: str, user_id: str, payload: dict) -> int:
    created = append_review(db_path, user_id, payload["history"]["summary"])
    derive_review_for_test(db_path, user_id, created["id"], payload)
    return created["id"]


def test_derive_rejects_cross_tenant_and_wrong_owner_before_any_projection(db_path):
    created = append_review(db_path, "u1", "u1 原话")

    with pytest.raises(ReviewConflict):
        derive_review_for_test(db_path, "u2", created["id"], extraction("攻击者公司"))

    with read_connection(db_path) as conn:
        assert conn.execute("SELECT state, revision FROM journal WHERE id=?", (created["id"],)).fetchone() == (
            "pending", 0,
        )
        assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM timeline_entries").fetchone()[0] == 0


def test_supplement_rejects_jd_batch_cross_user_and_already_applied_review(db_path):
    with transaction(db_path) as conn:
        jd_batch = conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, state, extraction_json) "
            "VALUES ('u1', 'jd_batch', '岗位', ?, 'awaiting_user', '{\"positions\":[]}')",
            (now_iso(),),
        ).lastrowid
    applied = run(execute_review_record(
        ReviewService(db_path, scripted(extraction())),
        "u1",
        "已完成复盘",
        today=TODAY,
    ))
    service = ReviewService(db_path, scripted(extraction()))

    with pytest.raises(ValueError, match="review_reference 必须是 UUID"):
        run(execute_review_record(
            service, "u1", "补充", review_reference=str(jd_batch), today=TODAY,
        ))
    with pytest.raises(ReviewRecordOperationNotFound):
        run(execute_review_record(
            service,
            "u2",
            "越权",
            review_reference=applied["review_reference"],
            today=TODAY,
        ))
    with pytest.raises(ReviewRecordOperationConflict, match="当前不接受补充"):
        run(execute_review_record(
            service,
            "u1",
            "重复补充",
            review_reference=applied["review_reference"],
            today=TODAY,
        ))
    assert service._llm.calls == 0


def test_reversed_concurrent_supplements_publish_only_latest_revision(db_path):
    async def scenario():
        first_supplement_started = asyncio.Event()
        release_first = asyncio.Event()

        class BlockingSecondCallLLM(ScriptedLLM):
            async def chat(self, messages, *, tools=None, **kwargs):
                response = self._next()
                if self.calls == 2:
                    first_supplement_started.set()
                    await release_first.wait()
                return response

        missing_company = {**extraction(), "company": None}
        llm = BlockingSecondCallLLM([
            json.dumps(missing_company, ensure_ascii=False),
            json.dumps(extraction(step="一面"), ensure_ascii=False),
            json.dumps(extraction(step="二面"), ensure_ascii=False),
        ])
        service = ReviewService(db_path, llm)
        pending = await execute_review_record(service, "u1", "没说公司", today=TODAY)
        old_task = asyncio.create_task(
            execute_review_record(
                service,
                "u1",
                "第一次补充",
                review_reference=pending["review_reference"],
                today=TODAY,
            ),
        )
        await first_supplement_started.wait()
        latest = await execute_review_record(
            service,
            "u1",
            "第二次补充",
            review_reference=pending["review_reference"],
            today=TODAY,
        )
        release_first.set()
        old = await old_task
        return pending["target_journal_id"], old, latest

    journal_id, old, latest = run(scenario())
    assert latest["state"] == "completed" and latest["outcome"] == "applied"
    assert old["state"] == "superseded"
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT step FROM timeline_entries WHERE user_id='u1' AND journal_id=?",
            (journal_id,),
        ).fetchone() == ("二面",)
        state, revision, payload = conn.execute(
            "SELECT state, revision, extraction_json FROM journal WHERE id=?", (journal_id,)
        ).fetchone()
        assert conn.execute(
            "SELECT COUNT(*) FROM journal WHERE user_id='u1' AND parent_journal_id=? "
            "AND operation_id IS NULL",
            (journal_id,),
        ).fetchone()[0] == 2
    assert state == "applied"
    assert revision == latest["result"]["target_revision"]
    assert json.loads(payload)["history"]["step"] == "二面"


@pytest.mark.parametrize("undo_order", [(0, 1), (1, 0)])
def test_shared_question_archives_independent_of_undo_order_and_reactivates(db_path, undo_order):
    journal_ids = [
        record(db_path, "u1", extraction(company, question="共享题？"))
        for company in ("腾讯", "字节")
    ]
    assert question_overview(db_path, "u1", company="腾讯")["total"] == 1
    assert question_overview(db_path, "u1", company="字节")["total"] == 1

    assert undo_review(db_path, "u1", journal_ids[undo_order[0]])["status"] == "ok"
    remaining_company = "字节" if undo_order[0] == 0 else "腾讯"
    removed_company = "腾讯" if undo_order[0] == 0 else "字节"
    assert question_overview(db_path, "u1", company=remaining_company)["total"] == 1
    assert question_overview(db_path, "u1", company=removed_company)["total"] == 0
    assert undo_review(db_path, "u1", journal_ids[undo_order[1]])["status"] == "ok"
    with read_connection(db_path) as conn:
        assert conn.execute("SELECT status FROM questions WHERE text='共享题？'").fetchone() == (
            "archived",
        )

    record(db_path, "u1", extraction("阿里", question="共享题？"))
    with read_connection(db_path) as conn:
        assert conn.execute("SELECT status FROM questions WHERE text='共享题？'").fetchone() == (
            "active",
        )
    assert question_overview(db_path, "u1", company="阿里")["total"] == 1


def test_stale_replay_cannot_resurrect_voided_review(db_path):
    journal_id = record(db_path, "u1", extraction())
    stale = get_entry(db_path, "u1", journal_id)
    assert stale is not None
    assert undo_review(db_path, "u1", journal_id)["status"] == "ok"

    with pytest.raises(ReviewConflict):
        derive_review_for_test(
            db_path, "u1", journal_id, stale["extraction"], replay=True,
            expected_state="applied", expected_revision=stale["revision"],
        )
    with read_connection(db_path) as conn:
        assert conn.execute("SELECT state FROM journal WHERE id=?", (journal_id,)).fetchone() == (
            "voided",
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM timeline_entries WHERE journal_id=?",
            (journal_id,),
        ).fetchone()[0] == 0


def test_application_update_journal_failure_rolls_back_business_change(db_path):
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', '原公司', '原岗位', ?, ?)",
            (now_iso(), now_iso()),
        )
        conn.execute(
            "CREATE TRIGGER reject_correction BEFORE INSERT ON journal "
            "WHEN NEW.kind = 'correction' BEGIN SELECT RAISE(ABORT, 'journal unavailable'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        execute_application_update_operation(
            db_path,
            "u1",
            operation_id=uuid4(),
            client_turn_id=uuid4(),
            company="原公司",
            position="原岗位",
            changes={"position": "新岗位"},
        )
    with read_connection(db_path) as conn:
        assert conn.execute("SELECT position FROM applications WHERE user_id='u1'").fetchone() == (
            "原岗位",
        )
        assert conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 0


def test_failed_extraction_is_explicit_terminal_state(db_path):
    class FailingLLM(ScriptedLLM):
        async def chat(self, messages, *, tools=None, **kwargs):
            raise RuntimeError("provider down")

    with pytest.raises(RuntimeError, match="provider down"):
        run(execute_review_record(
            ReviewService(db_path, FailingLLM()), "u1", "原话", today=TODAY,
        ))
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT state, revision, processed_time FROM journal "
            "WHERE user_id='u1' AND kind='review'"
        ).fetchone() == ("failed", 1, None)
        operation_state, operation_revision, operation_processed = conn.execute(
            "SELECT state, revision, processed_time FROM journal "
            "WHERE user_id='u1' AND operation_id IS NOT NULL"
        ).fetchone()
    assert (operation_state, operation_revision) == ("failed", 1)
    assert operation_processed is not None


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("history", "step"), " "),
        (("history", "date"), "2026-02-30"),
        (("projected_state", "stage"), "unknown"),
        (("history", "outcome"), "maybe"),
    ],
)
def test_review_contract_rejects_invalid_steps_dates_stages_and_outcomes(path, value):
    payload = extraction()
    payload[path[0]][path[1]] = value
    with pytest.raises(ValidationError):
        ReviewExtraction.model_validate(payload)


def _string_schema(schema: dict) -> dict:
    if schema.get("type") == "string":
        return schema
    return next(item for item in schema["anyOf"] if item.get("type") == "string")


def test_review_contract_json_schema_exposes_structural_limits():
    schema = ReviewExtraction.model_json_schema()
    properties = schema["properties"]
    question_schema = schema["$defs"]["ExtractedQuestion"]

    assert schema["additionalProperties"] is False
    assert question_schema["additionalProperties"] is False
    assert properties["questions"]["maxItems"] == MAX_REVIEW_QUESTIONS
    assert question_schema["properties"]["knowledge_points"]["maxItems"] == (
        MAX_QUESTION_KNOWLEDGE_POINTS
    )
    assert properties["factors"]["maxItems"] == MAX_REVIEW_FACTORS
    assert _string_schema(properties["company"])["maxLength"] == MAX_COMPANY_CHARS
    assert question_schema["properties"]["text"]["maxLength"] == MAX_QUESTION_CHARS
    assert question_schema["properties"]["knowledge_points"]["items"]["maxLength"] == (
        MAX_KNOWLEDGE_POINT_CHARS
    )
    assert properties["factors"]["items"]["maxLength"] == MAX_FACTOR_CHARS


def test_review_batch_contract_accepts_fifty_items_and_rejects_fifty_one():
    item = {
        "source_text": "投递公司00的岗位00",
        "extraction": extraction("公司00"),
    }
    accepted = ReviewBatchExtraction.model_validate({
        "items": [
            {
                **item,
                "source_text": f"投递公司{index:02d}的岗位{index:02d}",
                "extraction": extraction(f"公司{index:02d}"),
            }
            for index in range(MAX_REVIEW_BATCH_ITEMS)
        ],
    })
    assert len(accepted.items) == 50
    with pytest.raises(ValidationError):
        ReviewBatchExtraction.model_validate({
            "items": [
                {
                    **item,
                    "source_text": f"投递公司{index:02d}的岗位{index:02d}",
                    "extraction": extraction(f"公司{index:02d}"),
                }
                for index in range(MAX_REVIEW_BATCH_ITEMS + 1)
            ],
        })


def test_review_batch_identity_manifest_requires_unique_nonempty_identities():
    accepted = ReviewBatchIdentityManifest.model_validate({"items": [
        {"source_text": "甲公司后端", "company": "甲公司", "position": "后端"},
        {"source_text": "乙公司前端", "company": "乙公司", "position": "前端"},
    ]})
    assert len(accepted.items) == 2

    with pytest.raises(ValidationError):
        ReviewBatchIdentityManifest.model_validate({"items": [
            {"source_text": "甲公司后端", "company": None, "position": None},
        ]})
    with pytest.raises(ValidationError):
        ReviewBatchIdentityManifest.model_validate({"items": [
            {"source_text": "甲公司后端", "company": "甲公司", "position": "后端"},
            {"source_text": "另一处甲公司后端", "company": "甲公司", "position": "后端"},
        ]})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"unexpected": "field"}),
        lambda payload: payload.update({"company": " "}),
        lambda payload: payload["history"].update({"summary": ""}),
        lambda payload: payload.update({"questions": [
            {"text": "题目", "stuck": False, "knowledge_points": [], "extra": "field"},
        ]}),
        lambda payload: payload.update({"questions": [
            {"text": f"题目 {index}", "stuck": False, "knowledge_points": []}
            for index in range(MAX_REVIEW_QUESTIONS + 1)
        ]}),
        lambda payload: payload.update({"questions": [{
            "text": "题目",
            "stuck": False,
            "knowledge_points": [
                f"知识点 {index}" for index in range(MAX_QUESTION_KNOWLEDGE_POINTS + 1)
            ],
        }]}),
        lambda payload: payload.update({
            "factors": [f"因素 {index}" for index in range(MAX_REVIEW_FACTORS + 1)],
        }),
    ],
)
def test_review_contract_rejects_extra_blank_and_unbounded_payloads(mutate):
    payload = extraction()
    mutate(payload)

    with pytest.raises(ValidationError):
        ReviewExtraction.model_validate(payload)


@pytest.mark.parametrize("date_value", ["20260707", "2026-W28-2", "2026-7-07", " 2026-07-07"])
def test_review_contract_rejects_noncanonical_iso_dates(date_value):
    payload = extraction()
    payload["history"]["date"] = date_value

    with pytest.raises(ValidationError):
        ReviewExtraction.model_validate(payload)


def test_review_contract_stably_normalizes_and_deduplicates_lists():
    payload = extraction()
    first_question = {
        "text": "  检查点恢复如何幂等？  ",
        "stuck": False,
        "knowledge_points": [" 检查点幂等 ", "检查点幂等", "重放"],
    }
    same_question_variant = {
        "text": "检查点恢复如何幂等？",
        "stuck": True,
        "knowledge_points": ["重放", "事务", "不会进入结果的第四项"],
    }
    payload["questions"] = [first_question, same_question_variant]
    payload["factors"] = [" 睡眠差 ", "睡眠差", "紧张", "睡眠差"]

    model = ReviewExtraction.model_validate(payload)

    assert len(model.questions) == 1
    assert model.questions[0].text == "检查点恢复如何幂等？"
    assert model.questions[0].stuck is True
    assert model.questions[0].knowledge_points == ["检查点幂等", "重放", "事务"]
    assert model.factors == ["睡眠差", "紧张"]


def test_same_question_variants_derive_at_most_three_knowledge_links(db_path):
    payload = extraction()
    payload["questions"] = [
        {
            "text": "同一道题？",
            "stuck": index == MAX_REVIEW_QUESTIONS - 1,
            "knowledge_points": [
                f"变体知识点 {index}-{point_index}"
                for point_index in range(MAX_QUESTION_KNOWLEDGE_POINTS)
            ],
        }
        for index in range(MAX_REVIEW_QUESTIONS)
    ]
    bounded = ReviewExtraction.model_validate(payload)
    assert len(bounded.questions) == 1
    assert bounded.questions[0].stuck is True
    assert len(bounded.questions[0].knowledge_points) == MAX_QUESTION_KNOWLEDGE_POINTS
    created = append_review(db_path, "u1", "同题变体")

    derive_review_for_test(db_path, "u1", created["id"], bounded.model_dump())

    with read_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM knowledge_points").fetchone()[0] == (
            MAX_QUESTION_KNOWLEDGE_POINTS
        )
        assert conn.execute("SELECT COUNT(*) FROM question_knowledge").fetchone()[0] == (
            MAX_QUESTION_KNOWLEDGE_POINTS
        )


def test_review_contract_rejects_total_structured_text_over_chat_budget():
    payload = extraction()
    question_count = MAX_REVIEW_TOTAL_TEXT_CHARS // MAX_QUESTION_CHARS + 1
    payload["questions"] = [
        {
            "text": f"Q{index:02d}" + "字" * (MAX_QUESTION_CHARS - 3),
            "stuck": False,
            "knowledge_points": [],
        }
        for index in range(question_count)
    ]

    with pytest.raises(ValidationError, match="结构化文本合计"):
        ReviewExtraction.model_validate(payload)


def test_validated_review_at_upper_bound_derives_bounded_projection(db_path):
    payload = extraction()
    payload["questions"] = [
        {
            "text": f"第 {question_index} 道题？",
            "stuck": False,
            "knowledge_points": [
                f"知识点 {question_index}-{point_index}"
                for point_index in range(MAX_QUESTION_KNOWLEDGE_POINTS)
            ],
        }
        for question_index in range(MAX_REVIEW_QUESTIONS)
    ]
    payload["factors"] = [f"因素 {index}" for index in range(MAX_REVIEW_FACTORS)]
    bounded = ReviewExtraction.model_validate(payload).model_dump()
    created = append_review(db_path, "u1", "上界复盘")

    derive_review_for_test(db_path, "u1", created["id"], bounded)

    with read_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM timeline_entries").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == MAX_REVIEW_QUESTIONS
        assert conn.execute("SELECT COUNT(*) FROM knowledge_points").fetchone()[0] == (
            MAX_REVIEW_QUESTIONS * MAX_QUESTION_KNOWLEDGE_POINTS
        )
        assert conn.execute("SELECT COUNT(*) FROM question_knowledge").fetchone()[0] == (
            MAX_REVIEW_QUESTIONS * MAX_QUESTION_KNOWLEDGE_POINTS
        )
        assert conn.execute("SELECT COUNT(*) FROM status_log").fetchone()[0] == 1


def test_oversized_llm_review_fails_before_any_business_projection(db_path):
    payload = extraction()
    payload["questions"] = [
        {"text": f"第 {index} 道题？", "stuck": False, "knowledge_points": []}
        for index in range(MAX_REVIEW_QUESTIONS + 1)
    ]
    service = ReviewService(db_path, scripted(payload, payload))

    with pytest.raises(ReviewExtractionUnavailable):
        run(execute_review_record(service, "u1", "恶意超大提取", today=TODAY))

    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT state FROM journal WHERE user_id='u1' AND kind='review'"
        ).fetchone() == ("failed",)
        for table in (
            "applications",
            "timeline_entries",
            "questions",
            "knowledge_points",
            "question_knowledge",
            "status_log",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_manual_review_edit_uses_same_step_and_date_contract(db_path):
    journal_id = record(db_path, "u1", extraction())
    for changes in ({"step": " "}, {"occurred_date": "2026-02-30"}):
        with pytest.raises(ValueError):
            execute_review_timeline_entry_edit_operation(
                db_path,
                "u1",
                operation_id=uuid4(),
                client_turn_id=uuid4(),
                company="字节",
                position="后端",
                changes=changes,
            )
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT step, occurred_date FROM timeline_entries WHERE journal_id=?",
            (journal_id,),
        ).fetchone() == (
            "一面", "2026-07-07",
        )
