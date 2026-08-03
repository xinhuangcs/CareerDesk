"""Trusted batch-intake operation persistence, planning, approval, and recovery."""

import json
from collections.abc import Callable
from hashlib import sha256
from sqlite3 import Connection
from uuid import UUID, uuid4

from ...companies import public as companies
from ....platform.database import (
    application_identity_key,
    loads_json,
    normalize_application_identity_part,
    now_iso,
    read_connection,
    transaction,
)
from ..intake_models import (
    INTAKE_CONTRACT_VERSION,
    MAX_BATCH_POSITIONS,
    IntakeEffect,
    IntakeFlags,
    IntakeProposal,
    IntakeProposalPosition,
    ParsedPosition,
    public_intake_position,
)
from .prep import _invalidate_prep
from .shared import (
    STAGE_LABELS,
    IntakeOperationConflict,
    IntakeOperationInvalidSelection,
    IntakeOperationNotFound,
)


_INTAKE_OWNER_TABLE = "application_intake_operation_owners"
_INTAKE_IDENTITY_COLUMNS = (
    "journal.id, journal.user_id, journal.kind, journal.operation_id, "
    "journal.created_time, journal.state, journal.extraction_json IS NULL, journal.revision, "
    "owner.journal_id, owner.user_id, owner.operation_id, owner.created_time"
)
_INTAKE_IDENTITY_KEYS = (
    "journal_id",
    "journal_user_id",
    "kind",
    "journal_operation_id",
    "journal_created_time",
    "state",
    "extraction_is_null",
    "revision",
    "owner_journal_id",
    "owner_user_id",
    "owner_operation_id",
    "owner_created_time",
)


def _persisted_intake_operation_id(value) -> str | None:
    canonical = None
    if isinstance(value, str):
        try:
            canonical = str(UUID(value))
        except (AttributeError, TypeError, ValueError):
            pass
    return value if isinstance(value, str) and value == canonical else None


def _validate_intake_identity(
    row,
    *,
    user_id: str | None = None,
    journal_id: int | None = None,
    operation_id: str | None = None,
) -> dict:
    if row is None:
        raise IntakeOperationConflict("批量导入 operation owner 身份缺失")
    identity = dict(zip(_INTAKE_IDENTITY_KEYS, tuple(row), strict=True))
    journal_operation = _persisted_intake_operation_id(
        identity["journal_operation_id"],
    )
    owner_operation = _persisted_intake_operation_id(identity["owner_operation_id"])
    created_time = identity["journal_created_time"]
    if (
        isinstance(identity["journal_id"], bool)
        or not isinstance(identity["journal_id"], int)
        or identity["journal_id"] < 1
        or identity["kind"] != "jd_batch"
        or journal_operation is None
        or owner_operation is None
        or journal_operation != owner_operation
        or identity["owner_journal_id"] != identity["journal_id"]
        or identity["owner_user_id"] != identity["journal_user_id"]
        or not isinstance(created_time, str)
        or not created_time
        or len(created_time) > 64
        or created_time != created_time.strip()
        or identity["owner_created_time"] != created_time
        or identity["extraction_is_null"] not in (0, 1)
        or isinstance(identity["revision"], bool)
        or not isinstance(identity["revision"], int)
        or identity["revision"] < 0
        or (user_id is not None and identity["journal_user_id"] != user_id)
        or (journal_id is not None and identity["journal_id"] != journal_id)
        or (operation_id is not None and journal_operation != operation_id)
    ):
        raise IntakeOperationConflict("批量导入 operation owner 身份已损坏")
    return identity


def _assert_intake_owner_integrity(
    conn: Connection,
    user_id: str | None = None,
) -> None:
    user_clause = "" if user_id is None else "AND journal.user_id = ? "
    parameters: tuple = () if user_id is None else (user_id,)
    unowned = conn.execute(
        "SELECT journal.id FROM journal "
        f"LEFT JOIN {_INTAKE_OWNER_TABLE} owner ON owner.journal_id = journal.id "
        "WHERE journal.kind = 'jd_batch' AND owner.journal_id IS NULL "
        f"{user_clause}LIMIT 1",
        parameters,
    ).fetchone()
    if unowned is not None:
        raise IntakeOperationConflict("批量导入 operation owner 身份缺失")

    mismatch_predicate = (
        "(journal.user_id IS NOT owner.user_id "
        "OR journal.kind IS NOT 'jd_batch' "
        "OR journal.operation_id IS NOT owner.operation_id "
        "OR journal.created_time IS NOT owner.created_time)"
    )
    if user_id is None:
        mismatch = conn.execute(
            f"SELECT owner.journal_id FROM {_INTAKE_OWNER_TABLE} owner "
            "JOIN journal ON journal.id = owner.journal_id WHERE "
            f"{mismatch_predicate} LIMIT 1",
        ).fetchone()
        mismatches = (mismatch,)
    else:
        # Split the two tenant scopes so SQLite can use each table's user prefix
        # index. An OR here degrades this hot-path guard into a global owner scan.
        owner_mismatch = conn.execute(
            f"SELECT owner.journal_id FROM {_INTAKE_OWNER_TABLE} owner "
            "JOIN journal ON journal.id = owner.journal_id "
            f"WHERE owner.user_id = ? AND {mismatch_predicate} LIMIT 1",
            (user_id,),
        ).fetchone()
        journal_mismatch = conn.execute(
            f"SELECT owner.journal_id FROM journal INDEXED BY idx_journal_user_kind_time "
            f"JOIN {_INTAKE_OWNER_TABLE} owner ON owner.journal_id = journal.id "
            f"WHERE journal.user_id = ? AND {mismatch_predicate} LIMIT 1",
            (user_id,),
        ).fetchone()
        mismatches = (owner_mismatch, journal_mismatch)
    if any(mismatch is not None for mismatch in mismatches):
        raise IntakeOperationConflict("批量导入 operation owner 身份已损坏")


