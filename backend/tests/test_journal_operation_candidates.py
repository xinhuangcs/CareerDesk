"""Journal-owned broad operation candidate reader contract."""

from dataclasses import FrozenInstanceError
import json
from uuid import uuid4

import pytest

from careerdesk.platform.database import init_db, read_connection, transaction
from careerdesk.platform.database.connection import now_iso
from careerdesk.features.journal import public as journal
from careerdesk.features.journal import repository as journal_repository


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "operation-candidates.db")
    init_db(path)
    return path


def _receipt(
    turn_id: str,
    *,
    extraction: bool,
    family: str = "application_update",
) -> str:
    if extraction:
        return json.dumps({
            "operation_type": family,
            "client_turn_id": turn_id,
        })
    return json.dumps({"operation": {
        "type": family,
        "client_turn_id": turn_id,
    }})


def _insert(
    conn,
    *,
    user_id: str,
    extraction_json: str,
    derivation_json: str,
    kind: str = "correction",
    state: str = "applied",
) -> tuple[int, str]:
    operation_id = str(uuid4())
    journal_id = conn.execute(
        "INSERT INTO journal "
        "(user_id, kind, content, created_time, extraction_json, derivation_json, "
        "state, operation_id) VALUES (?, ?, '', ?, ?, ?, ?, ?)",
        (
            user_id,
            kind,
            now_iso(),
            extraction_json,
            derivation_json,
            state,
            operation_id,
        ),
    ).lastrowid
    return journal_id, operation_id


def test_reader_uses_both_indexes_deduplicates_and_keeps_raw_order(db_path):
    turn_id = str(uuid4())
    other_turn = str(uuid4())
    expected = []
    with transaction(db_path) as conn:
        expected.append(_insert(
            conn,
            user_id="u1",
            extraction_json=_receipt(turn_id, extraction=True),
            derivation_json=_receipt(turn_id, extraction=False),
        ))
        expected.append(_insert(
            conn,
            user_id="u1",
            extraction_json=_receipt(turn_id, extraction=True),
            derivation_json=_receipt(other_turn, extraction=False),
        ))
        expected.append(_insert(
            conn,
            user_id="u1",
            extraction_json=_receipt(other_turn, extraction=True),
            derivation_json=_receipt(turn_id, extraction=False),
        ))
        expected.append(_insert(
            conn,
            user_id="u1",
            extraction_json=_receipt(
                turn_id,
                extraction=True,
                family="future_operation",
            ),
            derivation_json=_receipt(
                turn_id,
                extraction=False,
                family="future_operation",
            ),
        ))
        expected.append(_insert(
            conn,
            user_id="u1",
            extraction_json=_receipt(turn_id, extraction=True),
            derivation_json="{",
            kind="review",
            state="pending",
        ))
        _insert(
            conn,
            user_id="u2",
            extraction_json=_receipt(turn_id, extraction=True),
            derivation_json=_receipt(turn_id, extraction=False),
        )
        _insert(
            conn,
            user_id="u1",
            extraction_json="{",
            derivation_json="{",
        )
        conn.execute(
            "INSERT INTO journal "
            "(user_id, kind, content, created_time, extraction_json, state) "
            "VALUES (?, 'correction', '', ?, ?, 'applied')",
            ("u1", now_iso(), _receipt(turn_id, extraction=True)),
        )

    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        before_changes = conn.total_changes
        plan = conn.execute(
            "EXPLAIN QUERY PLAN " + journal_repository._OPERATION_CANDIDATE_SQL,
            ("u1", turn_id, "u1", turn_id, "u1", 129),
        ).fetchall()
        candidates = journal.read_operation_candidates_for_turn_in_transaction(
            conn,
            "u1",
            turn_id,
            maximum=128,
        )
        assert conn.total_changes == before_changes

    details = "\n".join(str(row[3]) for row in plan)
    assert "idx_journal_operation_turn_extraction" in details
    assert "idx_journal_operation_turn_derivation" in details
    assert [candidate.journal_id for candidate in candidates] == [
        journal_id for journal_id, _operation_id in expected
    ]
    assert [candidate.operation_id for candidate in candidates] == [
        operation_id for _journal_id, operation_id in expected
    ]
    assert candidates[0].extraction_json == _receipt(turn_id, extraction=True)
    assert candidates[0].derivation_json == _receipt(turn_id, extraction=False)
    assert tuple(candidates[0].__dataclass_fields__) == (
        "journal_id",
        "operation_id",
        "extraction_json",
        "derivation_json",
        "kind",
    )
    assert isinstance(candidates, tuple)
    with pytest.raises(FrozenInstanceError):
        candidates[0].operation_id = "damaged"


def test_reader_returns_one_over_budget_and_rejects_unsafe_maximum(db_path):
    turn_id = str(uuid4())
    with transaction(db_path) as conn:
        for _index in range(3):
            _insert(
                conn,
                user_id="u1",
                extraction_json=_receipt(turn_id, extraction=True),
                derivation_json=_receipt(turn_id, extraction=False),
            )

    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        candidates = journal.read_operation_candidates_for_turn_in_transaction(
            conn,
            "u1",
            turn_id,
            maximum=1,
        )
        assert len(candidates) == 2
        for maximum in (False, 0, 129):
            with pytest.raises(ValueError, match="maximum"):
                journal.read_operation_candidates_for_turn_in_transaction(
                    conn,
                    "u1",
                    turn_id,
                    maximum=maximum,
                )


def test_reader_uses_callers_transaction_without_committing(db_path):
    turn_id = str(uuid4())
    journal_id = None
    with pytest.raises(RuntimeError, match="force rollback"):
        with transaction(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            journal_id, operation_id = _insert(
                conn,
                user_id="u1",
                extraction_json=_receipt(turn_id, extraction=True),
                derivation_json=_receipt(turn_id, extraction=False),
            )
            before_changes = conn.total_changes
            candidates = journal.read_operation_candidates_for_turn_in_transaction(
                conn,
                "u1",
                turn_id,
                maximum=1,
            )
            assert [candidate.operation_id for candidate in candidates] == [operation_id]
            assert conn.in_transaction
            assert conn.total_changes == before_changes
            raise RuntimeError("force rollback")

    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM journal WHERE id = ?",
            (journal_id,),
        ).fetchone() is None
