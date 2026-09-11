from __future__ import annotations

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from ..config import get_settings
from ..db import UserRepo
from ..logging_conf import get_logger
from ..redis_client import get_redis
from ..schemas import CheckoutRequest
from ..security import require_user

router = APIRouter(prefix="/api/v1", tags=["billing"])
log = get_logger("billing")
settings = get_settings()
stripe.api_key = settings.stripe_secret_key

# Renamed from v2's "Lifetime" framing while billed monthly (chargeback/FTC risk) —
# kept the honest naming from the v2 patch.
PRICING_TIERS = {
    "pro": {"name": "ViralClip AI — Pro (Monthly)", "amount": 2900, "mode": "subscription"},
    "full_version": {"name": "ViralClip AI — Full Version (Monthly)", "amount": 4900, "mode": "subscription"},
}


@router.post("/create-checkout-session")
async def create_checkout_session(body: CheckoutRequest, request: Request, user_id: str = Depends(require_user)):
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=500, detail="Billing is not configured on this server.")
    domain = str(request.base_url).rstrip("/")
    selected = PRICING_TIERS[body.tier]
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        client_reference_id=user_id,
        metadata={"tier": body.tier, "user_id": user_id},
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": selected["name"], "description": "Viral AI Short generation, background rendering, and YouTube auto-posting."},
                "unit_amount": selected["amount"],
                "recurring": {"interval": "month"},
            },
            "quantity": 1,
        }],
        mode="subscription",
        success_url=f"{domain}/?payment=success",
        cancel_url=f"{domain}/?payment=cancel",
    )
    return {"checkout_url": session.url}


@router.post("/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, settings.stripe_webhook_secret)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # IDEMPOTENCY: Stripe redelivers webhooks (network blips, non-2xx
    # responses, manual retries from the dashboard) and v2 applied every
    # delivery unconditionally. A redelivered `checkout.session.completed`
    # is harmless to reapply, but a redelivered subscription-lapse event
    # arriving *after* the user re-subscribed would wrongly downgrade them
    # back to free_tier. Dedup on Stripe's own event id.
    r = get_redis()
    if r is not None:
        first_time = await r.set(f"stripe:evt:{event['id']}", "1", nx=True, ex=86400)
        if not first_time:
            return {"status": "duplicate_ignored"}

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("client_reference_id")
        tier = (session.get("metadata") or {}).get("tier", "pro")
        if user_id:
            UserRepo.update(user_id, {"license": tier})
    elif event["type"] in ("customer.subscription.deleted", "customer.subscription.updated"):
        sub = event["data"]["object"]
        status = sub.get("status")
        user_id = (sub.get("metadata") or {}).get("user_id")
        if user_id and status in ("canceled", "unpaid", "incomplete_expired"):
            UserRepo.update(user_id, {"license": "free_tier"})

    return {"status": "success"}