def _intake_identity_by_journal(
    conn: Connection,
    user_id: str,
    journal_id: int,
) -> dict | None:
    row = conn.execute(
        f"SELECT {_INTAKE_IDENTITY_COLUMNS} FROM journal "
        f"LEFT JOIN {_INTAKE_OWNER_TABLE} owner ON owner.journal_id = journal.id "
        "WHERE journal.user_id = ? AND journal.id = ?",
        (user_id, journal_id),
    ).fetchone()
    if row is None:
        return None
    return _validate_intake_identity(row, user_id=user_id, journal_id=journal_id)


def _latest_intake_identity(conn: Connection, user_id: str) -> dict | None:
    row = conn.execute(
        f"SELECT {_INTAKE_IDENTITY_COLUMNS} FROM {_INTAKE_OWNER_TABLE} owner "
        "JOIN journal ON journal.id = owner.journal_id "
        "WHERE owner.user_id = ? ORDER BY owner.journal_id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return None if row is None else _validate_intake_identity(row, user_id=user_id)


def _active_intake_identities(
    conn: Connection,
    user_id: str,
    *,
    before_journal_id: int | None = None,
) -> list[dict]:
    before = "" if before_journal_id is None else "AND owner.journal_id < ? "
    parameters: tuple = (user_id,)
    if before_journal_id is not None:
        parameters = (user_id, before_journal_id)
    rows = conn.execute(
        f"SELECT {_INTAKE_IDENTITY_COLUMNS} FROM {_INTAKE_OWNER_TABLE} owner "
        "JOIN journal ON journal.id = owner.journal_id "
        "WHERE owner.user_id = ? AND journal.state IN ('pending', 'awaiting_user') "
        f"{before}ORDER BY owner.journal_id",
        parameters,
    ).fetchall()
    return [_validate_intake_identity(row, user_id=user_id) for row in rows]


def _supersede_intake_identities(
    conn: Connection,
    identities: list[dict],
    *,
    superseded_by: int,
    reason: str | None = None,
) -> None:
    derivation = {"superseded_by": superseded_by}
    if reason is not None:
        derivation["reason"] = reason
    encoded = json.dumps(derivation, ensure_ascii=False)
    for identity in identities:
        changed = conn.execute(
            "UPDATE journal SET processed_time = NULL, derivation_json = ?, "
            "state = 'superseded', revision = revision + 1 "
            "WHERE user_id = ? AND id = ? AND kind = 'jd_batch' "
            "AND state IN ('pending', 'awaiting_user') AND revision = ?",
            (
                encoded,
                identity["journal_user_id"],
                identity["journal_id"],
                identity["revision"],
            ),
        ).rowcount
        if changed != 1:
            raise IntakeOperationConflict("批量导入 owner 状态已变化")


def create_intake_batch(db_path: str, user_id: str, content: str) -> tuple[int, str]:
    """Persist one role paste and issue an operation ID never supplied by a model."""
    operation_id = str(uuid4())
    created_time = now_iso()
    with transaction(db_path) as conn:
        _assert_intake_owner_integrity(conn, user_id)
        cursor = conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, state, operation_id) "
            "VALUES (?, 'jd_batch', ?, ?, 'pending', ?)",
            (user_id, content, created_time, operation_id),
        )
        conn.execute(
            f"INSERT INTO {_INTAKE_OWNER_TABLE} "
            "(journal_id, user_id, operation_id, created_time) VALUES (?, ?, ?, ?)",
            (cursor.lastrowid, user_id, operation_id, created_time),
        )
        return cursor.lastrowid, operation_id


_DEPENDENCY_COLUMNS = (
    "id, company, position, department, channel, jd_text, jd_parsed_json, stage, "
    "current_step, current_state_entry_id, applied_date, next_stage, next_step, "
    "next_date, next_time, next_note, paused_from_stage, pause_reason, application_note, "
    "priority, revision"
)


def _dependency_snapshot(row) -> dict:
    values = tuple(row)
    return dict(zip(_DEPENDENCY_COLUMNS.split(", "), values, strict=True))


def _dependency_fingerprint(snapshot: dict) -> str:
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _parsed_jd_parts(raw: str | None) -> tuple[list[str], list[str]]:
    parsed = loads_json(raw, {})
    if not isinstance(parsed, dict):
        return [], []
    skills = parsed.get("skills", [])
    highlights = parsed.get("highlights", [])
    return (
        skills if isinstance(skills, list) else [],
        highlights if isinstance(highlights, list) else [],
    )


