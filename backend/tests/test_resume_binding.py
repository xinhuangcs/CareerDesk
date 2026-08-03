"""Applications-owned resume binding HTTP and persistence boundaries."""

import json

from fastapi.testclient import TestClient

from careerdesk.core.config import get_settings
from careerdesk.platform.database import init_db, now_iso, read_connection, transaction


def _seed(db_path: str, user_id: str) -> tuple[int, int, int, int]:
    prep = {
        "research_snapshot": {"last_success": True},
        "research_attempt": {"attempt_state": "succeeded"},
        "resume_adaptation": {"old": True},
        "resume_adaptation_summary": {"old": True},
        "nontech_answers": [{"old": True}],
        "unknown": {"keep": True},
    }
    with transaction(db_path) as conn:
        application_id = conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, prep_status, prep_json, created_time, updated_time) "
            "VALUES (?, '示例公司', '产品经理', 'ready', ?, ?, ?)",
            (user_id, json.dumps(prep), now_iso(), now_iso()),
        ).lastrowid
        active_resume_id = conn.execute(
            "INSERT INTO resumes "
            "(user_id, name, content_text, content_hash, extraction_receipt_json, segments_json, "
            "binding, archived, created_time, updated_time) VALUES "
            "(?, '产品岗位版', 'active', ?, '{}', '[]', 'family', 0, ?, ?)",
            (user_id, "0" * 64, now_iso(), now_iso()),
        ).lastrowid
        archived_resume_id = conn.execute(
            "INSERT INTO resumes "
            "(user_id, name, content_text, content_hash, extraction_receipt_json, segments_json, "
            "binding, archived, created_time, updated_time) VALUES "
            "(?, '已归档版', 'archived', ?, '{}', '[]', 'family', 1, ?, ?)",
            (user_id, "1" * 64, now_iso(), now_iso()),
        ).lastrowid
        foreign_resume_id = conn.execute(
            "INSERT INTO resumes "
            "(user_id, name, content_text, content_hash, extraction_receipt_json, segments_json, "
            "binding, archived, created_time, updated_time) VALUES "
            "('another-user', '他人版本', 'foreign', ?, '{}', '[]', 'family', 0, ?, ?)",
            ("2" * 64, now_iso(), now_iso()),
        ).lastrowid
    return application_id, active_resume_id, archived_resume_id, foreign_resume_id


def test_resume_binding_endpoint_guards_tenant_archive_cas_and_precise_invalidation(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_DEV_FAKE_USER", "u1")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    application_id, active_id, archived_id, foreign_id = _seed(
        settings.db_path,
        settings.dev_fake_user,
    )

    from careerdesk.bootstrap.app import create_app

    try:
        with TestClient(create_app()) as client:
            response = client.put(
                f"/api/timeline/applications/{application_id}/resume-binding",
                json={"resume_id": active_id, "expected_edit_revision": 0},
            )
            assert response.status_code == 200
            assert response.json()["resume_id"] == active_id
            assert response.json()["edit_revision"] == 1

            stale = client.put(
                f"/api/timeline/applications/{application_id}/resume-binding",
                json={"resume_id": None, "expected_edit_revision": 0},
            )
            assert stale.status_code == 409

            archived = client.put(
                f"/api/timeline/applications/{application_id}/resume-binding",
                json={"resume_id": archived_id, "expected_edit_revision": 1},
            )
            assert archived.status_code == 422

            foreign = client.put(
                f"/api/timeline/applications/{application_id}/resume-binding",
                json={"resume_id": foreign_id, "expected_edit_revision": 1},
            )
            assert foreign.status_code == 422

        with read_connection(settings.db_path) as conn:
            resume_id, revision, prep_status, raw_prep = conn.execute(
                "SELECT resume_id, revision, prep_status, prep_json "
                "FROM applications WHERE user_id = ? AND id = ?",
                (settings.dev_fake_user, application_id),
            ).fetchone()
        prep = json.loads(raw_prep)
        assert (resume_id, revision, prep_status) == (active_id, 1, "ready")
        assert prep["research_snapshot"] == {"last_success": True}
        assert prep["research_attempt"] == {"attempt_state": "succeeded"}
        assert prep["unknown"] == {"keep": True}
        assert prep["nontech_answers"] == [{"old": True}]
        assert not {
            "resume_adaptation",
            "resume_adaptation_summary",
        }.intersection(prep)
    finally:
        get_settings.cache_clear()


def test_resume_binding_endpoint_does_not_cross_application_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_DEV_FAKE_USER", "u1")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    foreign_application_id, _active_id, _archived_id, _foreign_id = _seed(
        settings.db_path,
        "another-user",
    )

    from careerdesk.bootstrap.app import create_app

    try:
        with TestClient(create_app()) as client:
            response = client.put(
                f"/api/timeline/applications/{foreign_application_id}/resume-binding",
                json={"resume_id": None, "expected_edit_revision": 0},
            )
        assert response.status_code == 404
    finally:
        get_settings.cache_clear()
