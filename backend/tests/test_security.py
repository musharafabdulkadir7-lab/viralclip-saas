import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("WORKER_SECRET", "test-secret-value")
os.environ.setdefault("JWT_SIGNING_KEY", "test-jwt-signing-key")

from app.security import (  # noqa: E402
    issue_session_token,
    stable_user_id_for_email,
    verify_session_token,
    verify_worker_token,
    sign_worker_token,
    verify_admin,
)



def test_session_token_roundtrip():
    token = issue_session_token("user_abc", "person@example.com")
    data = verify_session_token(token)
    assert data is not None
    assert data["sub"] == "user_abc"
    assert data["email"] == "person@example.com"


def test_session_token_rejects_garbage():
    assert verify_session_token("not-a-real-token") is None


def test_stable_user_id_is_deterministic():
    a = stable_user_id_for_email("Person@Example.com")
    b = stable_user_id_for_email("person@example.com ")
    assert a == b
    assert a.startswith("user_")


def test_worker_token_roundtrip():
    token = sign_worker_token("user_abc", purpose="poll")
    assert verify_worker_token("user_abc", token, purpose="poll")


def test_worker_token_wrong_purpose_rejected():
    token = sign_worker_token("user_abc", purpose="poll")
    assert not verify_worker_token("user_abc", token, purpose="creds")


def test_worker_token_wrong_user_rejected():
    token = sign_worker_token("user_abc", purpose="poll")
    assert not verify_worker_token("user_xyz", token, purpose="poll")


def test_worker_token_scopes_complete_progress_analyze():
    for purpose in ("complete", "progress", "analyze"):
        token = sign_worker_token("user_abc", purpose=purpose)
        assert verify_worker_token("user_abc", token, purpose=purpose)
        assert not verify_worker_token("user_abc", token, purpose="poll")


def test_worker_secret_rotation(monkeypatch):
    from app.config import get_settings
    settings = get_settings()

    monkeypatch.setattr(settings, "worker_secret", "old-secret")
    token_old = sign_worker_token("user_abc", purpose="poll")

    # Rotate secret: old becomes previous, new becomes current
    monkeypatch.setattr(settings, "worker_secret", "new-secret")
    monkeypatch.setattr(settings, "worker_secret_previous", "old-secret")

    token_new = sign_worker_token("user_abc", purpose="poll")

    # Both tokens signed with current and previous secrets verify successfully
    assert verify_worker_token("user_abc", token_new, purpose="poll")
    assert verify_worker_token("user_abc", token_old, purpose="poll")

    # Wrong secret rejected
    monkeypatch.setattr(settings, "worker_secret", "other-secret")
    monkeypatch.setattr(settings, "worker_secret_previous", "yet-another")
    assert not verify_worker_token("user_abc", token_new, purpose="poll")



def test_admin_secret_rotation(monkeypatch):
    from fastapi import HTTPException, Request
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "admin_secret", "new-secret")
    monkeypatch.setattr(settings, "admin_secret_previous", "old-secret")

    req_new = Request({"type": "http", "headers": [(b"x-admin-secret", b"new-secret")]})
    req_old = Request({"type": "http", "headers": [(b"x-admin-secret", b"old-secret")]})
    req_bad = Request({"type": "http", "headers": [(b"x-admin-secret", b"bad-secret")]})

    # Both new and previous secrets succeed
    verify_admin(req_new)
    verify_admin(req_old)

    import pytest
    with pytest.raises(HTTPException) as exc:
        verify_admin(req_bad)
    assert exc.value.status_code == 403


def test_atomic_free_tier_consumption_and_refund():
    from app.db import UserRepo
    uid = "test_user_atomic_quota"
    user = UserRepo.get_or_create(uid)
    user["free_clips_used"] = 0
    user["license"] = "free_tier"

    # Consuming under limit allows
    allowed, count = UserRepo.atomic_consume_free_clip(uid, limit=1)
    assert allowed is True
    assert count == 1

    # Second consumption at or over limit is rejected
    allowed2, count2 = UserRepo.atomic_consume_free_clip(uid, limit=1)
    assert allowed2 is False
    assert count2 == 1

    # Compensating refund restores quota
    UserRepo.refund_free_clip(uid)
    assert UserRepo.get_or_create(uid)["free_clips_used"] == 0

    # Can consume again after refund
    allowed3, count3 = UserRepo.atomic_consume_free_clip(uid, limit=1)
    assert allowed3 is True
    assert count3 == 1


import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_failover_redis_fails_over_on_connection_error():
    from app.redis_client import FailoverRedis

    fr = FailoverRedis("redis://primary-down:6379", "redis://secondary-up:6379")

    # Mock clients
    mock_primary = AsyncMock()
    mock_primary.get.side_effect = ConnectionError("Connection refused")
    mock_secondary = AsyncMock()
    mock_secondary.get.return_value = "cached_val"

    fr.primary = mock_primary
    fr.secondary = mock_secondary

    res = await fr.get("mykey")
    assert res == "cached_val"
    assert mock_primary.get.called
    assert mock_secondary.get.called


@pytest.mark.asyncio
async def test_start_google_login_handles_redis_down_and_missing_client_id(monkeypatch):
    from app.config import get_settings
    from app.routers.auth import start_google_login

    settings = get_settings()

    # 1. When google_client_id is not configured, redirect with error without 500 crash
    monkeypatch.setattr(settings, "google_client_id", "")
    resp_unconfigured = await start_google_login()
    assert resp_unconfigured.status_code == 307 or resp_unconfigured.status_code == 302
    assert "detail=not_configured" in resp_unconfigured.headers["location"]

    # 2. When google_client_id is set, even if Redis throws connection error, redirects safely and sets cookie fallback
    monkeypatch.setattr(settings, "google_client_id", "test_google_client_id")
    import app.routers.auth as auth_mod
    broken_redis = AsyncMock()
    broken_redis.setex.side_effect = ConnectionError("Redis host unreachable")
    monkeypatch.setattr(auth_mod, "get_redis", lambda: broken_redis)

    resp_with_broken_redis = await start_google_login()
    assert resp_with_broken_redis.status_code == 307 or resp_with_broken_redis.status_code == 302
    assert "accounts.google.com" in resp_with_broken_redis.headers["location"]
    # Verify fallback cookie was set
    assert "clipai_oauth_login" in resp_with_broken_redis.headers.get("set-cookie", "")