def _plan_intake_position(current: dict | None,
                          parsed: ParsedPosition) -> IntakeProposalPosition:
    """Turn parsed source and dependency snapshots into immutable effects and flags."""
    source = parsed.model_dump()
    incoming_has_parse = bool(parsed.skills or parsed.highlights)
    if current is None:
        effect = IntakeEffect(
            company=parsed.company,
            position=parsed.position,
            department=parsed.department,
            channel=parsed.channel,
            jd_text=parsed.jd_text,
            skills=parsed.skills,
            highlights=parsed.highlights,
            stage=parsed.stage or ("applied" if parsed.applied_date else "backlog"),
            current_step=parsed.current_step,
            applied_date=parsed.applied_date,
            pause_reason=parsed.pause_reason,
            next_action=parsed.next_action,
            application_note=parsed.application_note,
            priority=parsed.priority,
        )
        if effect.stage in {"rejected", "withdrawn"}:
            effect = effect.model_copy(update={"next_action": None})
        if effect.stage != "pooled":
            effect = effect.model_copy(update={"pause_reason": None})
        flags = IntakeFlags(
            invalidate_prep=False,
            add_applied_entry=(
                parsed.applied_date is not None and effect.stage == "applied"
            ),
            clear_next_action=parsed.next_action is not None and effect.next_action is None,
        )
        binding = {"mode": "create", "must_be_absent": True}
    else:
        existing_skills, existing_highlights = _parsed_jd_parts(current["jd_parsed_json"])
        current_next_action = (
            {
                "stage": current["next_stage"],
                "step": current["next_step"],
                "date": current["next_date"],
                "time": current["next_time"],
                "note": current["next_note"],
            }
            if current["next_step"] is not None else None
        )
        jd_changed = (
            parsed.jd_text is not None and parsed.jd_text != current["jd_text"]
        ) or (
            incoming_has_parse
            and (parsed.skills != existing_skills
                 or parsed.highlights != existing_highlights)
        )
        effect = IntakeEffect(
            # Preserve stored display text when whitespace-normalized identity
            # matches; spacing differences must not rewrite a natural key.
            company=current["company"],
            position=current["position"],
            department=parsed.department if parsed.department is not None else current["department"],
            channel=parsed.channel if parsed.channel is not None else current["channel"],
            jd_text=parsed.jd_text if parsed.jd_text is not None else current["jd_text"],
            skills=parsed.skills if incoming_has_parse else existing_skills,
            highlights=parsed.highlights if incoming_has_parse else existing_highlights,
            stage=(
                parsed.stage
                or ("applied" if current["stage"] == "backlog" and parsed.applied_date
                    else current["stage"])
            ),
            current_step=(
                parsed.current_step
                if parsed.current_step is not None else current["current_step"]
            ),
            applied_date=current["applied_date"] or parsed.applied_date,
            pause_reason=(
                parsed.pause_reason if parsed.pause_reason is not None else current["pause_reason"]
            ),
            next_action=(
                parsed.next_action
                if parsed.next_action is not None else current_next_action
            ),
            application_note=(
                parsed.application_note
                if parsed.application_note is not None else current["application_note"]
            ),
            priority=(parsed.priority if parsed.priority is not None else current["priority"]),
        )
        if effect.stage in {"rejected", "withdrawn"}:
            effect = effect.model_copy(update={"next_action": None})
        if effect.stage != "pooled":
            effect = effect.model_copy(update={"pause_reason": None})
        add_applied = (
            parsed.applied_date is not None
            and current["applied_date"] is None
            and effect.stage == "applied"
        )
        flags = IntakeFlags(
            invalidate_prep=jd_changed,
            add_applied_entry=add_applied,
            clear_next_action=(current_next_action is not None and effect.next_action is None),
        )
        binding = {
            "mode": "update",
            "application_id": current["id"],
            "dependency_fingerprint": _dependency_fingerprint(current),
        }
    return IntakeProposalPosition(
        source=source,
        binding=binding,
        effect=effect,
        flags=flags,
    )


def activate_intake_proposal(
    db_path: str,
    user_id: str,
    journal_id: int,
    positions: list[ParsedPosition],
    *,
    source_rows: int = 0,
    skipped_rows: int = 0,
    proposal_recorder: Callable[[Connection, str, str], object] | None = None,
) -> list[dict] | None:
    """Plan and publish the latest proposal in one immediate snapshot."""
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _assert_intake_owner_integrity(conn, user_id)
        identity = _intake_identity_by_journal(conn, user_id, journal_id)
        if identity is None:
            return None
        latest = _latest_intake_identity(conn, user_id)
        if latest is None:  # pragma: no cover - validated identity has an owner row
            raise IntakeOperationConflict("批量导入 owner 最新意图缺失")
        newest = latest["journal_id"]
        if (
            identity["state"] != "pending"
            or not identity["extraction_is_null"]
            or newest != journal_id
        ):
            if newest > journal_id and identity["state"] in {"pending", "awaiting_user"}:
                _supersede_intake_identities(
                    conn,
                    [identity],
                    superseded_by=newest,
                )
            return None

        older_active = _active_intake_identities(
            conn,
            user_id,
            before_journal_id=journal_id,
        )

        planned: list[IntakeProposalPosition] = []
        for parsed in positions:
            current_row = conn.execute(
                f"SELECT {_DEPENDENCY_COLUMNS} FROM applications "
                "WHERE user_id = ? AND company_key = ? AND position_key = ?",
                (user_id, *application_identity_key(parsed.company, parsed.position)),
            ).fetchone()
            current = _dependency_snapshot(current_row) if current_row is not None else None
            planned.append(_plan_intake_position(current, parsed))
        proposal = IntakeProposal(
            intake_contract_version=INTAKE_CONTRACT_VERSION,
            positions=planned,
            source_rows=source_rows,
            skipped_rows=skipped_rows,
        )
        activated = conn.execute(
            "UPDATE journal SET extraction_json = ?, processed_time = NULL, "
            "derivation_json = NULL, state = 'awaiting_user', revision = revision + 1 "
            "WHERE user_id = ? AND id = ? AND kind = 'jd_batch' "
            "AND state = 'pending' AND extraction_json IS NULL AND revision = ? "
            f"AND EXISTS (SELECT 1 FROM {_INTAKE_OWNER_TABLE} owner "
            "WHERE owner.journal_id = journal.id AND owner.user_id = journal.user_id "
            "AND owner.operation_id = journal.operation_id "
            "AND owner.created_time = journal.created_time)",
            (
                json.dumps(proposal.model_dump(), ensure_ascii=False),
                user_id,
                journal_id,
                identity["revision"],
            ),
        ).rowcount
        if activated != 1:
            raise IntakeOperationConflict("批量导入 owner 激活状态已变化")
        _supersede_intake_identities(
            conn,
            older_active,
            superseded_by=journal_id,
        )
        if proposal_recorder is not None:
            proposal_recorder(
                conn,
                "intake",
                identity["journal_operation_id"],
            )
        return [public_intake_position(position) for position in proposal.positions]


