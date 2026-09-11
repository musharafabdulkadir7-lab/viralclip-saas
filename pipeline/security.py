"""Must stay in lockstep with backend/app/security.py's verify_worker_token.
Time-boxed (5 min windows) + purpose-scoped HMAC, instead of v2's
unbounded HMAC(WORKER_SECRET, user_id) that was valid forever."""
from __future__ import annotations

import hashlib
import hmac
import time

from .config import settings

WORKER_TOKEN_TTL_SEC = 300


def sign_worker_token(user_id: str, purpose: str = "poll") -> str:
    if not settings.worker_secret:
        raise RuntimeError("WORKER_SECRET is not set — cannot authenticate to the API server.")
    window = int(time.time()) // WORKER_TOKEN_TTL_SEC
    msg = f"{user_id}:{purpose}:{window}".encode()
    return hmac.new(settings.worker_secret.encode(), msg, hashlib.sha256).hexdigest()