"""CareerDesk's fresh-only declarative SQLite schema and physical manifest gate."""

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from functools import cache

from .identity import application_identity_sql

__all__ = [
    "FRESH_SCHEMA_REVISION",
    "INDEXES",
    "SCHEMA",
    "SCHEMA_VERSION",
    "TRIGGERS",
    "assert_current_schema_manifest",
    "assert_database_shape_before_init",
    "assert_supported_schema_version",
]

# ``FRESH_SCHEMA_REVISION`` is the product/domain contract.  The physical
# SQLite user_version stays monotonic across local builds so an older binary
# binary sees this database as future data and refuses it instead of trying
# to migrate a fresh-only database through the retired schema chain.
FRESH_SCHEMA_REVISION = 1

# ── Table declarations ────────────────────────────────────────────────────────
# All tables are STRICT and idempotently created. Enum columns use CHECK because
# dirty display-source values would break the board. Booleans use INTEGER 0/1.
SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    id              INTEGER PRIMARY KEY,
    user_id         TEXT NOT NULL,
    kind            TEXT NOT NULL
                    CHECK (kind IN ('review', 'jd_batch', 'correction')),
    content         TEXT NOT NULL,
    created_time    TEXT NOT NULL,
    processed_time  TEXT,
    extraction_json TEXT,
    derivation_json TEXT,
    state           TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'awaiting_user', 'applied', 'failed', 'superseded', 'voided')),
    revision        INTEGER NOT NULL DEFAULT 0,
    parent_journal_id INTEGER REFERENCES journal(id),
    operation_id    TEXT
                    CHECK (operation_id IS NULL OR (
                        length(operation_id) = 36
                        AND operation_id = lower(operation_id)
                        AND substr(operation_id, 9, 1) = '-'
                        AND substr(operation_id, 14, 1) = '-'
                        AND substr(operation_id, 19, 1) = '-'
                        AND substr(operation_id, 24, 1) = '-'
                        AND length(replace(operation_id, '-', '')) = 32
                        AND replace(operation_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                    ))
) STRICT;

CREATE TABLE IF NOT EXISTS application_intake_operation_owners (
    journal_id    INTEGER PRIMARY KEY REFERENCES journal(id),
    user_id       TEXT NOT NULL CHECK (length(user_id) > 0),
    operation_id  TEXT NOT NULL UNIQUE
                  CHECK (
                      length(operation_id) = 36
                      AND operation_id = lower(operation_id)
                      AND substr(operation_id, 9, 1) = '-'
                      AND substr(operation_id, 14, 1) = '-'
                      AND substr(operation_id, 19, 1) = '-'
                      AND substr(operation_id, 24, 1) = '-'
                      AND length(replace(operation_id, '-', '')) = 32
                      AND replace(operation_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                  ),
    created_time  TEXT NOT NULL
                  CHECK (
                      length(created_time) BETWEEN 1 AND 64
                      AND created_time = trim(created_time)
                  )
) STRICT;

CREATE TABLE IF NOT EXISTS companies (
    id            INTEGER PRIMARY KEY,
    user_id       TEXT NOT NULL,
    name          TEXT NOT NULL,
    name_key      TEXT GENERATED ALWAYS AS (__COMPANY_NAME_KEY_SQL__) STORED,
    aliases_json  TEXT,
    research_json TEXT,
    research_time TEXT,
    notes         TEXT,
    created_time  TEXT NOT NULL,
    updated_time  TEXT NOT NULL,
    UNIQUE (user_id, name_key),
    CHECK (length(name_key) >= 1)
) STRICT;

CREATE TABLE IF NOT EXISTS resumes (
    id             INTEGER PRIMARY KEY,
    user_id        TEXT NOT NULL,
    name           TEXT NOT NULL,
    family         TEXT,
    binding        TEXT NOT NULL DEFAULT 'family'
                   CHECK (binding IN ('family', 'application')),
    application_id INTEGER REFERENCES applications(id),
    file_path      TEXT,
    content_text   TEXT NOT NULL,
    content_hash   TEXT NOT NULL CHECK (
                       length(content_hash) = 64
                       AND content_hash NOT GLOB '*[^0-9a-f]*'
                   ),
    extraction_receipt_json TEXT NOT NULL CHECK (json_valid(extraction_receipt_json)),
    segments_json  TEXT NOT NULL CHECK (json_valid(segments_json)),
    lines_json     TEXT,
    annotation_status TEXT NOT NULL DEFAULT 'pending'
                      CHECK (annotation_status IN ('pending', 'ready', 'failed')),
    annotation_generation TEXT,
    annotation_hash TEXT,
    archived       INTEGER NOT NULL DEFAULT 0,
    created_time   TEXT NOT NULL,
    updated_time   TEXT NOT NULL,
    UNIQUE (user_id, name)
) STRICT;

CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    company         TEXT NOT NULL,
    company_key     TEXT GENERATED ALWAYS AS (__APPLICATION_COMPANY_KEY_SQL__) STORED,
    company_id      INTEGER REFERENCES companies(id),
    position        TEXT NOT NULL,
    position_key    TEXT GENERATED ALWAYS AS (__APPLICATION_POSITION_KEY_SQL__) STORED,
    department      TEXT,
    channel         TEXT,
    jd_text         TEXT,
    jd_content_hash TEXT CHECK (
                        jd_content_hash IS NULL OR (
                            length(jd_content_hash) = 64
                            AND jd_content_hash NOT GLOB '*[^0-9a-f]*'
                        )
                    ),
    jd_receipt_json TEXT CHECK (jd_receipt_json IS NULL OR json_valid(jd_receipt_json)),
    jd_receipt_status TEXT NOT NULL DEFAULT 'unconfirmed'
                      CHECK (jd_receipt_status IN ('unconfirmed', 'confirmed')),
    jd_parsed_json  TEXT,
    stage           TEXT NOT NULL DEFAULT 'backlog'
                    CHECK (stage IN ('backlog', 'applied', 'written_test', 'interviewing', 'offer', 'withdrawn', 'rejected', 'pooled')),
    current_step    TEXT CHECK (
                        current_step IS NULL OR (
                            length(current_step) BETWEEN 1 AND 300
                            AND current_step = trim(current_step)
                        )
                    ),
    current_state_entry_id INTEGER REFERENCES timeline_entries(id),
    next_stage      TEXT CHECK (
                        next_stage IS NULL OR next_stage IN (
                            'backlog', 'applied', 'written_test', 'interviewing',
                            'offer', 'withdrawn', 'rejected', 'pooled'
                        )
                    ),
    next_step       TEXT CHECK (
                        next_step IS NULL OR (
                            length(next_step) BETWEEN 1 AND 300
                            AND next_step = trim(next_step)
                        )
                    ),
    next_date       TEXT CHECK (next_date IS NULL OR length(next_date) = 10),
    next_time       TEXT CHECK (next_time IS NULL OR length(next_time) = 5),
    next_note       TEXT CHECK (
                        next_note IS NULL OR (
                            length(next_note) BETWEEN 1 AND 2000
                            AND next_note = trim(next_note)
                        )
                    ),
    paused_from_stage TEXT CHECK (
                        paused_from_stage IS NULL OR paused_from_stage IN (
                            'backlog', 'applied', 'written_test', 'interviewing', 'offer'
                        )
                    ),
    pause_reason    TEXT CHECK (
                        pause_reason IS NULL OR (
                            length(pause_reason) BETWEEN 1 AND 1000
                            AND pause_reason = trim(pause_reason)
                        )
                    ),
    application_note TEXT CHECK (
                        application_note IS NULL OR (
                            length(application_note) BETWEEN 1 AND 2000
                            AND application_note = trim(application_note)
                        )
                    ),
    priority        TEXT CHECK (priority IS NULL OR priority IN ('high', 'medium', 'low')),
    resume_id       INTEGER REFERENCES resumes(id),
    applied_date    TEXT CHECK (applied_date IS NULL OR length(applied_date) = 10),
    prep_status     TEXT NOT NULL DEFAULT 'none'
                    CHECK (prep_status IN ('none', 'pending', 'running', 'ready', 'failed')),
    prep_generation TEXT,
    prep_heartbeat_time TEXT,
    prep_json       TEXT,
    revision        INTEGER NOT NULL DEFAULT 0
                    CHECK (revision >= 0),
    created_time    TEXT NOT NULL,
    updated_time    TEXT NOT NULL,
    UNIQUE (user_id, company_key, position_key),
    CHECK (length(company_key) >= 1 AND length(position_key) >= 1),
    CHECK (
        (next_step IS NULL AND next_stage IS NULL AND next_date IS NULL
         AND next_time IS NULL AND next_note IS NULL)
        OR
        (next_step IS NOT NULL AND next_stage IS NOT NULL)
    ),
    CHECK (next_time IS NULL OR next_date IS NOT NULL),
    CHECK (stage NOT IN ('rejected', 'withdrawn') OR next_step IS NULL),
    CHECK (stage = 'pooled' OR paused_from_stage IS NULL),
    CHECK (stage = 'pooled' OR pause_reason IS NULL)
) STRICT;

