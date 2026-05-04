from fastapi import FastAPI, Request, HTTPException
from datetime import datetime, timezone
import json
import os

app = FastAPI()

WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "change-this-secret")

ALLOWED_ACTIONS = {
    "LONG_ENTRY",
    "SHORT_ENTRY",
    "LONG_TRAIL_UPDATE",
    "SHORT_TRAIL_UPDATE",
    "LONG_EXIT",
    "SHORT_EXIT",
    "TEST_LONG_ENTRY",
    "TEST_SHORT_ENTRY",
    "TEST_LONG_TRAIL_UPDATE",
    "TEST_SHORT_TRAIL_UPDATE",
    "TEST_LONG_EXIT",
    "TEST_SHORT_EXIT",
}

EXPECTED_SYMBOL = "BTCUSDT_MEXC"
EXPECTED_TIMEFRAME = "15"


@app.get("/")
def health_check():
    return {
        "status": "ok",
        "service": "btc-v6t-webhook-receiver",
        "message": "Receiver is running"
    }


@app.post("/tv-webhook")
async def tradingview_webhook(request: Request):
    secret = request.query_params.get("secret")

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    action = payload.get("action")
    symbol = payload.get("symbol")
    timeframe = str(payload.get("timeframe"))

    accepted = True
    reason = "accepted"

    if symbol != EXPECTED_SYMBOL:
        accepted = False
        reason = f"invalid symbol: {symbol}"

    elif timeframe != EXPECTED_TIMEFRAME:
        accepted = False
        reason = f"invalid timeframe: {timeframe}"

    elif action not in ALLOWED_ACTIONS:
        accepted = False
        reason = f"invalid action: {action}"

    is_test = isinstance(action, str) and action.startswith("TEST_")

    event = {
        "received_at_utc": datetime.now(timezone.utc).isoformat(),
        "accepted": accepted,
        "reason": reason,
        "is_test": is_test,
        "payload": payload,
    }

    # For free Render testing: print logs to Render dashboard.
    # Do not rely on local file storage long-term on free Render.
    print(json.dumps(event))

    return {
        "status": "ok",
        "accepted": accepted,
        "reason": reason,
        "received_action": action,
        "is_test": is_test,
    }