def fail_intake_batch(db_path: str, user_id: str, journal_id: int, *,
                      reason: str = "parse_failed") -> None:
    """Close parsing; a latest failed intent still supersedes older proposals."""
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _assert_intake_owner_integrity(conn, user_id)
        identity = _intake_identity_by_journal(conn, user_id, journal_id)
        if identity is None:
            return
        latest = _latest_intake_identity(conn, user_id)
        if latest is None:  # pragma: no cover - validated identity has an owner row
            raise IntakeOperationConflict("批量导入 owner 最新意图缺失")
        newest = latest["journal_id"]
        if newest == journal_id:
            if identity["state"] != "pending" or not identity["extraction_is_null"]:
                return
            older_active = _active_intake_identities(
                conn,
                user_id,
                before_journal_id=journal_id,
            )
            failed = conn.execute(
                "UPDATE journal SET processed_time = NULL, derivation_json = ?, "
                "state = 'failed', revision = revision + 1 "
                "WHERE user_id = ? AND id = ? AND kind = 'jd_batch' "
                "AND state = 'pending' AND extraction_json IS NULL AND revision = ?",
                (
                    json.dumps({"intake_failure": {"reason": reason}}, ensure_ascii=False),
                    user_id,
                    journal_id,
                    identity["revision"],
                ),
            ).rowcount
            if failed != 1:
                raise IntakeOperationConflict("批量导入 owner 失败状态已变化")
            _supersede_intake_identities(
                conn,
                older_active,
                superseded_by=journal_id,
            )
            return
        if newest > journal_id and identity["state"] in {"pending", "awaiting_user"}:
            _supersede_intake_identities(
                conn,
                [identity],
                superseded_by=newest,
            )


def recover_interrupted_intakes(db_path: str) -> int:
    """Fail orphaned parsing work and supersede previews by latest intent at startup."""
    intake_count = 0
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _assert_intake_owner_integrity(conn)
        rows = conn.execute(
            f"SELECT {_INTAKE_IDENTITY_COLUMNS} FROM {_INTAKE_OWNER_TABLE} owner "
            "JOIN journal ON journal.id = owner.journal_id ORDER BY owner.journal_id",
        ).fetchall()
        identities = [_validate_intake_identity(row) for row in rows]
        active = [
            identity for identity in identities
            if identity["state"] in {"pending", "awaiting_user"}
        ]
        latest_by_user: dict[str, int] = {}
        for identity in identities:
            latest_by_user[identity["journal_user_id"]] = identity["journal_id"]
        interrupted = json.dumps(
            {"intake_failure": {"reason": "application_restart_interrupted"}},
            ensure_ascii=False,
        )
        for identity in active:
            if identity["state"] != "pending":
                continue
            changed = conn.execute(
                "UPDATE journal SET processed_time = NULL, derivation_json = ?, "
                "state = 'failed', revision = revision + 1 "
                "WHERE id = ? AND user_id = ? AND kind = 'jd_batch' "
                "AND state = 'pending' AND revision = ?",
                (
                    interrupted,
                    identity["journal_id"],
                    identity["journal_user_id"],
                    identity["revision"],
                ),
            ).rowcount
            if changed != 1:
                raise IntakeOperationConflict("批量导入恢复状态已变化")
            intake_count += changed

        for identity in active:
            newest = latest_by_user[identity["journal_user_id"]]
            if identity["state"] != "awaiting_user" or identity["journal_id"] >= newest:
                continue
            changed = conn.execute(
                "UPDATE journal SET processed_time = NULL, derivation_json = ?, "
                "state = 'superseded', revision = revision + 1 "
                "WHERE id = ? AND user_id = ? AND kind = 'jd_batch' "
                "AND state = 'awaiting_user' AND revision = ?",
                (json.dumps(
                    {"superseded_by": newest, "reason": "newer_intake_intent"},
                    ensure_ascii=False,
                ), identity["journal_id"], identity["journal_user_id"],
                    identity["revision"]),
            ).rowcount
            if changed != 1:
                raise IntakeOperationConflict("批量导入恢复状态已变化")
    return intake_count


_INTAKE_OPERATION_COLUMNS = (
    "journal.id, journal.operation_id, journal.state, journal.created_time, "
    "journal.extraction_json, journal.derivation_json, "
    f"EXISTS (SELECT 1 FROM {_INTAKE_OWNER_TABLE} newer "
    "WHERE newer.user_id = owner.user_id AND newer.journal_id > owner.journal_id), "
    "journal.user_id, journal.kind, journal.revision, "
    "owner.journal_id, owner.user_id, owner.operation_id, owner.created_time"
)