CREATE TABLE IF NOT EXISTS timeline_entries (
    id             INTEGER PRIMARY KEY,
    user_id        TEXT NOT NULL,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    step           TEXT CHECK (
                       step IS NULL OR (
                           length(step) BETWEEN 1 AND 300 AND step = trim(step)
                       )
                   ),
    occurred_date  TEXT CHECK (occurred_date IS NULL OR length(occurred_date) = 10),
    outcome        TEXT CHECK (outcome IS NULL OR outcome IN ('passed', 'failed', 'cancelled')),
    summary        TEXT CHECK (
                       summary IS NULL OR (
                           length(summary) BETWEEN 1 AND 2000 AND summary = trim(summary)
                       )
                   ),
    from_stage     TEXT NOT NULL CHECK (from_stage IN ('backlog', 'applied', 'written_test', 'interviewing', 'offer', 'withdrawn', 'rejected', 'pooled')),
    from_step      TEXT CHECK (from_step IS NULL OR (length(from_step) BETWEEN 1 AND 300 AND from_step = trim(from_step))),
    to_stage       TEXT NOT NULL CHECK (to_stage IN ('backlog', 'applied', 'written_test', 'interviewing', 'offer', 'withdrawn', 'rejected', 'pooled')),
    to_step        TEXT CHECK (to_step IS NULL OR (length(to_step) BETWEEN 1 AND 300 AND to_step = trim(to_step))),
    source         TEXT NOT NULL CHECK (source IN ('manual', 'agent', 'review', 'drag', 'system')),
    journal_id     INTEGER REFERENCES journal(id),
    created_time   TEXT NOT NULL,
    CHECK (
        step IS NOT NULL OR summary IS NOT NULL OR outcome IS NOT NULL
        OR from_stage != to_stage OR from_step IS NOT to_step
    )
) STRICT;

CREATE TABLE IF NOT EXISTS knowledge_points (
    id              INTEGER PRIMARY KEY,
    user_id         TEXT NOT NULL,
    name            TEXT NOT NULL,
    topic           TEXT,
    box             INTEGER NOT NULL DEFAULT 0,
    correct_streak  INTEGER NOT NULL DEFAULT 0,
    last_asked_time TEXT,
    last_wrong_time TEXT,
    due_date        TEXT,
    note            TEXT,
    created_time    TEXT NOT NULL,
    updated_time    TEXT NOT NULL,
    UNIQUE (user_id, name)
) STRICT;

CREATE TABLE IF NOT EXISTS questions (
    id               INTEGER PRIMARY KEY,
    user_id          TEXT NOT NULL,
    text             TEXT NOT NULL,
    source           TEXT NOT NULL
                     CHECK (source IN ('real', 'generated', 'imported')),
    company          TEXT,
    source_step      TEXT,
    asked_date       TEXT,
    application_id   INTEGER REFERENCES applications(id),
    question_set_id  INTEGER REFERENCES question_sets(id),
    immutable_revision INTEGER NOT NULL DEFAULT 1 CHECK (immutable_revision >= 1),
    category         TEXT CHECK (category IS NULL OR category IN (
                         'hr_motivation', 'resume_deep_dive', 'behavioral_situational',
                         'professional_domain', 'business_company', 'case_work_sample'
                     )),
    channel          TEXT CHECK (channel IS NULL OR channel IN ('interview', 'written')),
    response_format  TEXT CHECK (response_format IS NULL OR response_format IN (
                         'oral_text', 'short_written', 'long_written', 'case_outline'
                     )),
    evaluation_kind  TEXT CHECK (evaluation_kind IS NULL OR evaluation_kind IN (
                         'evidence_consistency', 'factual', 'rubric', 'case'
                     )),
    primary_competency TEXT,
    secondary_tags_json TEXT CHECK (secondary_tags_json IS NULL OR json_valid(secondary_tags_json)),
    rubric_json      TEXT CHECK (rubric_json IS NULL OR json_valid(rubric_json)),
    answer_guide_json TEXT CHECK (answer_guide_json IS NULL OR json_valid(answer_guide_json)),
    evidence_json    TEXT CHECK (evidence_json IS NULL OR json_valid(evidence_json)),
    answer_verification_json TEXT CHECK (
                                 answer_verification_json IS NULL
                                 OR json_valid(answer_verification_json)
                             ),
    status           TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'archived')),
    quality_flag     TEXT
                     CHECK (quality_flag IN ('good', 'bad') OR quality_flag IS NULL),
    journal_id       INTEGER REFERENCES journal(id),
    created_time     TEXT NOT NULL,
    updated_time     TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS question_sets (
    id                   INTEGER PRIMARY KEY,
    user_id              TEXT NOT NULL,
    kind                 TEXT NOT NULL CHECK (kind IN ('generated', 'library_snapshot')),
    edition              TEXT CHECK (edition IS NULL OR edition IN ('basic', 'custom')),
    resume_id            INTEGER,
    application_id       INTEGER,
    state                TEXT NOT NULL CHECK (state IN ('pending', 'running', 'ready', 'failed')),
    stage                TEXT NOT NULL DEFAULT 'preparing' CHECK (
                             stage IN ('preparing', 'summarizing', 'generating', 'ready', 'failed')
                         ),
    generation           TEXT NOT NULL,
    claim_token          TEXT,
    lease_expires_time   TEXT,
    heartbeat_time       TEXT,
    safe_error_code      TEXT,
    material_fingerprint TEXT NOT NULL,
    policy_fingerprint   TEXT NOT NULL,
    generation_fingerprint TEXT NOT NULL,
    prompt_version       TEXT NOT NULL,
    schema_version       TEXT NOT NULL,
    rubric_version       TEXT NOT NULL,
    segmentation_version TEXT NOT NULL,
    summary_policy_version TEXT NOT NULL,
    model_label          TEXT,
    input_receipt_json   TEXT NOT NULL CHECK (json_valid(input_receipt_json)),
    coverage_json        TEXT NOT NULL CHECK (json_valid(coverage_json)),
    context_label        TEXT NOT NULL,
    archived_at          TEXT,
    started_time         TEXT,
    finished_time        TEXT,
    created_time         TEXT NOT NULL,
    updated_time         TEXT NOT NULL,
    CHECK (
        (kind = 'generated' AND edition IS NOT NULL AND resume_id IS NOT NULL)
        OR
        (kind = 'library_snapshot' AND edition IS NULL AND resume_id IS NULL
         AND application_id IS NULL)
    ),
    CHECK (edition != 'basic' OR application_id IS NULL),
    CHECK (edition != 'custom' OR application_id IS NOT NULL)
) STRICT;

CREATE TABLE IF NOT EXISTS question_set_commands (
    user_id                  TEXT NOT NULL,
    client_command_id        TEXT NOT NULL,
    request_digest           TEXT NOT NULL CHECK (length(request_digest) = 64),
    state                    TEXT NOT NULL CHECK (state IN ('running', 'completed', 'failed')),
    canonical_question_set_id INTEGER REFERENCES question_sets(id),
    safe_error_code          TEXT,
    created_time             TEXT NOT NULL,
    updated_time             TEXT NOT NULL,
    PRIMARY KEY (user_id, client_command_id)
) STRICT;

