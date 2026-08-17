import os

import pytest

os.environ["API_SHARED_SECRET"] = "test-secret-123"
os.environ["BIND_HOST"] = "127.0.0.1"
os.environ["BIND_PORT"] = "8100"
os.environ["BROWSER_POOL_SIZE"] = "2"


@pytest.fixture
def api_secret():
    return "test-secret-123"