def _canonical_operation_id(operation_id: str | UUID) -> str:
    """Accept only UUID identity; malformed and cross-tenant values appear absent."""
    try:
        return str(UUID(str(operation_id)))
    except (AttributeError, TypeError, ValueError) as error:
        raise IntakeOperationNotFound("批量导入操作不存在") from error


def _operation_row(conn: Connection, user_id: str, operation_id: str):
    rows = conn.execute(
        f"SELECT {_INTAKE_OPERATION_COLUMNS} FROM journal "
        f"LEFT JOIN {_INTAKE_OWNER_TABLE} owner ON owner.journal_id = journal.id "
        "WHERE journal.user_id = ? AND journal.operation_id = ? UNION "
        f"SELECT {_INTAKE_OPERATION_COLUMNS} FROM {_INTAKE_OWNER_TABLE} owner "
        "JOIN journal ON journal.id = owner.journal_id "
        "WHERE owner.user_id = ? AND owner.operation_id = ? LIMIT 2",
        (user_id, operation_id, user_id, operation_id),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise IntakeOperationConflict("批量导入 operation owner 身份冲突")
    _validate_intake_operation_row(
        rows[0],
        user_id=user_id,
        operation_id=operation_id,
    )
    return rows[0]


def _validate_intake_operation_row(
    row,
    *,
    user_id: str | None = None,
    operation_id: str | None = None,
) -> dict:
    if row is None or len(row) != 14:
        raise IntakeOperationConflict("批量导入 operation owner 身份缺失")
    identity = _validate_intake_identity(
        (
            row[0],
            row[7],
            row[8],
            row[1],
            row[3],
            row[2],
            int(row[4] is None),
            row[9],
            row[10],
            row[11],
            row[12],
            row[13],
        ),
        user_id=user_id,
        operation_id=operation_id,
    )
    if row[6] not in (0, 1):
        raise IntakeOperationConflict("批量导入 owner newer 身份已损坏")
    return identity


def _operation_dto(row) -> dict:
    """Project the journal lifecycle into the stable public operation contract."""
    _validate_intake_operation_row(row)
    _, operation_id, journal_state, created_time, extraction_json, derivation_json, newer = (
        row[:7]
    )
    extraction = loads_json(extraction_json, {})
    derivation = loads_json(derivation_json, {})
    try:
        proposal = IntakeProposal.model_validate(extraction)
    except (TypeError, ValueError):
        proposal = None
    positions = (
        [public_intake_position(position) for position in proposal.positions]
        if proposal is not None else []
    )
    operation = derivation.get("operation", {}) if isinstance(derivation, dict) else {}

    if journal_state == "awaiting_user" and proposal is not None and not newer:
        state = "pending"
    elif (journal_state == "applied" and proposal is not None
          and operation.get("action") == "approve"):
        state = "completed"
    elif (journal_state == "voided" and proposal is not None
          and operation.get("action") == "reject"):
        state = "rejected"
    else:
        state = "stale"
    terminal = state in {"completed", "rejected"}
    return {
        "operation_id": operation_id,
        "state": state,
        "created_time": created_time,
        "positions": positions,
        "source_rows": proposal.source_rows if proposal is not None else 0,
        "skipped_rows": proposal.skipped_rows if proposal is not None else 0,
        "exclude_indexes": operation.get("exclude_indexes") if state == "completed" else None,
        "result": operation.get("result") if terminal else None,
    }


def list_pending_intake_operations(db_path: str, user_id: str) -> list[dict]:
    """List trusted role previews that the current user may approve."""
    with read_connection(db_path) as conn:
        _assert_intake_owner_integrity(conn, user_id)
        rows = conn.execute(
            f"SELECT {_INTAKE_OPERATION_COLUMNS} FROM {_INTAKE_OWNER_TABLE} owner "
            "JOIN journal ON journal.id = owner.journal_id "
            "WHERE owner.user_id = ? AND journal.state = 'awaiting_user' "
            "AND journal.extraction_json IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {_INTAKE_OWNER_TABLE} newer "
            "WHERE newer.user_id = owner.user_id "
            "AND newer.journal_id > owner.journal_id) "
            "ORDER BY owner.journal_id DESC",
            (user_id,),
        ).fetchall()
    operations = [_operation_dto(row) for row in rows]
    return [operation for operation in operations if operation["state"] == "pending"]


def get_intake_operation(db_path: str, user_id: str,
                         operation_id: str | UUID) -> dict | None:
    """Read batch intake by server ID without revealing cross-user existence."""
    canonical_id = _canonical_operation_id(operation_id)
    with read_connection(db_path) as conn:
        _assert_intake_owner_integrity(conn, user_id)
        row = _operation_row(conn, user_id, canonical_id)
    return _operation_dto(row) if row is not None else None


def find_applications_by_company(
    db_path: str,
    user_id: str,
    company: str,
    *,
    fuzzy: bool = False,
    position: str | None = None,
) -> list[dict]:
    """Match natural role keys with optional fuzzy company and exact position."""
    # Fuzzy reads must use the same persisted identity alphabet as exact writes.
    # Otherwise an agent query cannot find a display name that differs only by
    # whitespace, even though every mutation treats both as one company.
    if not isinstance(company, str) or (
        position is not None and not isinstance(position, str)
    ):
        return []
    query = normalize_application_identity_part(company)
    if not query:
        return []
    if fuzzy:
        # Company text is data, not a SQL pattern.  Escaping also prevents a
        # model/user supplied ``%`` from accidentally returning every job.
        query = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        clause = "company_key LIKE '%' || ? || '%' ESCAPE '!'"
    else:
        clause = "company_key = ?"
    parameters = [user_id, query]
    if position is not None:
        position_key = normalize_application_identity_part(position)
        if not position_key:
            return []
        clause += " AND position_key = ?"
        parameters.append(position_key)
    with read_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT id, company, position, stage, current_step, next_stage, next_step, "
            f"next_date, next_time, next_note, revision FROM applications "
            f"WHERE user_id = ? AND {clause}",
            parameters,
        ).fetchall()
    return [
        {
            "id": row[0],
            "company": row[1],
            "position": row[2],
            "stage": row[3],
            "current_step": row[4],
            "next_action": (
                {"stage": row[5], "step": row[6], "date": row[7],
                 "time": row[8], "note": row[9]}
                if row[6] is not None else None
            ),
            "revision": row[10],
        }
        for row in rows
    ]


