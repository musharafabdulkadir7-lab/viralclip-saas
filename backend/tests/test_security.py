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