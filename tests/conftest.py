from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ADMIN = "test-admin-token"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DAH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DAH_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("DAH_SECRET_KEY", "test-secret-key-0123456789")
    monkeypatch.setenv("DAH_PUBLIC_BASE_URL", "http://testserver")
    from dah import config, db

    from dah.auth import identity

    config.get_settings.cache_clear()
    db.reset_engine()
    identity._failures.clear()
    yield tmp_path
    db.reset_engine()
    config.get_settings.cache_clear()


@pytest.fixture()
def client(env):
    from dah.api.app import create_app

    with TestClient(create_app()) as c:
        c.headers["Authorization"] = f"Bearer {ADMIN}"
        yield c


@pytest.fixture()
def anon(env):
    from dah.api.app import create_app

    with TestClient(create_app()) as c:
        yield c
