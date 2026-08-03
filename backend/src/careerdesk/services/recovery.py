"""Runtime-state recovery after process restart.

In-process background work cannot survive exit, so startup converts orphaned pending or
running state into visible retryable failure instead of leaving the UI polling forever.
"""

import json

from ..platform.database import loads_json, now_iso, transaction
from ..features.applications.public import recover_interrupted_intakes
from ..features.research.public import ResearchAttempt
from ..features.reviews.public import (
    recover_interrupted_review_record_operations_in_transaction,
)


def recover_interrupted_work(db_path: str) -> dict[str, int]:
    """Recover orphaned work and converge old proposals to latest import intent."""
    prep_count = 0
    review_record_count = 0
    grill_answer_count = 0
    with transaction(db_path) as conn:
        applications = conn.execute(
            "SELECT id, prep_generation, prep_json FROM applications "
            "WHERE prep_status IN ('pending', 'running')"
        ).fetchall()
        for application_id, prep_generation, prep_json in applications:
            loaded_prep = loads_json(prep_json, {})
            prep = loaded_prep if isinstance(loaded_prep, dict) else {}
            attempt = prep.get("research_attempt")
            if isinstance(attempt, dict) and attempt.get("attempt_state") in {
                "pending",
                "running",
            }:
                prep["research_attempt"] = ResearchAttempt.model_validate(
                    {
                        "attempt_state": "failed",
                        "generation": attempt.get("generation") or prep_generation,
                        "updated_time": now_iso(),
                        "error_code": "interrupted",
                    }
                ).model_dump(mode="json")
            prep["error"] = "上次调研生成因应用退出而中断，请重新生成"
            conn.execute(
                "UPDATE applications SET prep_status = 'failed', prep_generation = NULL, "
                "prep_heartbeat_time = NULL, prep_json = ?, updated_time = ? WHERE id = ?",
                (json.dumps(prep, ensure_ascii=False), now_iso(), application_id),
            )
            prep_count += 1

        review_record_recovery = (
            recover_interrupted_review_record_operations_in_transaction(conn)
        )
        review_record_count = sum(review_record_recovery.values())
        grill_answer_count = conn.execute(
            "UPDATE grill_session_items SET claim_token = NULL, claim_started_time = NULL, "
            "claim_error_code = 'outcome_unknown' "
            "WHERE claim_token IS NOT NULL",
        ).rowcount

    intake_count = recover_interrupted_intakes(db_path)
    from ..features.questions.public import recover_running_generations

    question_set_count = recover_running_generations(db_path)
    return {
        "preps": prep_count,
        "intakes": intake_count,
        "review_records": review_record_count,
        "question_sets": question_set_count,
        "grill_answers": grill_answer_count,
    }
