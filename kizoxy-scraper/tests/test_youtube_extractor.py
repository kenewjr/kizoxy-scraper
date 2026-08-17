from unittest.mock import MagicMock, patch

import pytest
import yt_dlp

from app.errors import NotFoundException
from app.youtube.extractor import check_channel_live, get_channel_latest_videos


def test_get_channel_latest_videos_mocked():
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.extract_info.return_value = {
        "entries": [
            {
                "id": "vid123",
                "title": "Test Video 1",
                "upload_date": "20260811",
                "url": "https://www.youtube.com/watch?v=vid123",
            }
        ]
    }

    with patch("yt_dlp.YoutubeDL") as mock_ydl_cls:
        mock_ydl_cls.return_value.__enter__.return_value = mock_ydl_instance
        videos = get_channel_latest_videos("UC_test", limit=1)
        assert len(videos) == 1
        assert videos[0]["id"] == "vid123"
        assert videos[0]["title"] == "Test Video 1"


def test_check_channel_live_mocked():
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.extract_info.return_value = {
        "is_live": True,
        "id": "live123",
        "title": "Live Stream Title",
    }

    with patch("yt_dlp.YoutubeDL") as mock_ydl_cls:
        mock_ydl_cls.return_value.__enter__.return_value = mock_ydl_instance
        status = check_channel_live("UC_test")
        assert status["is_live"] is True
        assert status["video_id"] == "live123"
        assert status["title"] == "Live Stream Title"


def test_check_channel_live_not_found():
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.extract_info.side_effect = yt_dlp.utils.DownloadError(
        "ERROR: [youtube] UC_fake: This channel does not exist. 404"
    )

    with patch("yt_dlp.YoutubeDL") as mock_ydl_cls:
        mock_ydl_cls.return_value.__enter__.return_value = mock_ydl_instance
        with pytest.raises(NotFoundException):
            check_channel_live("UC_fake")


@pytest.mark.live
def test_youtube_live_integration():
    videos = get_channel_latest_videos("@YouTube", limit=2)
    assert isinstance(videos, list)
    assert len(videos) > 0
