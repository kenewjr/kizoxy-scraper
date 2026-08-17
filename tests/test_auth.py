from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

client = TestClient(app)


def test_health_no_auth_required():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "kizoxy-scraper"


def test_auth_missing_header():
    response = client.get("/youtube/channel/UC123/latest")
    assert response.status_code in (403, 422)


def test_auth_invalid_header():
    response = client.get(
        "/youtube/channel/UC123/latest", headers={"X-Api-Key": "wrong-secret"}
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}


@patch.object(settings, "api_key", "test-secret-123")
@patch("app.youtube.extractor.get_channel_latest_videos", return_value=[])
def test_auth_valid_header(mock_extractor):
    response = client.get(
        "/youtube/channel/UC123/latest", headers={"X-Api-Key": "test-secret-123"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True


@patch.object(settings, "api_key", "test-secret-123")
def test_auth_duplicate_header_value_still_rejected(capsys):
    response = client.get(
        "/youtube/channel/UC123/latest",
        headers={"X-Api-Key": "test-secret-123, test-secret-123"},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}
    assert "same API key header" in capsys.readouterr().out
