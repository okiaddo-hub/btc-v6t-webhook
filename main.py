from fastapi import FastAPI, Request, HTTPException
from datetime import datetime, timezone
import json
import os
import time
import hmac
import hashlib
import urllib.parse
import urllib.request
import urllib.error

app = FastAPI()

WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "change-this-secret")

# MEXC environment variables
MEXC_ACCESS_KEY = os.getenv("MEXC_ACCESS_KEY")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY")
LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"

# MEXC futures contract symbols usually use underscore format, e.g. BTC_USDT.
MEXC_CONTRACT_SYMBOL = os.getenv("MEXC_CONTRACT_SYMBOL", "BTC_USDT")
MEXC_CONTRACT_BASE_URL = "https://contract.mexc.com"

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


# =====================================================
# In-memory paper state
# NOTE:
# On free Render, this can reset if the service restarts/sleeps.
# Good for testing logic, not permanent recordkeeping.
# =====================================================

paper_state = {
    "position": "flat",       # flat / long / short
    "entry": None,
    "stop": None,
    "target": None,
    "qty": None,
    "last_action": None,
    "last_reason": "initial state",
    "updated_at_utc": None,
    "event_count": 0,
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def clean_action(action: str) -> str:
    """
    Converts TEST_LONG_ENTRY -> LONG_ENTRY.
    Real actions are unchanged.
    """
    if isinstance(action, str) and action.startswith("TEST_"):
        return action.replace("TEST_", "", 1)
    return action


# =====================================================
# MEXC read-only helpers
# =====================================================

def mexc_get_private(path: str, params: dict | None = None):
    """
    Sends signed GET request to MEXC Contract API.

    Signing rule for private GET:
    signature payload = accessKey + timestamp + sorted_query_string
    signature = HMAC_SHA256(secret, payload)
    """
    if not MEXC_ACCESS_KEY or not MEXC_SECRET_KEY:
        return {
            "ok": False,
            "error": "MEXC_ACCESS_KEY or MEXC_SECRET_KEY missing in Render environment variables",
        }

    params = params or {}

    # Remove None values so they do not participate in signature.
    clean_params = {
        k: v for k, v in params.items()
        if v is not None and v != ""
    }

    # MEXC docs say GET params are sorted and joined with &.
    query_string = urllib.parse.urlencode(sorted(clean_params.items()))

    request_time = str(int(time.time() * 1000))
    signature_payload = MEXC_ACCESS_KEY + request_time + query_string

    signature = hmac.new(
        MEXC_SECRET_KEY.encode("utf-8"),
        signature_payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    url = MEXC_CONTRACT_BASE_URL + path
    if query_string:
        url += "?" + query_string

    headers = {
        "ApiKey": MEXC_ACCESS_KEY,
        "Request-Time": request_time,
        "Signature": signature,
        "Content-Type": "application/json",
        "Recv-Window": "30000",
    }

    req = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return {
                "ok": True,
                "status_code": response.status,
                "data": json.loads(raw),
            }

    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw

        return {
            "ok": False,
            "status_code": e.code,
            "error": parsed,
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }


def get_mexc_open_positions(symbol: str | None = None):
    params = {}
    if symbol:
        params["symbol"] = symbol

    return mexc_get_private(
        "/api/v1/private/position/open_positions",
        params=params
    )


# =====================================================
# Paper state machine
# =====================================================

def process_paper_event(payload: dict, is_test: bool):
    """
    Simulates future execution logic without touching MEXC.

    TEST_ events are ignored by paper trader.
    Real events update paper state.
    """
    action = payload.get("action")
    base_action = clean_action(action)

    price = payload.get("price")
    stop = payload.get("stop")
    target = payload.get("target")
    qty = payload.get("qty")

    paper_state["event_count"] += 1

    # TEST events should never affect paper position.
    if is_test:
        paper_state["last_action"] = action
        paper_state["last_reason"] = "test event ignored by paper trader"
        paper_state["updated_at_utc"] = utc_now()
        return {
            "paper_accepted": False,
            "paper_reason": "test event ignored by paper trader",
            "paper_state": paper_state.copy(),
        }

    # -------------------------
    # ENTRY EVENTS
    # -------------------------

    if base_action == "LONG_ENTRY":
        if paper_state["position"] != "flat":
            paper_state["last_action"] = action
            paper_state["last_reason"] = f"rejected LONG_ENTRY because paper position is already {paper_state['position']}"
            paper_state["updated_at_utc"] = utc_now()
            return {
                "paper_accepted": False,
                "paper_reason": paper_state["last_reason"],
                "paper_state": paper_state.copy(),
            }

        paper_state.update({
            "position": "long",
            "entry": price,
            "stop": stop,
            "target": target,
            "qty": qty,
            "last_action": action,
            "last_reason": "paper long opened",
            "updated_at_utc": utc_now(),
        })

        return {
            "paper_accepted": True,
            "paper_reason": "paper long opened",
            "paper_state": paper_state.copy(),
        }

    if base_action == "SHORT_ENTRY":
        if paper_state["position"] != "flat":
            paper_state["last_action"] = action
            paper_state["last_reason"] = f"rejected SHORT_ENTRY because paper position is already {paper_state['position']}"
            paper_state["updated_at_utc"] = utc_now()
            return {
                "paper_accepted": False,
                "paper_reason": paper_state["last_reason"],
                "paper_state": paper_state.copy(),
            }

        paper_state.update({
            "position": "short",
            "entry": price,
            "stop": stop,
            "target": target,
            "qty": qty,
            "last_action": action,
            "last_reason": "paper short opened",
            "updated_at_utc": utc_now(),
        })

        return {
            "paper_accepted": True,
            "paper_reason": "paper short opened",
            "paper_state": paper_state.copy(),
        }

    # -------------------------
    # TRAIL UPDATE EVENTS
    # -------------------------

    if base_action == "LONG_TRAIL_UPDATE":
        if paper_state["position"] != "long":
            paper_state["last_action"] = action
            paper_state["last_reason"] = f"rejected LONG_TRAIL_UPDATE because paper position is {paper_state['position']}"
            paper_state["updated_at_utc"] = utc_now()
            return {
                "paper_accepted": False,
                "paper_reason": paper_state["last_reason"],
                "paper_state": paper_state.copy(),
            }

        old_stop = paper_state["stop"]

        # For long trades, stop should only move UP.
        if stop is None or old_stop is None or stop <= old_stop:
            paper_state["last_action"] = action
            paper_state["last_reason"] = f"rejected LONG_TRAIL_UPDATE because stop did not improve: old={old_stop}, new={stop}"
            paper_state["updated_at_utc"] = utc_now()
            return {
                "paper_accepted": False,
                "paper_reason": paper_state["last_reason"],
                "paper_state": paper_state.copy(),
            }

        paper_state.update({
            "stop": stop,
            "target": target,
            "last_action": action,
            "last_reason": "paper long stop updated",
            "updated_at_utc": utc_now(),
        })

        return {
            "paper_accepted": True,
            "paper_reason": "paper long stop updated",
            "paper_state": paper_state.copy(),
        }

    if base_action == "SHORT_TRAIL_UPDATE":
        if paper_state["position"] != "short":
            paper_state["last_action"] = action
            paper_state["last_reason"] = f"rejected SHORT_TRAIL_UPDATE because paper position is {paper_state['position']}"
            paper_state["updated_at_utc"] = utc_now()
            return {
                "paper_accepted": False,
                "paper_reason": paper_state["last_reason"],
                "paper_state": paper_state.copy(),
            }

        old_stop = paper_state["stop"]

        # For short trades, stop should only move DOWN.
        if stop is None or old_stop is None or stop >= old_stop:
            paper_state["last_action"] = action
            paper_state["last_reason"] = f"rejected SHORT_TRAIL_UPDATE because stop did not improve: old={old_stop}, new={stop}"
            paper_state["updated_at_utc"] = utc_now()
            return {
                "paper_accepted": False,
                "paper_reason": paper_state["last_reason"],
                "paper_state": paper_state.copy(),
            }

        paper_state.update({
            "stop": stop,
            "target": target,
            "last_action": action,
            "last_reason": "paper short stop updated",
            "updated_at_utc": utc_now(),
        })

        return {
            "paper_accepted": True,
            "paper_reason": "paper short stop updated",
            "paper_state": paper_state.copy(),
        }

    # -------------------------
    # EXIT EVENTS
    # -------------------------

    if base_action == "LONG_EXIT":
        if paper_state["position"] != "long":
            paper_state["last_action"] = action
            paper_state["last_reason"] = f"rejected LONG_EXIT because paper position is {paper_state['position']}"
            paper_state["updated_at_utc"] = utc_now()
            return {
                "paper_accepted": False,
                "paper_reason": paper_state["last_reason"],
                "paper_state": paper_state.copy(),
            }

        paper_state.update({
            "position": "flat",
            "entry": None,
            "stop": None,
            "target": None,
            "qty": None,
            "last_action": action,
            "last_reason": f"paper long closed at price={price}",
            "updated_at_utc": utc_now(),
        })

        return {
            "paper_accepted": True,
            "paper_reason": f"paper long closed at price={price}",
            "paper_state": paper_state.copy(),
        }

    if base_action == "SHORT_EXIT":
        if paper_state["position"] != "short":
            paper_state["last_action"] = action
            paper_state["last_reason"] = f"rejected SHORT_EXIT because paper position is {paper_state['position']}"
            paper_state["updated_at_utc"] = utc_now()
            return {
                "paper_accepted": False,
                "paper_reason": paper_state["last_reason"],
                "paper_state": paper_state.copy(),
            }

        paper_state.update({
            "position": "flat",
            "entry": None,
            "stop": None,
            "target": None,
            "qty": None,
            "last_action": action,
            "last_reason": f"paper short closed at price={price}",
            "updated_at_utc": utc_now(),
        })

        return {
            "paper_accepted": True,
            "paper_reason": f"paper short closed at price={price}",
            "paper_state": paper_state.copy(),
        }

    # Fallback
    paper_state["last_action"] = action
    paper_state["last_reason"] = f"no paper rule for action: {action}"
    paper_state["updated_at_utc"] = utc_now()

    return {
        "paper_accepted": False,
        "paper_reason": paper_state["last_reason"],
        "paper_state": paper_state.copy(),
    }


# =====================================================
# Routes
# =====================================================

@app.get("/")
def health_check():
    return {
        "status": "ok",
        "service": "btc-v6t-webhook-receiver",
        "message": "Receiver is running",
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "mexc_contract_symbol": MEXC_CONTRACT_SYMBOL,
    }


@app.get("/state")
def get_state():
    return {
        "status": "ok",
        "paper_state": paper_state,
        "live_trading_enabled": LIVE_TRADING_ENABLED,
    }


@app.get("/mexc-open-positions")
def mexc_open_positions(request: Request):
    """
    Read-only MEXC futures open-position check.

    Browser test:
    /mexc-open-positions?secret=...
    """
    secret = request.query_params.get("secret")

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    result = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)

    print(json.dumps({
        "event": "mexc_open_positions_check",
        "received_at_utc": utc_now(),
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "mexc_contract_symbol": MEXC_CONTRACT_SYMBOL,
        "result": result,
    }))

    return {
        "status": "ok",
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "mexc_contract_symbol": MEXC_CONTRACT_SYMBOL,
        "mexc_result": result,
    }


@app.post("/reset-state")
def reset_state(request: Request):
    """
    Resets paper state manually.

    Use carefully.
    POST /reset-state?secret=...
    """
    secret = request.query_params.get("secret")

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    paper_state.update({
        "position": "flat",
        "entry": None,
        "stop": None,
        "target": None,
        "qty": None,
        "last_action": "RESET",
        "last_reason": "paper state manually reset",
        "updated_at_utc": utc_now(),
        "event_count": 0,
    })

    print(json.dumps({
        "event": "manual_reset",
        "paper_state": paper_state,
    }))

    return {
        "status": "ok",
        "message": "paper state reset",
        "paper_state": paper_state,
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

    paper_result = {
        "paper_accepted": False,
        "paper_reason": "webhook validation failed",
        "paper_state": paper_state.copy(),
    }

    mexc_position_snapshot = None

    if accepted:
        paper_result = process_paper_event(payload, is_test)

        # Read-only MEXC snapshot for real events only.
        # This does not trade.
        if not is_test:
            mexc_position_snapshot = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)

    event = {
        "received_at_utc": utc_now(),
        "webhook_accepted": accepted,
        "webhook_reason": reason,
        "is_test": is_test,
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "payload": payload,
        "paper_result": paper_result,
        "mexc_position_snapshot": mexc_position_snapshot,
    }

    # Free Render testing: print logs to Render dashboard.
    print(json.dumps(event))

    return {
        "status": "ok",
        "webhook_accepted": accepted,
        "webhook_reason": reason,
        "received_action": action,
        "is_test": is_test,
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "paper_result": paper_result,
        "mexc_position_snapshot": mexc_position_snapshot,
    }
