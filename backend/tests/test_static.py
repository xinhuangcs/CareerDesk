
import pytest
from fastapi.testclient import TestClient

from careerdesk.core.config import get_settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><title>CareerDesk</title><div id=root></div>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log('hi')", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (dist / "careerdesk-job-import-example.xlsx").write_bytes(b"example workbook")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_FRONTEND_DIST_DIR", str(dist))
    monkeypatch.setenv("APP_LLM_MODEL", "")
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_root_serves_index(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "CareerDesk" in r.text


def test_deep_link_falls_back_to_index(client):
    r = client.get("/grill")
    assert r.status_code == 200
    assert "<div id=root>" in r.text


def test_assets_served(client):
    r = client.get("/assets/app.js")
    assert r.status_code == 200
    assert "console.log" in r.text


def test_root_static_file_served(client):
    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert "<svg" in r.text


def test_spreadsheet_static_file_is_forced_to_download(client):
    r = client.get("/careerdesk-job-import-example.xlsx")
    assert r.status_code == 200
    assert r.content == b"example workbook"
    assert r.headers["content-disposition"] == (
        'attachment; filename="careerdesk-job-import-example.xlsx"'
    )


def test_api_and_healthz_not_shadowed(client):
    assert client.get("/healthz").text == "ok"
    assert client.get("/api/does-not-exist").status_code == 404


def test_path_traversal_blocked(client, tmp_path):
    (tmp_path / "secret.txt").write_text("TOPSECRET", encoding="utf-8")
    for path in ("/..%2fsecret.txt", "/%2e%2e%2fsecret.txt", "/../secret.txt"):
        assert "TOPSECRET" not in client.get(path).text