def _load_intake_proposal(extraction_json: str | None) -> IntakeProposal:
    """Revalidate journal JSON version, bounds, and consistency as untrusted input."""
    extraction = loads_json(extraction_json, {})
    return IntakeProposal.model_validate(extraction)


def _mark_intake_stale(conn: Connection, user_id: str, journal_id: int,
                       expected_revision: int, reason: str) -> None:
    """Terminate a claimed proposal that cannot execute safely."""
    derivation = {
        "operation": {
            "action": "stale",
            "reason": reason,
        },
    }
    changed = conn.execute(
        "UPDATE journal SET processed_time = NULL, derivation_json = ?, "
        "state = 'superseded', revision = revision + 1 "
        "WHERE user_id = ? AND id = ? AND kind = 'jd_batch' "
        "AND state = 'awaiting_user' AND revision = ?",
        (json.dumps(derivation, ensure_ascii=False), user_id, journal_id,
         expected_revision),
    ).rowcount
    if changed != 1:
        raise IntakeOperationConflict("批量导入操作状态已变化")


def _preflight_intake_bindings(conn: Connection, user_id: str,
                               positions: list[IntakeProposalPosition]) -> str | None:
    """Validate every retained row in one write transaction; any drift blocks all."""
    for position in positions:
        effect = position.effect
        binding = position.binding
        if binding.mode == "create":
            existing = conn.execute(
                "SELECT 1 FROM applications "
                "WHERE user_id = ? AND company_key = ? AND position_key = ?",
                (user_id, *application_identity_key(effect.company, effect.position)),
            ).fetchone()
            if existing is not None:
                return "create_binding_now_exists"
            continue

        row = conn.execute(
            f"SELECT {_DEPENDENCY_COLUMNS} FROM applications "
            "WHERE user_id = ? AND id = ?",
            (user_id, binding.application_id),
        ).fetchone()
        if row is None:
            return "update_binding_missing"
        current = _dependency_snapshot(row)
        # Compare whitespace-normalized natural keys, matching planning semantics.
        if application_identity_key(
            current["company"], current["position"],
        ) != application_identity_key(effect.company, effect.position):
            return "update_binding_natural_key_mismatch"
        if _dependency_fingerprint(current) != binding.dependency_fingerprint:
            return "update_binding_drifted"
    return None


def _effect_jd_parsed_json(effect: IntakeEffect) -> str | None:
    if not (effect.skills or effect.highlights):
        return None
    return json.dumps(
        {"skills": effect.skills, "highlights": effect.highlights},
        ensure_ascii=False,
    )


def _effect_next_values(effect: IntakeEffect) -> tuple:
    if effect.next_action is None:
        return (None, None, None, None, None)
    return (
        effect.next_action.stage,
        effect.next_action.step,
        effect.next_action.date,
        effect.next_action.time,
        effect.next_action.note,
    )