CREATE TABLE IF NOT EXISTS question_set_items (
    id                    INTEGER PRIMARY KEY,
    user_id               TEXT NOT NULL,
    question_set_id       INTEGER NOT NULL REFERENCES question_sets(id),
    ordinal               INTEGER NOT NULL CHECK (ordinal >= 0),
    canonical_question_id INTEGER REFERENCES questions(id),
    canonical_revision    INTEGER NOT NULL CHECK (canonical_revision >= 1),
    canonical_digest      TEXT NOT NULL CHECK (length(canonical_digest) = 64),
    text                  TEXT NOT NULL,
    category              TEXT NOT NULL CHECK (category IN (
                              'hr_motivation', 'resume_deep_dive', 'behavioral_situational',
                              'professional_domain', 'business_company', 'case_work_sample'
                          )),
    channel               TEXT NOT NULL CHECK (channel IN ('interview', 'written')),
    response_format       TEXT NOT NULL CHECK (response_format IN (
                              'oral_text', 'short_written', 'long_written', 'case_outline'
                          )),
    evaluation_kind       TEXT NOT NULL CHECK (evaluation_kind IN (
                              'evidence_consistency', 'factual', 'rubric', 'case'
                          )),
    difficulty            TEXT NOT NULL CHECK (difficulty IN ('introductory', 'intermediate', 'advanced')),
    primary_competency    TEXT NOT NULL,
    secondary_tags_json   TEXT NOT NULL CHECK (json_valid(secondary_tags_json)),
    rubric_json           TEXT NOT NULL CHECK (json_valid(rubric_json)),
    answer_authority      TEXT NOT NULL CHECK (answer_authority IN (
                              'source_grounded', 'user_verified', 'model_generated_unverified'
                          )),
    answer_guide_json     TEXT NOT NULL CHECK (json_valid(answer_guide_json)),
    evidence_json         TEXT NOT NULL CHECK (json_valid(evidence_json)),
    follow_up_allowed     INTEGER NOT NULL CHECK (follow_up_allowed IN (0, 1)),
    repeat_scope          TEXT NOT NULL CHECK (repeat_scope IN ('none', 'global', 'resume', 'application')),
    created_time          TEXT NOT NULL,
    UNIQUE (question_set_id, ordinal)
) STRICT;

CREATE TABLE IF NOT EXISTS question_knowledge (
    question_id        INTEGER NOT NULL REFERENCES questions(id),
    knowledge_point_id INTEGER NOT NULL REFERENCES knowledge_points(id),
    PRIMARY KEY (question_id, knowledge_point_id)
) STRICT;

CREATE TABLE IF NOT EXISTS review_question_occurrences (
    user_id        TEXT NOT NULL,
    journal_id     INTEGER NOT NULL REFERENCES journal(id),
    question_id    INTEGER NOT NULL REFERENCES questions(id),
    application_id INTEGER REFERENCES applications(id),
    company        TEXT NOT NULL,
    source_step    TEXT,
    asked_date     TEXT,
    PRIMARY KEY (user_id, journal_id, question_id)
) STRICT;