def _apply_intake_effects_in_transaction(
    conn: Connection,
    user_id: str,
    journal_id: int,
    positions: list[IntakeProposalPosition],
) -> dict:
    """Execute only displayed effects and flags without coalescing live values."""
    created: list[dict] = []
    updated: list[dict] = []
    for position in positions:
        effect = position.effect
        flags = position.flags
        jd_parsed_json = _effect_jd_parsed_json(effect)
        timestamp = now_iso()
        current = None
        if position.binding.mode == "create":
            company_id = companies.ensure_company_in_transaction(
                conn, user_id, effect.company,
            )
            cursor = conn.execute(
                "INSERT INTO applications "
                "(user_id, company, company_id, position, department, channel, jd_text, jd_parsed_json, "
                "stage, current_step, applied_date, pause_reason, application_note, priority, "
                "next_stage, next_step, next_date, next_time, next_note, created_time, updated_time) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, effect.company, company_id, effect.position, effect.department,
                 effect.channel, effect.jd_text, jd_parsed_json, effect.stage,
                 effect.current_step, effect.applied_date, effect.pause_reason,
                 effect.application_note, effect.priority, *_effect_next_values(effect),
                 timestamp, timestamp),
            )
            application_id = cursor.lastrowid
            if effect.stage != "backlog" or effect.current_step is not None:
                entry = conn.execute(
                    "INSERT INTO timeline_entries (user_id, application_id, step, "
                    "occurred_date, summary, from_stage, from_step, to_stage, to_step, "
                    "source, journal_id, created_time) VALUES (?, ?, ?, ?, ?, 'backlog', "
                    "NULL, ?, ?, 'agent', ?, ?)",
                    (
                        user_id,
                        application_id,
                        effect.current_step,
                        effect.applied_date if effect.stage == "applied" else None,
                        (
                            "投递"
                            if flags.add_applied_entry
                            else f"批量导入新增岗位并设为「{STAGE_LABELS[effect.stage]}」"
                        ),
                        effect.stage,
                        effect.current_step,
                        journal_id,
                        timestamp,
                    ),
                )
                conn.execute(
                    "UPDATE applications SET current_state_entry_id = ? WHERE id = ?",
                    (int(entry.lastrowid), application_id),
                )
            target = created
        else:
            application_id = position.binding.application_id
            current = conn.execute(
                "SELECT stage, current_step, current_state_entry_id, paused_from_stage, revision "
                "FROM applications WHERE user_id = ? AND id = ?",
                (user_id, application_id),
            ).fetchone()
            if current is None:  # pragma: no cover - BEGIN IMMEDIATE preflight
                raise IntakeOperationConflict("岗位绑定在执行期间消失")
            state_changed = (
                effect.stage != current[0] or effect.current_step != current[1]
            )
            current_state_entry_id = current[2]
            if state_changed:
                entry = conn.execute(
                    "INSERT INTO timeline_entries (user_id, application_id, step, "
                    "occurred_date, summary, from_stage, from_step, to_stage, to_step, "
                    "source, journal_id, created_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "'agent', ?, ?)",
                    (
                        user_id,
                        application_id,
                        effect.current_step,
                        effect.applied_date if effect.stage == "applied" else None,
                        "批量导入更新岗位进度",
                        current[0],
                        current[1],
                        effect.stage,
                        effect.current_step,
                        journal_id,
                        timestamp,
                    ),
                )
                current_state_entry_id = int(entry.lastrowid)
            paused_from_stage = (
                current[0]
                if effect.stage == "pooled" and current[0] in {
                    "backlog", "applied", "written_test", "interviewing", "offer",
                }
                else current[3] if effect.stage == "pooled"
                else None
            )
            changed = conn.execute(
                "UPDATE applications SET company = ?, position = ?, department = ?, "
                "channel = ?, jd_text = ?, jd_parsed_json = ?, stage = ?, current_step = ?, "
                "current_state_entry_id = ?, applied_date = ?, next_stage = ?, next_step = ?, "
                "next_date = ?, next_time = ?, next_note = ?, paused_from_stage = ?, "
                "pause_reason = ?, application_note = ?, priority = ?, "
                "revision = revision + 1, updated_time = ? "
                "WHERE user_id = ? AND id = ? AND revision = ?",
                (effect.company, effect.position, effect.department, effect.channel,
                 effect.jd_text, jd_parsed_json, effect.stage, effect.current_step,
                 current_state_entry_id, effect.applied_date, *_effect_next_values(effect),
                 paused_from_stage,
                 effect.pause_reason if effect.stage == "pooled" else None,
                 effect.application_note, effect.priority, timestamp,
                 user_id, application_id, current[4]),
            ).rowcount
            if changed != 1:  # pragma: no cover - preflight used the same immediate txn
                raise IntakeOperationConflict("岗位绑定在执行期间发生变化")
            target = updated

        if flags.invalidate_prep:
            _invalidate_prep(conn, user_id, application_id)
        target.append({
            "id": application_id,
            "company": effect.company,
            "position": effect.position,
        })
    return {"created": created, "updated": updated}


def _normalize_exclude_indexes(exclude_indexes: list[int] | None) -> list[int]:
    values = exclude_indexes or []
    if len(values) > MAX_BATCH_POSITIONS:
        raise IntakeOperationInvalidSelection("剔除行号数量超过安全上限")
    if any(isinstance(index, bool) or not isinstance(index, int) or index < 1
           for index in values):
        raise IntakeOperationInvalidSelection("剔除行号必须是从 1 开始的整数")
    return sorted(set(values))


def _operation_command_hash(action: str, operation_id: str,
                            exclude_indexes: list[int] | None = None) -> str:
    command = {"action": action, "operation_id": operation_id}
    if exclude_indexes is not None:
        command["exclude_indexes"] = exclude_indexes
    canonical = json.dumps(command, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def approve_intake_operation(db_path: str, user_id: str, operation_id: str | UUID, *,
                             exclude_indexes: list[int] | None = None) -> dict:
    """Atomically claim, preflight all bindings, and execute exact effects."""
    canonical_id = _canonical_operation_id(operation_id)
    excluded = _normalize_exclude_indexes(exclude_indexes)
    command_hash = _operation_command_hash("approve", canonical_id, excluded)
    conflict_reason: str | None = None
    completed: dict | None = None

    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _assert_intake_owner_integrity(conn, user_id)
        row = _operation_row(conn, user_id, canonical_id)
        if row is None:
            raise IntakeOperationNotFound("批量导入操作不存在")
        dto = _operation_dto(row)
        if row[2] != "awaiting_user" or row[4] is None or row[6]:
            derivation = loads_json(row[5], {})
            operation = derivation.get("operation", {}) if isinstance(derivation, dict) else {}
            if dto["state"] == "completed":
                if operation.get("command_hash") == command_hash:
                    return dto
                raise IntakeOperationConflict("该操作已用不同的剔除选择完成")
            raise IntakeOperationConflict(f"该操作当前为 {dto['state']}，不能确认")

        claimed = conn.execute(
            "UPDATE journal SET revision = revision + 1 "
            "WHERE user_id = ? AND id = ? AND operation_id = ? AND kind = 'jd_batch' "
            "AND state = 'awaiting_user' AND extraction_json IS NOT NULL AND revision = ? "
            f"AND EXISTS (SELECT 1 FROM {_INTAKE_OWNER_TABLE} owner "
            "WHERE owner.journal_id = journal.id AND owner.user_id = journal.user_id "
            "AND owner.operation_id = journal.operation_id "
            "AND owner.created_time = journal.created_time) "
            f"AND NOT EXISTS (SELECT 1 FROM {_INTAKE_OWNER_TABLE} newer "
            "WHERE newer.user_id = journal.user_id AND newer.journal_id > journal.id) "
            "RETURNING id, extraction_json, revision",
            (user_id, row[0], canonical_id, row[9]),
        ).fetchone()
        if claimed is None:
            raise IntakeOperationConflict("批量导入 owner claim 状态已变化")

        journal_id, extraction_json, claim_revision = claimed
        try:
            proposal = _load_intake_proposal(extraction_json)
        except (TypeError, ValueError):
            conflict_reason = "intake_contract_invalid"
            _mark_intake_stale(
                conn, user_id, journal_id, claim_revision, conflict_reason,
            )
        else:
            invalid = [index for index in excluded if index > len(proposal.positions)]
            if invalid:
                raise IntakeOperationInvalidSelection(
                    f"剔除行号超出预览范围：{invalid}",
                )
            kept = [
                position
                for index, position in enumerate(proposal.positions, start=1)
                if index not in excluded
            ]
            if not kept:
                raise IntakeOperationInvalidSelection("不能剔除全部岗位")

            conflict_reason = _preflight_intake_bindings(conn, user_id, kept)
            if conflict_reason is not None:
                _mark_intake_stale(
                    conn, user_id, journal_id, claim_revision, conflict_reason,
                )
            else:
                business_result = _apply_intake_effects_in_transaction(
                    conn, user_id, journal_id, kept,
                )
                result = {"status": "ok", **business_result}
                operation = {
                    "action": "approve",
                    "command_hash": command_hash,
                    "exclude_indexes": excluded,
                    "result": result,
                }
                derivation = {
                    "created": [item["id"] for item in business_result["created"]],
                    "updated": [item["id"] for item in business_result["updated"]],
                    "operation": operation,
                }
                finalized = conn.execute(
                    "UPDATE journal SET processed_time = ?, derivation_json = ?, "
                    "state = 'applied', revision = revision + 1 "
                    "WHERE user_id = ? AND id = ? AND kind = 'jd_batch' "
                    "AND state = 'awaiting_user' AND revision = ?",
                    (now_iso(), json.dumps(derivation, ensure_ascii=False),
                     user_id, journal_id, claim_revision),
                ).rowcount
                if finalized != 1:
                    raise IntakeOperationConflict("批量导入操作状态已变化")
                row = _operation_row(conn, user_id, canonical_id)
                if row is None:  # pragma: no cover - primary key updated in this txn
                    raise IntakeOperationNotFound("批量导入操作不存在")
                completed = _operation_dto(row)

    if conflict_reason is not None:
        raise IntakeOperationConflict(
            f"批量导入预览已失效，请重新生成（{conflict_reason}）",
        )
    if completed is None:  # pragma: no cover - exhaustive branch guard
        raise IntakeOperationConflict("批量导入操作未能完成")
    return completed


def reject_intake_operation(db_path: str, user_id: str,
                            operation_id: str | UUID) -> dict:
    """Idempotently reject a valid preview; terminate corrupt contracts as stale."""
    canonical_id = _canonical_operation_id(operation_id)
    command_hash = _operation_command_hash("reject", canonical_id)
    operation = {"action": "reject", "command_hash": command_hash}
    conflict_reason: str | None = None
    rejected_dto: dict | None = None
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _assert_intake_owner_integrity(conn, user_id)
        row = _operation_row(conn, user_id, canonical_id)
        if row is None:
            raise IntakeOperationNotFound("批量导入操作不存在")
        dto = _operation_dto(row)
        if dto["state"] == "rejected":
            return dto
        if row[2] != "awaiting_user" or row[6]:
            raise IntakeOperationConflict(f"该操作当前为 {dto['state']}，不能拒绝")

        journal_id, revision = row[0], row[9]
        try:
            _load_intake_proposal(row[4])
        except (TypeError, ValueError):
            conflict_reason = "intake_contract_invalid"
            _mark_intake_stale(conn, user_id, journal_id, revision, conflict_reason)
        else:
            rejected = conn.execute(
                "UPDATE journal SET processed_time = NULL, derivation_json = ?, "
                "state = 'voided', revision = revision + 1 "
                "WHERE user_id = ? AND id = ? AND kind = 'jd_batch' "
                "AND state = 'awaiting_user' AND revision = ?",
                (json.dumps({"operation": operation}, ensure_ascii=False),
                 user_id, journal_id, revision),
            ).rowcount
            if rejected != 1:
                raise IntakeOperationConflict("批量导入操作状态已变化")
            terminal_row = _operation_row(conn, user_id, canonical_id)
            if terminal_row is None:  # pragma: no cover
                raise IntakeOperationNotFound("批量导入操作不存在")
            rejected_dto = _operation_dto(terminal_row)

    if conflict_reason is not None:
        raise IntakeOperationConflict(
            f"批量导入预览已损坏，已安全终结（{conflict_reason}）",
        )
    if rejected_dto is None:  # pragma: no cover
        raise IntakeOperationConflict("批量导入操作未能拒绝")
    return rejected_dto