CREATE TABLE IF NOT EXISTS grill_sessions (
    id             INTEGER PRIMARY KEY,
    user_id        TEXT NOT NULL,
    question_set_id INTEGER NOT NULL REFERENCES question_sets(id),
    kind           TEXT NOT NULL CHECK (kind IN ('generated', 'library_snapshot')),
    edition        TEXT CHECK (edition IS NULL OR edition IN ('basic', 'custom')),
    context_label  TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'active'
                   CHECK (state IN ('active', 'suspended', 'finished')),
    plan_json      TEXT,
    summary_json   TEXT,
    started_time   TEXT NOT NULL,
    ended_time     TEXT,
    updated_time   TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS grill_session_items (
    id                    INTEGER PRIMARY KEY,
    user_id               TEXT NOT NULL,
    session_id            INTEGER NOT NULL REFERENCES grill_sessions(id),
    question_set_item_id  INTEGER NOT NULL REFERENCES question_set_items(id),
    ordinal               INTEGER NOT NULL CHECK (ordinal >= 0),
    state                 TEXT NOT NULL DEFAULT 'unanswered'
                          CHECK (state IN ('unanswered', 'answered', 'skipped')),
    follow_up_count       INTEGER NOT NULL DEFAULT 0 CHECK (follow_up_count BETWEEN 0 AND 1),
    claim_token           TEXT,
    claim_started_time    TEXT,
    claim_error_code      TEXT CHECK (
                              claim_error_code IS NULL
                              OR length(claim_error_code) BETWEEN 1 AND 100
                          ),
    session_owned_guide_json TEXT CHECK (
                                 session_owned_guide_json IS NULL
                                 OR json_valid(session_owned_guide_json)
                             ),
    UNIQUE (session_id, ordinal),
    UNIQUE (session_id, question_set_item_id)
) STRICT;

CREATE TABLE IF NOT EXISTS grill_answers (
    id              INTEGER PRIMARY KEY,
    user_id         TEXT NOT NULL,
    session_id      INTEGER NOT NULL REFERENCES grill_sessions(id),
    session_item_id INTEGER NOT NULL UNIQUE REFERENCES grill_session_items(id),
    question_id     INTEGER REFERENCES questions(id),
    transcript_json TEXT,
    verdict         TEXT
                    CHECK (verdict IN ('meets', 'partially_meets', 'needs_work', 'ungradable', 'skipped') OR verdict IS NULL),
    stuck           INTEGER NOT NULL DEFAULT 0,
    feedback        TEXT,
    created_time    TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS competency_progress (
    id                INTEGER PRIMARY KEY,
    user_id           TEXT NOT NULL,
    scope_kind        TEXT NOT NULL CHECK (scope_kind IN ('global', 'resume', 'application')),
    scope_ref         TEXT NOT NULL,
    context_label     TEXT NOT NULL,
    competency_key    TEXT NOT NULL,
    box               INTEGER NOT NULL DEFAULT 0 CHECK (box BETWEEN 0 AND 4),
    correct_streak    INTEGER NOT NULL DEFAULT 0 CHECK (correct_streak >= 0),
    practice_count    INTEGER NOT NULL DEFAULT 0 CHECK (practice_count >= 0),
    last_verdict      TEXT CHECK (last_verdict IS NULL OR last_verdict IN (
                            'meets', 'partially_meets', 'needs_work'
                        )),
    last_asked_time   TEXT,
    last_wrong_time   TEXT,
    due_date          TEXT,
    created_time      TEXT NOT NULL,
    updated_time      TEXT NOT NULL,
    UNIQUE (user_id, scope_kind, scope_ref, competency_key)
) STRICT;

CREATE TABLE IF NOT EXISTS status_log (
    id           INTEGER PRIMARY KEY,
    user_id      TEXT NOT NULL,
    log_date     TEXT NOT NULL,
    time_of_day  TEXT
                 CHECK (time_of_day IN ('morning', 'afternoon', 'evening') OR time_of_day IS NULL),
    mood         TEXT,
    factors_json TEXT,
    journal_id   INTEGER REFERENCES journal(id),
    created_time TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS preferences (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL CHECK (length(user_id) > 0),
    key          TEXT NOT NULL
                 CHECK (length(key) BETWEEN 1 AND 100 AND key = trim(key)),
    value        TEXT NOT NULL
                 CHECK (length(value) BETWEEN 1 AND 2000 AND value = trim(value)),
    revision     INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_time TEXT NOT NULL
                 CHECK (length(created_time) BETWEEN 1 AND 64 AND created_time = trim(created_time)),
    updated_time TEXT NOT NULL
                 CHECK (length(updated_time) BETWEEN 1 AND 64 AND updated_time = trim(updated_time)),
    UNIQUE (user_id, key)
) STRICT;

CREATE TABLE IF NOT EXISTS preference_owners (
    preference_id INTEGER PRIMARY KEY CHECK (preference_id > 0),
    user_id       TEXT NOT NULL CHECK (length(user_id) > 0),
    created_time  TEXT NOT NULL
                  CHECK (
                      length(created_time) BETWEEN 1 AND 64
                      AND created_time = trim(created_time)
                  )
) STRICT;

CREATE TABLE IF NOT EXISTS preference_item_command_owners (
    user_id       TEXT NOT NULL CHECK (length(user_id) > 0),
    command_id    TEXT NOT NULL
                  CHECK (
                      length(command_id) = 36
                      AND command_id = lower(command_id)
                      AND substr(command_id, 9, 1) = '-'
                      AND substr(command_id, 14, 1) = '-'
                      AND substr(command_id, 19, 1) = '-'
                      AND substr(command_id, 24, 1) = '-'
                      AND length(replace(command_id, '-', '')) = 32
                      AND replace(command_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                  ),
    created_time  TEXT NOT NULL
                  CHECK (
                      length(created_time) BETWEEN 1 AND 64
                      AND created_time = trim(created_time)
                  ),
    PRIMARY KEY (user_id, command_id)
) STRICT;

CREATE TABLE IF NOT EXISTS preference_item_commands (
    user_id           TEXT NOT NULL CHECK (length(user_id) > 0),
    command_id        TEXT NOT NULL
                      CHECK (
                          length(command_id) = 36
                          AND command_id = lower(command_id)
                          AND substr(command_id, 9, 1) = '-'
                          AND substr(command_id, 14, 1) = '-'
                          AND substr(command_id, 19, 1) = '-'
                          AND substr(command_id, 24, 1) = '-'
                          AND length(replace(command_id, '-', '')) = 32
                          AND replace(command_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                      ),
    action             TEXT NOT NULL CHECK (action IN ('set', 'delete')),
    target_id          INTEGER NOT NULL CHECK (target_id > 0),
    expected_revision  INTEGER NOT NULL CHECK (expected_revision > 0),
    state              TEXT NOT NULL
                       CHECK (state IN ('completed', 'rejected', 'cancelled')),
    outcome            TEXT CHECK (outcome IN ('updated', 'deleted', 'no_change')),
    journal_id         INTEGER UNIQUE REFERENCES journal(id),
    operation_id       TEXT UNIQUE
                       CHECK (operation_id IS NULL OR (
                           length(operation_id) = 36
                           AND operation_id = lower(operation_id)
                           AND substr(operation_id, 9, 1) = '-'
                           AND substr(operation_id, 14, 1) = '-'
                           AND substr(operation_id, 19, 1) = '-'
                           AND substr(operation_id, 24, 1) = '-'
                           AND length(replace(operation_id, '-', '')) = 32
                           AND replace(operation_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                       )),
    error_code         TEXT CHECK (error_code IN (
                           'target_missing', 'target_changed',
                           'limit_exceeded', 'projection_invalid'
                       )),
    finished_time      TEXT NOT NULL
                       CHECK (
                           length(finished_time) BETWEEN 1 AND 64
                           AND finished_time = trim(finished_time)
                       ),
    PRIMARY KEY (user_id, command_id),
    FOREIGN KEY (user_id, command_id)
        REFERENCES preference_item_command_owners(user_id, command_id),
    CHECK (
        (state = 'completed' AND error_code IS NULL AND (
            (outcome IN ('updated', 'deleted')
             AND journal_id IS NOT NULL AND operation_id IS NOT NULL)
            OR
            (outcome = 'no_change'
             AND journal_id IS NULL AND operation_id IS NULL)
        ))
        OR
        (state = 'rejected' AND outcome IS NULL AND journal_id IS NULL
         AND operation_id IS NULL AND error_code IS NOT NULL)
        OR
        (state = 'cancelled' AND outcome IS NULL AND journal_id IS NULL
         AND operation_id IS NULL AND error_code IS NULL)
    )
) STRICT;

CREATE TABLE IF NOT EXISTS assistant_turns (
    user_id             TEXT NOT NULL,
    client_turn_id      TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    request_hash        TEXT NOT NULL
                        CHECK (length(request_hash) = 64
                               AND request_hash NOT GLOB '*[^0-9a-f]*'),
    state               TEXT NOT NULL
                        CHECK (state IN ('running', 'completed', 'unknown')),
    attempt_token       TEXT,
    replay_events_json  TEXT
                        CHECK (replay_events_json IS NULL OR json_valid(replay_events_json)),
    unknown_error_json  TEXT
                        CHECK (unknown_error_json IS NULL OR json_valid(unknown_error_json)),
    created_time        TEXT NOT NULL,
    updated_time        TEXT NOT NULL,
    finished_time       TEXT,
    replay_evicted_time   TEXT,
    PRIMARY KEY (user_id, client_turn_id),
    CHECK (
        (state = 'running'
         AND attempt_token IS NOT NULL
         AND replay_events_json IS NULL
         AND unknown_error_json IS NULL
         AND finished_time IS NULL)
        OR
        (state = 'completed'
         AND attempt_token IS NULL
         AND replay_events_json IS NOT NULL
         AND unknown_error_json IS NULL
         AND finished_time IS NOT NULL)
        OR
        (state = 'unknown'
         AND attempt_token IS NULL
         AND replay_events_json IS NULL
         AND unknown_error_json IS NOT NULL
         AND finished_time IS NOT NULL)
    )
) STRICT;

CREATE TABLE IF NOT EXISTS assistant_turn_cancellations (
    user_id             TEXT NOT NULL CHECK (length(user_id) > 0),
    client_turn_id      TEXT NOT NULL
                        CHECK (
                            length(client_turn_id) = 36
                            AND client_turn_id = lower(client_turn_id)
                            AND substr(client_turn_id, 9, 1) = '-'
                            AND substr(client_turn_id, 14, 1) = '-'
                            AND substr(client_turn_id, 19, 1) = '-'
                            AND substr(client_turn_id, 24, 1) = '-'
                            AND length(replace(client_turn_id, '-', '')) = 32
                            AND replace(client_turn_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                        ),
    created_time        TEXT NOT NULL
                        CHECK (
                            length(created_time) BETWEEN 1 AND 64
                            AND created_time = trim(created_time)
                        ),
    PRIMARY KEY (user_id, client_turn_id)
) STRICT;

CREATE TABLE IF NOT EXISTS application_update_undo_commands (
    user_id        TEXT NOT NULL,
    command_id     TEXT NOT NULL
                   CHECK (
                       length(command_id) = 36
                       AND command_id = lower(command_id)
                       AND substr(command_id, 9, 1) = '-'
                       AND substr(command_id, 14, 1) = '-'
                       AND substr(command_id, 19, 1) = '-'
                       AND substr(command_id, 24, 1) = '-'
                       AND length(replace(command_id, '-', '')) = 32
                       AND replace(command_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                   ),
    operation_id   TEXT NOT NULL
                   CHECK (
                       length(operation_id) = 36
                       AND operation_id = lower(operation_id)
                       AND substr(operation_id, 9, 1) = '-'
                       AND substr(operation_id, 14, 1) = '-'
                       AND substr(operation_id, 19, 1) = '-'
                       AND substr(operation_id, 24, 1) = '-'
                       AND length(replace(operation_id, '-', '')) = 32
                       AND replace(operation_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                   ),
    state           TEXT NOT NULL CHECK (state IN ('completed', 'rejected')),
    error_code      TEXT
                    CHECK (error_code IS NULL OR error_code IN (
                        'operation_not_found', 'operation_invalid', 'target_missing',
                        'target_changed', 'prep_changed', 'lifecycle_changed',
                        'provenance_changed', 'natural_key_taken'
                    )),
    error_message   TEXT
                    CHECK (error_message IS NULL OR (
                        length(error_message) BETWEEN 1 AND 256
                        AND error_message = trim(error_message)
                    )),
    finished_time   TEXT NOT NULL
                    CHECK (
                        length(finished_time) BETWEEN 1 AND 64
                        AND finished_time = trim(finished_time)
                    ),
    PRIMARY KEY (user_id, command_id),
    CHECK (
        (state = 'completed' AND error_code IS NULL AND error_message IS NULL)
        OR
        (state = 'rejected' AND error_code IS NOT NULL AND error_message IS NOT NULL)
    )
) STRICT;

CREATE TABLE IF NOT EXISTS review_timeline_entry_edit_undo_commands (
    user_id        TEXT NOT NULL CHECK (length(user_id) > 0),
    command_id     TEXT NOT NULL
                   CHECK (
                       length(command_id) = 36
                       AND command_id = lower(command_id)
                       AND substr(command_id, 9, 1) = '-'
                       AND substr(command_id, 14, 1) = '-'
                       AND substr(command_id, 19, 1) = '-'
                       AND substr(command_id, 24, 1) = '-'
                       AND length(replace(command_id, '-', '')) = 32
                       AND replace(command_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                   ),
    operation_id   TEXT NOT NULL
                   CHECK (
                       length(operation_id) = 36
                       AND operation_id = lower(operation_id)
                       AND substr(operation_id, 9, 1) = '-'
                       AND substr(operation_id, 14, 1) = '-'
                       AND substr(operation_id, 19, 1) = '-'
                       AND substr(operation_id, 24, 1) = '-'
                       AND length(replace(operation_id, '-', '')) = 32
                       AND replace(operation_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                   ),
    state           TEXT NOT NULL CHECK (state IN ('completed', 'rejected')),
    error_code      TEXT
                    CHECK (error_code IS NULL OR error_code IN (
                        'operation_not_found', 'operation_invalid', 'target_missing',
                        'target_changed', 'lifecycle_changed', 'provenance_changed'
                    )),
    error_message   TEXT
                    CHECK (error_message IS NULL OR (
                        length(error_message) BETWEEN 1 AND 256
                        AND error_message = trim(error_message)
                    )),
    finished_time   TEXT NOT NULL
                    CHECK (
                        length(finished_time) BETWEEN 1 AND 64
                        AND finished_time = trim(finished_time)
                    ),
    PRIMARY KEY (user_id, command_id),
    CHECK (
        (state = 'completed' AND error_code IS NULL AND error_message IS NULL)
        OR
        (state = 'rejected' AND error_code IS NOT NULL AND error_message IS NOT NULL)
    )
) STRICT;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
"""
SCHEMA = (
    SCHEMA.replace("__COMPANY_NAME_KEY_SQL__", application_identity_sql("name"))
    .replace(
        "__APPLICATION_COMPANY_KEY_SQL__",
        application_identity_sql("company"),
    )
    .replace(
        "__APPLICATION_POSITION_KEY_SQL__",
        application_identity_sql("position"),
    )
)

# Secondary indexes match hot reads: board, weakness ranking, pools, and replay.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_journal_user_kind_time
    ON journal (user_id, kind, created_time);
CREATE INDEX IF NOT EXISTS idx_journal_user_state_revision
    ON journal (user_id, state, revision);
CREATE INDEX IF NOT EXISTS idx_journal_user_parent
    ON journal (user_id, parent_journal_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_operation
    ON journal (operation_id)
    WHERE operation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_application_intake_owner_user_journal
    ON application_intake_operation_owners (user_id, journal_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_review_undo_live_target
    ON journal (user_id, parent_journal_id)
    WHERE kind = 'correction' AND operation_id IS NOT NULL
      AND parent_journal_id IS NOT NULL AND state IN ('awaiting_user', 'applied')
      AND json_extract(CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,
                       '$.operation_type') = 'review_undo';
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_application_delete_pending_target
    ON journal (
        user_id,
        CAST(json_extract(
            CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,
            '$.target.application_id'
        ) AS INTEGER)
    )
    WHERE kind = 'correction' AND operation_id IS NOT NULL AND state = 'awaiting_user'
      AND json_extract(CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,
                       '$.operation_type') = 'application_delete'
      AND json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json ELSE '{}' END,
                       '$.operation.type') = 'application_delete';
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_application_merge_pending_source
    ON journal (
        user_id,
        CAST(json_extract(
            CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,
            '$.source.application_id'
        ) AS INTEGER)
    )
    WHERE kind = 'correction' AND operation_id IS NOT NULL AND state = 'awaiting_user'
      AND json_extract(CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,
                       '$.operation_type') = 'application_merge'
      AND json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json ELSE '{}' END,
                       '$.operation.type') = 'application_merge';
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_application_merge_pending_destination
    ON journal (
        user_id,
        CAST(json_extract(
            CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,
            '$.destination.application_id'
        ) AS INTEGER)
    )
    WHERE kind = 'correction' AND operation_id IS NOT NULL AND state = 'awaiting_user'
      AND json_extract(CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,
                       '$.operation_type') = 'application_merge'
      AND json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json ELSE '{}' END,
                       '$.operation.type') = 'application_merge';
CREATE INDEX IF NOT EXISTS idx_journal_review_operations_pending
    ON journal (user_id, state, created_time)
    WHERE kind = 'correction' AND operation_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_review_record_turn
    ON journal (
        user_id,
        json_extract(
            CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,
            '$.client_turn_id'
        ),
        json_extract(
            CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,
            '$.request_digest'
        )
    )
    WHERE kind = 'correction' AND operation_id IS NOT NULL
      AND json_extract(
          CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,
          '$.operation_type'
      ) = 'review_record';
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_review_record_source
    ON journal (
        user_id,
        CAST(json_extract(
            CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,
            '$.source_journal_id'
        ) AS INTEGER)
    )
    WHERE kind = 'correction' AND operation_id IS NOT NULL
      AND json_extract(
          CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,
          '$.operation_type'
      ) = 'review_record';
CREATE INDEX IF NOT EXISTS idx_journal_review_record_target
    ON journal (user_id, parent_journal_id, created_time)
    WHERE kind = 'correction' AND operation_id IS NOT NULL
      AND json_extract(
          CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,
          '$.operation_type'
      ) = 'review_record';
CREATE INDEX IF NOT EXISTS idx_applications_user_stage
    ON applications (user_id, stage);
CREATE INDEX IF NOT EXISTS idx_applications_user_next_date
    ON applications (user_id, next_date) WHERE next_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_timeline_entries_application_time
    ON timeline_entries (application_id, created_time);
CREATE INDEX IF NOT EXISTS idx_timeline_entries_user_date
    ON timeline_entries (user_id, occurred_date);
CREATE INDEX IF NOT EXISTS idx_questions_user_source_status
    ON questions (user_id, source, status);
CREATE INDEX IF NOT EXISTS idx_questions_user_status_created
    ON questions (user_id, status, created_time, id);
CREATE INDEX IF NOT EXISTS idx_questions_user_set_status_created
    ON questions (user_id, question_set_id, status, created_time, id);
CREATE INDEX IF NOT EXISTS idx_question_sets_user_context
    ON question_sets (user_id, kind, edition, state, archived_at, created_time);
CREATE UNIQUE INDEX IF NOT EXISTS uq_question_sets_running_generation
    ON question_sets (user_id, generation_fingerprint)
    WHERE state IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS idx_question_set_items_set
    ON question_set_items (question_set_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_question_set_items_canonical
    ON question_set_items (canonical_question_id, canonical_revision);
CREATE INDEX IF NOT EXISTS idx_question_knowledge_kp
    ON question_knowledge (knowledge_point_id);
CREATE INDEX IF NOT EXISTS idx_review_question_occurrences_question
    ON review_question_occurrences (user_id, question_id);
CREATE INDEX IF NOT EXISTS idx_review_question_occurrences_company
    ON review_question_occurrences (user_id, company);
CREATE INDEX IF NOT EXISTS idx_knowledge_points_user_box
    ON knowledge_points (user_id, box, due_date);
CREATE INDEX IF NOT EXISTS idx_grill_answers_session
    ON grill_answers (session_id);
CREATE INDEX IF NOT EXISTS idx_grill_session_items_session
    ON grill_session_items (session_id, ordinal);
CREATE UNIQUE INDEX IF NOT EXISTS uq_grill_one_active_per_user
    ON grill_sessions (user_id) WHERE state = 'active';
CREATE INDEX IF NOT EXISTS idx_competency_progress_scope_due
    ON competency_progress (user_id, scope_kind, scope_ref, due_date, box);
CREATE INDEX IF NOT EXISTS idx_status_log_user_date
    ON status_log (user_id, log_date);
CREATE INDEX IF NOT EXISTS idx_journal_operation_turn_extraction
    ON journal (
        user_id,
        json_extract(
            CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,
            '$.client_turn_id'
        )
    )
    WHERE operation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_journal_operation_turn_derivation
    ON journal (
        user_id,
        json_extract(
            CASE WHEN json_valid(derivation_json) THEN derivation_json ELSE '{}' END,
            '$.operation.client_turn_id'
        )
    )
    WHERE operation_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_preference_update_turn_extraction
    ON journal (
        user_id,
        json_extract(
            CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,
            '$.client_turn_id'
        )
    )
    WHERE kind = 'correction' AND operation_id IS NOT NULL
      AND json_extract(
          CASE WHEN json_valid(extraction_json) THEN extraction_json ELSE '{}' END,
          '$.operation_type'
      ) = 'preference_update';
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_preference_update_turn_derivation
    ON journal (
        user_id,
        json_extract(
            CASE WHEN json_valid(derivation_json) THEN derivation_json ELSE '{}' END,
            '$.operation.client_turn_id'
        )
    )
    WHERE kind = 'correction' AND operation_id IS NOT NULL
      AND json_extract(
          CASE WHEN json_valid(derivation_json) THEN derivation_json ELSE '{}' END,
          '$.operation.operation_type'
      ) = 'preference_update';
CREATE UNIQUE INDEX IF NOT EXISTS uq_assistant_one_running_per_session
    ON assistant_turns (user_id, session_id)
    WHERE state = 'running';
CREATE INDEX IF NOT EXISTS idx_assistant_completed_retention
    ON assistant_turns (finished_time)
    WHERE state = 'completed' AND replay_evicted_time IS NULL;
CREATE INDEX IF NOT EXISTS idx_application_update_undo_commands_operation
    ON application_update_undo_commands (user_id, operation_id, finished_time);
CREATE INDEX IF NOT EXISTS idx_review_timeline_entry_edit_undo_commands_operation
    ON review_timeline_entry_edit_undo_commands (user_id, operation_id, finished_time);
CREATE INDEX IF NOT EXISTS idx_preference_owners_user
    ON preference_owners (user_id, preference_id);
CREATE INDEX IF NOT EXISTS idx_preference_item_commands_operation
    ON preference_item_commands (user_id, operation_id, finished_time);
CREATE INDEX IF NOT EXISTS idx_preference_item_command_owners_time
    ON preference_item_command_owners (user_id, created_time, command_id);
"""

# Keep indexes/triggers separate so safely rebuildable objects can recover atomically.
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_applications_jd_confirmation_invalidate
AFTER UPDATE OF jd_text ON applications
FOR EACH ROW
WHEN NEW.jd_text IS NOT OLD.jd_text
BEGIN
    UPDATE applications
    SET jd_content_hash = NULL,
        jd_receipt_json = NULL,
        jd_receipt_status = 'unconfirmed'
    WHERE id = NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS trg_application_intake_owner_insert_contract
BEFORE INSERT ON application_intake_operation_owners
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM journal
    WHERE id = NEW.journal_id
      AND user_id = NEW.user_id
      AND kind = 'jd_batch'
      AND operation_id = NEW.operation_id
      AND created_time = NEW.created_time
)
BEGIN
    SELECT RAISE(ABORT, 'application intake owner must match its journal row');
END;
CREATE TRIGGER IF NOT EXISTS trg_application_intake_owner_immutable_update
BEFORE UPDATE ON application_intake_operation_owners
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'application intake owner is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_application_intake_owner_immutable_delete
BEFORE DELETE ON application_intake_operation_owners
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'application intake owner is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_application_intake_journal_identity_update
BEFORE UPDATE OF user_id, kind, operation_id, created_time ON journal
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM application_intake_operation_owners
    WHERE journal_id = OLD.id
) AND (
    NEW.user_id IS NOT OLD.user_id
    OR NEW.kind IS NOT OLD.kind
    OR NEW.operation_id IS NOT OLD.operation_id
    OR NEW.created_time IS NOT OLD.created_time
)
BEGIN
    SELECT RAISE(ABORT, 'application intake journal identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_application_intake_journal_no_delete
BEFORE DELETE ON journal
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM application_intake_operation_owners
    WHERE journal_id = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'application intake journal identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_preferences_insert_revision
BEFORE INSERT ON preferences
FOR EACH ROW
WHEN NEW.revision != 1
BEGIN
    SELECT RAISE(ABORT, 'new preference revision must be 1');
END;
CREATE TRIGGER IF NOT EXISTS trg_preferences_update_contract
BEFORE UPDATE ON preferences
FOR EACH ROW
WHEN NEW.id != OLD.id
  OR NEW.user_id != OLD.user_id
  OR NEW.key != OLD.key
  OR NEW.created_time != OLD.created_time
  OR NEW.value IS OLD.value
  OR NEW.revision != OLD.revision + 1
BEGIN
    SELECT RAISE(ABORT, 'preference update must change value and advance revision once');
END;
CREATE TRIGGER IF NOT EXISTS trg_preference_owner_insert_contract
BEFORE INSERT ON preference_owners
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM preferences
    WHERE id = NEW.preference_id
      AND user_id = NEW.user_id
      AND created_time = NEW.created_time
)
BEGIN
    SELECT RAISE(ABORT, 'preference owner must match its active row');
END;
CREATE TRIGGER IF NOT EXISTS trg_preference_owner_immutable_update
BEFORE UPDATE ON preference_owners
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'preference owner is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_preference_owner_immutable_delete
BEFORE DELETE ON preference_owners
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'preference owner is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_preference_create_owner
AFTER INSERT ON preferences
FOR EACH ROW
BEGIN
    INSERT INTO preference_owners (preference_id, user_id, created_time)
    VALUES (NEW.id, NEW.user_id, NEW.created_time);
END;
CREATE TRIGGER IF NOT EXISTS trg_preference_item_command_insert_contract
BEFORE INSERT ON preference_item_commands
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM preference_item_command_owners
    WHERE user_id = NEW.user_id
      AND command_id = NEW.command_id
      AND created_time = NEW.finished_time
) OR (NEW.journal_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM journal
    WHERE id = NEW.journal_id
      AND user_id = NEW.user_id
      AND kind = 'correction'
      AND operation_id = NEW.operation_id
      AND state = 'applied'
      AND revision = 0
      AND parent_journal_id IS NULL
      AND created_time = NEW.finished_time
      AND processed_time = NEW.finished_time
      AND json_extract(extraction_json, '$.operation_type') = 'preference_item_change'
      AND json_extract(extraction_json, '$.command_id') = NEW.command_id
      AND json_extract(extraction_json, '$.operation_id') = NEW.operation_id
      AND json_extract(derivation_json, '$.operation.operation_type') = 'preference_item_change'
      AND json_extract(derivation_json, '$.operation.command_id') = NEW.command_id
      AND json_extract(derivation_json, '$.operation.operation_id') = NEW.operation_id
))
BEGIN
    SELECT RAISE(ABORT, 'preference item command must match its journal receipt');
END;
CREATE TRIGGER IF NOT EXISTS trg_preference_item_command_owner_immutable_update
BEFORE UPDATE ON preference_item_command_owners
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'preference item command owner is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_preference_item_command_owner_immutable_delete
BEFORE DELETE ON preference_item_command_owners
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'preference item command owner is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_preference_item_command_immutable_update
BEFORE UPDATE ON preference_item_commands
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'preference item command is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_preference_item_command_immutable_delete
BEFORE DELETE ON preference_item_commands
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'preference item command is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_preference_item_command_journal_immutable_update
BEFORE UPDATE ON journal
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM preference_item_commands WHERE journal_id = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'preference item command journal is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_preference_item_command_journal_immutable_delete
BEFORE DELETE ON journal
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM preference_item_commands WHERE journal_id = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'preference item command journal is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_assistant_turn_reject_cancelled
BEFORE INSERT ON assistant_turns
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM assistant_turn_cancellations
    WHERE user_id = NEW.user_id AND client_turn_id = NEW.client_turn_id
)
BEGIN
    SELECT RAISE(ABORT, 'assistant turn id is permanently cancelled');
END;
CREATE TRIGGER IF NOT EXISTS trg_assistant_cancellation_reject_existing
BEFORE INSERT ON assistant_turn_cancellations
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM assistant_turns
    WHERE user_id = NEW.user_id AND client_turn_id = NEW.client_turn_id
)
BEGIN
    SELECT RAISE(ABORT, 'assistant turn id already exists');
END;
"""

# ── Schema version ────────────────────────────────────────────────────
# Fresh r1 domain model; the physical version prevents older binary rollback.
SCHEMA_VERSION = 28

# Versioned physical-schema contract. These hashes describe semantic PRAGMA
# manifests, not database files, and are verified before a database is trusted.
_FRESH_CURRENT_TABLE_PROFILE_DIGEST = (
    "3ba07acef6e9739c066642a157bc707f6762555e3cbf3e53e04b4a2b017a6fa6"
)
_FRESH_CURRENT_INDEX_MANIFEST_DIGEST = (
    "b7d806b866ffcbca967d789592613d7174121d8051694326a374fd51afc15606"
)
_FRESH_CURRENT_TRIGGER_MANIFEST_DIGEST = (
    "2d8cfb8c06deaf6397450e05801104a4c0ce30e3869f2979acdcc6923d59f0e0"
)
@dataclass(frozen=True)
class _ReferenceManifest:
    required_objects: frozenset[tuple[str, str]]
    table_names: tuple[str, ...]
    index_fingerprints: tuple[tuple[str, str], ...]
    trigger_fingerprints: tuple[tuple[str, str], ...]


def _manifest_digest(value) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _canonical_sql(sql: str) -> str:
    output: list[str] = []
    index = 0
    pending_space = False
    quote_end: str | None = None
    while index < len(sql):
        character = sql[index]
        if quote_end is not None:
            output.append(character)
            if character == quote_end:
                if (
                    quote_end != "]"
                    and index + 1 < len(sql)
                    and sql[index + 1] == quote_end
                ):
                    output.append(sql[index + 1])
                    index += 2
                    continue
                quote_end = None
            index += 1
            continue
        if character in {"'", '"', "`"}:
            if pending_space and output:
                output.append(" ")
            pending_space = False
            quote_end = character
            output.append(character)
            index += 1
            continue
        if character == "[":
            if pending_space and output:
                output.append(" ")
            pending_space = False
            quote_end = "]"
            output.append(character)
            index += 1
            continue
        if character == "-" and index + 1 < len(sql) and sql[index + 1] == "-":
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            pending_space = True
            continue
        if character.isspace():
            pending_space = True
            index += 1
            continue
        if pending_space and output:
            output.append(" ")
        pending_space = False
        output.append(character)
        index += 1
    return "".join(output).strip()


def _sql_digest(sql: str | None) -> str:
    if not isinstance(sql, str) or not sql.strip():
        raise RuntimeError("schema manifest 无法读取对象 SQL 定义，启动已中止")
    return hashlib.sha256(_canonical_sql(sql).encode()).hexdigest()


def _schema_object_row(
    conn: sqlite3.Connection,
    name: str,
    expected_type: str,
) -> tuple[str, str]:
    rows = conn.execute(
        "SELECT type, tbl_name, sql FROM sqlite_schema WHERE name = ?",
        (name,),
    ).fetchall()
    if len(rows) != 1 or rows[0][0] != expected_type:
        labels = {"table": "表", "index": "索引", "trigger": "触发器"}
        raise RuntimeError(
            f"schema manifest 中的必需{labels[expected_type]} {name} 类型或身份不匹配；"
            "启动已中止，请从已验证备份恢复"
        )
    _, table_name, sql = rows[0]
    return table_name, sql


def _key_columns(conn: sqlite3.Connection, index_name: str) -> tuple[tuple, ...]:
    rows = conn.execute(
        'SELECT seqno, cid, name, "desc", coll, key '
        "FROM pragma_index_xinfo(?) ORDER BY seqno",
        (index_name,),
    ).fetchall()
    return tuple(
        (seqno, name, descending, collation, cid == -2)
        for seqno, cid, name, descending, collation, is_key in rows
        if is_key == 1
    )


def _foreign_key_groups(conn: sqlite3.Connection, table_name: str) -> tuple:
    rows = conn.execute(
        'SELECT id, seq, "table", "from", "to", on_update, on_delete, match '
        "FROM pragma_foreign_key_list(?) ORDER BY id, seq",
        (table_name,),
    ).fetchall()
    groups: dict[int, list[tuple]] = {}
    for (
        foreign_key_id,
        sequence,
        target_table,
        source_column,
        target_column,
        on_update,
        on_delete,
        match,
    ) in rows:
        groups.setdefault(foreign_key_id, []).append(
            (
                sequence,
                target_table,
                source_column,
                target_column,
                on_update,
                on_delete,
                match,
            )
        )
    return tuple(
        sorted(
            (tuple(group) for group in groups.values()),
            key=_manifest_digest,
        )
    )


def _table_manifest_entry(conn: sqlite3.Connection, table_name: str) -> dict:
    _, sql = _schema_object_row(conn, table_name, "table")
    table_list = conn.execute(
        "SELECT type, ncol, wr, strict FROM pragma_table_list "
        "WHERE schema = 'main' AND name = ?",
        (table_name,),
    ).fetchall()
    if len(table_list) != 1:
        raise RuntimeError(
            f"schema manifest 无法唯一定位必需表 {table_name}；启动已中止"
        )
    columns = conn.execute(
        'SELECT cid, name, type, "notnull", dflt_value, pk, hidden '
        "FROM pragma_table_xinfo(?) ORDER BY cid",
        (table_name,),
    ).fetchall()
    implicit_indexes = []
    for index_name, unique, origin, partial in conn.execute(
        'SELECT name, "unique", origin, partial FROM pragma_index_list(?) '
        "WHERE origin IN ('u', 'pk')",
        (table_name,),
    ).fetchall():
        implicit_indexes.append(
            {
                "unique": unique,
                "origin": origin,
                "partial": partial,
                "keys": _key_columns(conn, index_name),
            }
        )
    implicit_indexes.sort(key=_manifest_digest)
    return {
        "table": table_list[0],
        "columns": columns,
        "foreign_keys": _foreign_key_groups(conn, table_name),
        "implicit_indexes": implicit_indexes,
        "sql_sha256": _sql_digest(sql),
    }


def _table_profile_digest(
    conn: sqlite3.Connection,
    table_names: tuple[str, ...],
) -> str:
    return _manifest_digest(
        {name: _table_manifest_entry(conn, name) for name in table_names}
    )


def _index_fingerprint(conn: sqlite3.Connection, index_name: str) -> str:
    table_name, sql = _schema_object_row(conn, index_name, "index")
    index_rows = conn.execute(
        'SELECT "unique", origin, partial FROM pragma_index_list(?) WHERE name = ?',
        (table_name, index_name),
    ).fetchall()
    if len(index_rows) != 1:
        raise RuntimeError(
            f"schema manifest 无法唯一定位必需索引 {index_name}；启动已中止"
        )
    unique, origin, partial = index_rows[0]
    keys = _key_columns(conn, index_name)
    has_expression = any(key[-1] for key in keys)
    manifest = {
        "table": table_name,
        "unique": unique,
        "origin": origin,
        "partial": partial,
        "keys": keys,
    }
    # index_xinfo only reports cid=-2 for an expression and cannot expose a
    # partial predicate.  Exact stored SQL closes those two semantic gaps.
    if partial or has_expression:
        manifest["sql_sha256"] = _sql_digest(sql)
    return _manifest_digest(manifest)


def _trigger_fingerprint(conn: sqlite3.Connection, trigger_name: str) -> str:
    table_name, sql = _schema_object_row(conn, trigger_name, "trigger")
    return _manifest_digest(
        {"table": table_name, "sql_sha256": _sql_digest(sql)}
    )


def _build_reference_manifest(
    indexes: str,
    *,
    schema_sql: str = SCHEMA,
) -> tuple[_ReferenceManifest, dict[str, str]]:
    with closing(sqlite3.connect(":memory:")) as reference:
        reference.executescript(f"{schema_sql}\n{indexes}\n{TRIGGERS}")
        rows = reference.execute(
            "SELECT type, name FROM sqlite_schema "
            "WHERE type IN ('table', 'index', 'trigger') "
            "AND name NOT LIKE 'sqlite_%'",
        ).fetchall()
        required_objects = frozenset(
            (object_type, name) for object_type, name in rows
        )
        table_names = tuple(
            sorted(name for object_type, name in rows if object_type == "table")
        )
        index_fingerprints = tuple(
            sorted(
                (name, _index_fingerprint(reference, name))
                for object_type, name in rows
                if object_type == "index"
            )
        )
        trigger_fingerprints = tuple(
            sorted(
                (name, _trigger_fingerprint(reference, name))
                for object_type, name in rows
                if object_type == "trigger"
            )
        )
        actual_digests = {
            "tables": _table_profile_digest(reference, table_names),
            "indexes": _manifest_digest(dict(index_fingerprints)),
            "triggers": _manifest_digest(dict(trigger_fingerprints)),
        }
    return _ReferenceManifest(
        required_objects=required_objects,
        table_names=table_names,
        index_fingerprints=index_fingerprints,
        trigger_fingerprints=trigger_fingerprints,
    ), actual_digests


@cache
def _fresh_current_reference_manifest() -> _ReferenceManifest:
    manifest, actual_digests = _build_reference_manifest(INDEXES)
    expected_digests = {
        "tables": _FRESH_CURRENT_TABLE_PROFILE_DIGEST,
        "indexes": _FRESH_CURRENT_INDEX_MANIFEST_DIGEST,
        "triggers": _FRESH_CURRENT_TRIGGER_MANIFEST_DIGEST,
    }
    if actual_digests != expected_digests:
        raise RuntimeError(
            f"内置 fresh schema r{FRESH_SCHEMA_REVISION}（物理 v{SCHEMA_VERSION}）"
            "声明与版本化 schema manifest 不一致；"
            "启动已中止，请先显式更新版本化 manifest"
        )
    return manifest


@cache
def _required_schema_objects() -> frozenset[tuple[str, str]]:
    """Derive required object names without restricting third-party extensions."""
    return _fresh_current_reference_manifest().required_objects


def _assert_required_schema_objects(
    conn: sqlite3.Connection,
    *,
    object_types: frozenset[str] = frozenset({"table", "index", "trigger"}),
) -> None:
    actual = frozenset(
        (object_type, name)
        for object_type, name in conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger') "
            "AND name NOT LIKE 'sqlite_%'",
        ).fetchall()
    )
    missing = sorted(
        required
        for required in _required_schema_objects() - actual
        if required[0] in object_types
    )
    if not missing:
        return
    labels = {"table": "表", "index": "索引", "trigger": "触发器"}
    preview = "、".join(f"{labels[kind]} {name}" for kind, name in missing[:8])
    if len(missing) > 8:
        preview = f"{preview} 等 {len(missing)} 项"
    raise RuntimeError(
        f"当前数据库声明为 v{SCHEMA_VERSION}，但缺少必需 schema 对象：{preview}；"
        "为避免用空结构掩盖数据丢失，启动已中止，请从已验证备份恢复"
    )


def _raise_schema_manifest_mismatch(object_label: str, *, version: int = SCHEMA_VERSION) -> None:
    raise RuntimeError(
        f"当前数据库声明为 v{version}，但 {object_label} 的定义不匹配 "
        f"v{version} schema manifest；启动已中止，请从已验证备份恢复"
    )


def _assert_schema_manifest(
    conn: sqlite3.Connection,
    *,
    reference: _ReferenceManifest,
    table_profile_digest: str,
    version: int,
    allow_missing_derived: bool,
) -> None:
    required_types = (
        frozenset({"table"})
        if allow_missing_derived
        else frozenset({"table", "index", "trigger"})
    )
    _assert_required_schema_objects(conn, object_types=required_types)

    table_profile = _table_profile_digest(conn, reference.table_names)
    if table_profile != table_profile_digest:
        _raise_schema_manifest_mismatch("必需表闭合物理 profile", version=version)

    required_objects = reference.required_objects
    core_tables = frozenset(reference.table_names)
    core_table_keys = frozenset(name.casefold() for name in core_tables)
    unmanifested_core_objects = sorted(
        (object_type, name, table_name)
        for object_type, name, table_name in conn.execute(
            "SELECT type, name, tbl_name FROM sqlite_schema "
            "WHERE type IN ('index', 'trigger') "
            "AND name NOT LIKE 'sqlite_%'",
        ).fetchall()
        if table_name.casefold() in core_table_keys
        and (object_type, name) not in required_objects
    )
    if unmanifested_core_objects:
        object_type, name, table_name = unmanifested_core_objects[0]
        labels = {"index": "索引", "trigger": "触发器"}
        _raise_schema_manifest_mismatch(
            f"核心表 {table_name} 上未登记的{labels[object_type]} {name}",
            version=version,
        )

    extension_tables = sorted(
        name
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
        ).fetchall()
        if name not in core_tables
    )
    for extension_table in extension_tables:
        inbound_core_targets = sorted(
            {
                target_table
                for (target_table,) in conn.execute(
                    'SELECT "table" FROM pragma_foreign_key_list(?)',
                    (extension_table,),
                ).fetchall()
                if target_table.casefold() in core_table_keys
            }
        )
        if inbound_core_targets:
            _raise_schema_manifest_mismatch(
                f"扩展表 {extension_table} 指向核心表 {inbound_core_targets[0]} 的外键",
                version=version,
            )

    expected_indexes = dict(reference.index_fingerprints)
    for index_name, expected_fingerprint in expected_indexes.items():
        row = conn.execute(
            "SELECT type FROM sqlite_schema WHERE name = ?",
            (index_name,),
        ).fetchone()
        if row is None:
            if allow_missing_derived:
                continue
            _raise_schema_manifest_mismatch(f"必需索引 {index_name}", version=version)
        if row[0] != "index":
            _raise_schema_manifest_mismatch(f"必需索引 {index_name}", version=version)
        if _index_fingerprint(conn, index_name) != expected_fingerprint:
            _raise_schema_manifest_mismatch(f"必需索引 {index_name}", version=version)

    for trigger_name, expected_fingerprint in reference.trigger_fingerprints:
        row = conn.execute(
            "SELECT type FROM sqlite_schema WHERE name = ?",
            (trigger_name,),
        ).fetchone()
        if row is None:
            if allow_missing_derived:
                continue
            _raise_schema_manifest_mismatch(f"必需触发器 {trigger_name}", version=version)
        if row[0] != "trigger":
            _raise_schema_manifest_mismatch(f"必需触发器 {trigger_name}", version=version)
        if _trigger_fingerprint(conn, trigger_name) != expected_fingerprint:
            _raise_schema_manifest_mismatch(f"必需触发器 {trigger_name}", version=version)


def assert_current_schema_manifest(
    conn: sqlite3.Connection,
    *,
    allow_missing_derived: bool,
) -> None:
    _assert_schema_manifest(
        conn,
        reference=_fresh_current_reference_manifest(),
        table_profile_digest=_FRESH_CURRENT_TABLE_PROFILE_DIGEST,
        version=SCHEMA_VERSION,
        allow_missing_derived=allow_missing_derived,
    )


def assert_supported_schema_version(current_version: int) -> None:
    if current_version < 0:
        raise RuntimeError(
            f"数据库版本 v{current_version} 无效；"
            "为避免把损坏的版本标记当作有效数据库，启动已中止，请从已验证备份恢复"
        )
    if current_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库版本 v{current_version} 高于当前程序支持的 v{SCHEMA_VERSION}；"
            "为避免旧版本程序改写未来数据库，启动已中止，"
            "请改用更新版本的 CareerDesk 打开该数据目录"
        )
    if 0 < current_version < SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库版本 v{current_version} 不是当前 fresh-only "
            f"v{SCHEMA_VERSION}；本仓库不提供旧 schema 迁移"
        )


def assert_database_shape_before_init(
    conn: sqlite3.Connection,
    current_version: int,
) -> None:
    (existing_objects,) = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master"
    ).fetchone()
    (existing_tables,) = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()
    if current_version == 0 and existing_objects != 0:
        raise RuntimeError(
            "数据库未记录 schema 版本但已包含 schema 对象；"
            "为避免把未知结构当作有效数据库，启动已中止，请从已验证备份恢复"
        )
    if current_version != 0 and existing_tables == 0:
        raise RuntimeError(
            f"数据库记录为 v{current_version}，但没有任何表；"
            "为避免把损坏数据库当作新库覆盖，启动已中止，请从已验证备份恢复"
        )
    if current_version == SCHEMA_VERSION:
        # Never “heal” missing or drifted user-data tables with empty replacements.
        # Indexes/triggers may be absent after interruption, but mismatched definitions
        # are rejected by the manifest before any read-write open.
        assert_current_schema_manifest(
            conn,
            allow_missing_derived=True,
        )
