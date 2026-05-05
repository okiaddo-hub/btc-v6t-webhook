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
import uuid

app = FastAPI()

WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "change-this-secret")

# =====================================================
# Environment variables
# =====================================================

MEXC_ACCESS_KEY = os.getenv("MEXC_ACCESS_KEY")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY")

# Main arming switch.
# If false, no live MEXC order can be placed.
LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"

# Separate switch for automatic TradingView webhook execution.
# Keep this false during manual testing.
AUTO_TV_EXECUTION_ENABLED = os.getenv("AUTO_TV_EXECUTION_ENABLED", "false").lower() == "true"

# MEXC futures settings
MEXC_CONTRACT_SYMBOL = os.getenv("MEXC_CONTRACT_SYMBOL", "BTC_USDT")
MEXC_CONTRACT_BASE_URL = os.getenv("MEXC_CONTRACT_BASE_URL", "https://api.mexc.com")

# Current documented MEXC Futures order endpoint:
# POST /api/v1/private/order/create
MEXC_ORDER_CREATE_PATH = os.getenv(
    "MEXC_ORDER_CREATE_PATH",
    "/api/v1/private/order/create"
)

MEXC_LEVERAGE = int(os.getenv("MEXC_LEVERAGE", "4"))
MEXC_OPEN_TYPE = int(os.getenv("MEXC_OPEN_TYPE", "1"))       # 1 isolated, 2 cross
MEXC_ORDER_TYPE = int(os.getenv("MEXC_ORDER_TYPE", "5"))     # 5 market order

# MEXC docs:
# positionMode:
# 1 = dual-side / hedge
# 2 = one-way
#
# Your previous entry-only order worked with the current setup,
# so this preserves your current default of 2.
MEXC_POSITION_MODE = int(os.getenv("MEXC_POSITION_MODE", "2"))

# TP/SL trigger price type:
# 1 = latest price
# 2 = fair price
# 3 = index price
MEXC_SL_PRICE_TYPE = int(os.getenv("MEXC_SL_PRICE_TYPE", "1"))
MEXC_TP_PRICE_TYPE = int(os.getenv("MEXC_TP_PRICE_TYPE", "1"))

# Trigger protection:
# 0 = disabled
# 1 = enabled
MEXC_PRICE_PROTECT = int(os.getenv("MEXC_PRICE_PROTECT", "0"))

# BTC_USDT contract detail from MEXC:
# contractSize = 0.0001 BTC
# volScale = 0
# volUnit = 1
# minVol = 1
MEXC_CONTRACT_SIZE = float(os.getenv("MEXC_CONTRACT_SIZE", "0.0001"))
MEXC_MIN_CONTRACT_VOL = int(os.getenv("MEXC_MIN_CONTRACT_VOL", "1"))

# Hard order-size guards.
# These are in BTC quantity from TradingView / URL input, not MEXC contract count.
MAX_ORDER_VOL = float(os.getenv("MAX_ORDER_VOL", "0.02"))
MIN_ORDER_VOL = float(os.getenv("MIN_ORDER_VOL", "0.0001"))

# Separate stricter cap for manual live test.
# Also in BTC quantity.
MAX_MANUAL_TEST_VOL = float(os.getenv("MAX_MANUAL_TEST_VOL", "0.001"))

MANUAL_LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_THIS_PLACES_A_LIVE_ORDER"

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
# =====================================================

paper_state = {
    "position": "flat",
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
    if isinstance(action, str) and action.startswith("TEST_"):
        return action.replace("TEST_", "", 1)
    return action


def to_float(value, field_name: str):
    try:
        return float(value)
    except Exception:
        raise ValueError(f"{field_name} must be numeric, got {value}")


def btc_qty_to_mexc_vol(qty_btc: float):
    """
    Converts BTC quantity into MEXC contract volume.

    For BTC_USDT:
    contractSize = 0.0001 BTC
    Therefore:
    0.001 BTC / 0.0001 = 10 contracts

    MEXC volScale = 0, so vol must be an integer.
    """
    raw_vol = qty_btc / MEXC_CONTRACT_SIZE
    mexc_vol = int(round(raw_vol))

    if mexc_vol < MEXC_MIN_CONTRACT_VOL:
        raise ValueError(
            f"Calculated MEXC vol {mexc_vol} is below minimum {MEXC_MIN_CONTRACT_VOL}. "
            f"qty_btc={qty_btc}, contract_size={MEXC_CONTRACT_SIZE}"
        )

    # Sanity check: avoid accepting a quantity that does not convert cleanly.
    reconstructed_qty = mexc_vol * MEXC_CONTRACT_SIZE
    conversion_error = abs(reconstructed_qty - qty_btc)

    if conversion_error > (MEXC_CONTRACT_SIZE / 10):
        raise ValueError(
            f"BTC qty does not convert cleanly to MEXC contract volume. "
            f"qty_btc={qty_btc}, mexc_vol={mexc_vol}, reconstructed_qty={reconstructed_qty}"
        )

    return mexc_vol


def has_open_mexc_position(mexc_result: dict):
    if not mexc_result.get("ok"):
        return False, f"MEXC position check failed: {mexc_result}"

    data_wrapper = mexc_result.get("data", {})
    if not isinstance(data_wrapper, dict):
        return False, f"Unexpected MEXC response format: {data_wrapper}"

    if not data_wrapper.get("success"):
        return False, f"MEXC returned success=false: {data_wrapper}"

    positions = data_wrapper.get("data", [])

    if positions is None:
        positions = []

    if not isinstance(positions, list):
        return False, f"Unexpected MEXC positions format: {positions}"

    open_positions = []

    for pos in positions:
        if not isinstance(pos, dict):
            continue

        hold_vol = pos.get("holdVol", pos.get("hold_vol", pos.get("volume", None)))
        state = pos.get("state", None)

        try:
            hold_vol_num = float(hold_vol) if hold_vol is not None else None
        except Exception:
            hold_vol_num = None

        if hold_vol_num is None:
            open_positions.append(pos)
        elif hold_vol_num != 0:
            open_positions.append(pos)
        elif state not in [None, 0, "0", "closed", "Closed"]:
            open_positions.append(pos)

    if open_positions:
        return True, f"MEXC already has open position(s): {open_positions}"

    return False, "MEXC shows no open position"


# =====================================================
# MEXC request helpers
# =====================================================

def mexc_sign(payload: str, request_time: str):
    signature_payload = MEXC_ACCESS_KEY + request_time + payload

    return hmac.new(
        MEXC_SECRET_KEY.encode("utf-8"),
        signature_payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def mexc_get_private(path: str, params=None):
    if not MEXC_ACCESS_KEY or not MEXC_SECRET_KEY:
        return {
            "ok": False,
            "error": "MEXC_ACCESS_KEY or MEXC_SECRET_KEY missing in VPS environment variables",
        }

    params = params or {}
    clean_params = {
        k: v for k, v in params.items()
        if v is not None and v != ""
    }

    query_string = urllib.parse.urlencode(sorted(clean_params.items()))

    request_time = str(int(time.time() * 1000))
    signature = mexc_sign(query_string, request_time)

    url = MEXC_CONTRACT_BASE_URL + path
    if query_string:
        url += "?" + query_string

    headers = {
        "ApiKey": MEXC_ACCESS_KEY,
        "Request-Time": request_time,
        "Signature": signature,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 V6T-VPS-Bot/1.0",
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


def mexc_post_private(path: str, body: dict):
    if not MEXC_ACCESS_KEY or not MEXC_SECRET_KEY:
        return {
            "ok": False,
            "error": "MEXC_ACCESS_KEY or MEXC_SECRET_KEY missing in VPS environment variables",
        }

    body_string = json.dumps(body, separators=(",", ":"))

    request_time = str(int(time.time() * 1000))
    signature = mexc_sign(body_string, request_time)

    url = MEXC_CONTRACT_BASE_URL + path

    headers = {
        "ApiKey": MEXC_ACCESS_KEY,
        "Request-Time": request_time,
        "Signature": signature,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 V6T-VPS-Bot/1.0",
        "Recv-Window": "30000",
    }

    req = urllib.request.Request(
        url,
        data=body_string.encode("utf-8"),
        headers=headers,
        method="POST"
    )

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


def get_mexc_open_positions(symbol=None):
    params = {}
    if symbol:
        params["symbol"] = symbol

    return mexc_get_private(
        "/api/v1/private/position/open_positions",
        params=params
    )


def get_mexc_open_stop_orders(symbol=None):
    """
    Inspect current unfinished TP/SL stop orders.

    This helps confirm whether attached SL/TP was actually created.
    """
    params = {
        "is_finished": 0,
        "page_num": 1,
        "page_size": 20,
    }

    if symbol:
        params["symbol"] = symbol

    return mexc_get_private(
        "/api/v1/private/stoporder/list/orders",
        params=params
    )


# =====================================================
# Order validation and order builder
# =====================================================

def validate_entry_payload(payload: dict):
    action = payload.get("action")
    base_action = clean_action(action)

    if base_action not in {"LONG_ENTRY", "SHORT_ENTRY"}:
        raise ValueError(f"Cannot validate non-entry action: {action}")

    price = to_float(payload.get("price"), "price")
    qty = to_float(payload.get("qty"), "qty")
    stop = to_float(payload.get("stop"), "stop")
    target = to_float(payload.get("target"), "target")

    if price <= 0:
        raise ValueError(f"price must be > 0, got {price}")

    if qty <= 0:
        raise ValueError(f"qty must be > 0, got {qty}")

    if qty < MIN_ORDER_VOL:
        raise ValueError(f"qty {qty} is below MIN_ORDER_VOL {MIN_ORDER_VOL}")

    if qty > MAX_ORDER_VOL:
        raise ValueError(f"qty {qty} exceeds MAX_ORDER_VOL {MAX_ORDER_VOL}")

    if stop <= 0:
        raise ValueError(f"stop must be > 0, got {stop}")

    if target <= 0:
        raise ValueError(f"target must be > 0, got {target}")

    if base_action == "LONG_ENTRY":
        if not stop < price:
            raise ValueError(f"LONG geometry invalid: stop {stop} must be below price {price}")
        if not target > price:
            raise ValueError(f"LONG geometry invalid: target {target} must be above price {price}")

    if base_action == "SHORT_ENTRY":
        if not stop > price:
            raise ValueError(f"SHORT geometry invalid: stop {stop} must be above price {price}")
        if not target < price:
            raise ValueError(f"SHORT geometry invalid: target {target} must be below price {price}")

    return {
        "price": price,
        "qty": qty,
        "stop": stop,
        "target": target,
        "base_action": base_action,
    }


def build_mexc_entry_order(payload: dict):
    """
    Builds a MEXC market entry order WITH attached SL/TP.

    Important MEXC format points:
    - Current documented order endpoint is /api/v1/private/order/create.
    - type=5 means market.
    - price is still sent as 0.
    - vol is contract count, not BTC amount.
    - stopLossPrice and takeProfitPrice are attached directly.
    - lossTrend/profitTrend are sent explicitly.
    """
    validated = validate_entry_payload(payload)

    qty_btc = validated["qty"]
    mexc_vol = btc_qty_to_mexc_vol(qty_btc)

    stop = validated["stop"]
    target = validated["target"]
    base_action = validated["base_action"]

    side = 1 if base_action == "LONG_ENTRY" else 3

    external_oid = "v6t" + str(uuid.uuid4()).replace("-", "")[:20]

    order_body = {
        "symbol": MEXC_CONTRACT_SYMBOL,
        "price": 0,
        "vol": mexc_vol,
        "leverage": MEXC_LEVERAGE,
        "side": side,
        "type": MEXC_ORDER_TYPE,
        "openType": MEXC_OPEN_TYPE,
        "externalOid": external_oid,
        "positionMode": MEXC_POSITION_MODE,

        # Attached TP/SL fields
        "stopLossPrice": stop,
        "takeProfitPrice": target,
        "lossTrend": MEXC_SL_PRICE_TYPE,
        "profitTrend": MEXC_TP_PRICE_TYPE,
        "priceProtect": MEXC_PRICE_PROTECT,
    }

    return {
        "order_body": order_body,
        "conversion": {
            "input_qty_btc": qty_btc,
            "mexc_contract_size": MEXC_CONTRACT_SIZE,
            "mexc_vol_contracts": mexc_vol,
        },
        "validation": {
            "passed": True,
            "min_order_vol_btc": MIN_ORDER_VOL,
            "max_order_vol_btc": MAX_ORDER_VOL,
            "geometry_checked": True,
            "attached_sl_tp": True,
            "lossTrend": MEXC_SL_PRICE_TYPE,
            "profitTrend": MEXC_TP_PRICE_TYPE,
            "priceProtect": MEXC_PRICE_PROTECT,
            "mexc_order_path": MEXC_ORDER_CREATE_PATH,
        },
        "warnings": [
            "This attempts attached SL/TP at entry using /api/v1/private/order/create.",
            "If MEXC still returns 5003, fallback is entry first, then /api/v1/private/stoporder/place.",
        ],
    }


def build_mexc_entry_only_order(payload: dict):
    """
    Builds a MEXC market entry order with NO attached SL/TP.

    This is for tiny manual plumbing tests only.
    You must manually close the position after confirming it opened.
    """
    validated = validate_entry_payload(payload)

    qty_btc = validated["qty"]
    mexc_vol = btc_qty_to_mexc_vol(qty_btc)

    base_action = validated["base_action"]

    side = 1 if base_action == "LONG_ENTRY" else 3

    external_oid = "v6t" + str(uuid.uuid4()).replace("-", "")[:20]

    order_body = {
        "symbol": MEXC_CONTRACT_SYMBOL,
        "price": 0,
        "vol": mexc_vol,
        "leverage": MEXC_LEVERAGE,
        "side": side,
        "type": MEXC_ORDER_TYPE,
        "openType": MEXC_OPEN_TYPE,
        "externalOid": external_oid,
        "positionMode": MEXC_POSITION_MODE,
    }

    return {
        "order_body": order_body,
        "conversion": {
            "input_qty_btc": qty_btc,
            "mexc_contract_size": MEXC_CONTRACT_SIZE,
            "mexc_vol_contracts": mexc_vol,
        },
        "validation": {
            "passed": True,
            "min_order_vol_btc": MIN_ORDER_VOL,
            "max_order_vol_btc": MAX_ORDER_VOL,
            "max_manual_test_vol_btc": MAX_MANUAL_TEST_VOL,
            "entry_only": True,
            "attached_sl_tp": False,
            "mexc_order_path": MEXC_ORDER_CREATE_PATH,
        },
        "warnings": [
            "ENTRY ONLY: no SL/TP will be attached.",
            "Use only for tiny manual test.",
            "Manually close the position immediately after confirming it opened.",
        ],
    }


def dry_run_only(order_body: dict):
    return {
        "live_order_sent": False,
        "reason": "dry run only - this endpoint never submits orders",
        "would_send_order": order_body,
    }


def place_live_order_only_if_armed(order_body: dict):
    if not LIVE_TRADING_ENABLED:
        return {
            "live_order_sent": False,
            "reason": "blocked - LIVE_TRADING_ENABLED=false",
            "would_send_order": order_body,
        }

    return {
        "live_order_sent": True,
        "reason": f"LIVE_TRADING_ENABLED=true - submitting order to MEXC path {MEXC_ORDER_CREATE_PATH}",
        "mexc_order_path": MEXC_ORDER_CREATE_PATH,
        "mexc_response": mexc_post_private(
            MEXC_ORDER_CREATE_PATH,
            order_body
        ),
    }


# =====================================================
# Entry guard
# =====================================================

def run_entry_guard(payload: dict, is_test: bool, mexc_position_snapshot=None):
    action = payload.get("action")
    base_action = clean_action(action)

    result = {
        "guard_checked": False,
        "guard_passed": False,
        "would_enter": False,
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "auto_tv_execution_enabled": AUTO_TV_EXECUTION_ENABLED,
        "reason": "not checked",
        "mexc_position_snapshot": mexc_position_snapshot,
    }

    if base_action not in {"LONG_ENTRY", "SHORT_ENTRY"}:
        result.update({
            "guard_checked": False,
            "reason": f"not an entry action: {action}",
        })
        return result

    result["guard_checked"] = True

    if is_test:
        result.update({
            "guard_passed": False,
            "would_enter": False,
            "reason": "test event rejected by entry guard",
        })
        return result

    try:
        validate_entry_payload(payload)
    except Exception as e:
        result.update({
            "guard_passed": False,
            "would_enter": False,
            "reason": f"entry payload validation failed: {str(e)}",
        })
        return result

    if paper_state["position"] != "flat":
        result.update({
            "guard_passed": False,
            "would_enter": False,
            "reason": f"paper state is not flat: {paper_state['position']}",
        })
        return result

    if mexc_position_snapshot is None:
        mexc_position_snapshot = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)
        result["mexc_position_snapshot"] = mexc_position_snapshot

    mexc_has_position, mexc_reason = has_open_mexc_position(mexc_position_snapshot)

    if not mexc_position_snapshot.get("ok"):
        result.update({
            "guard_passed": False,
            "would_enter": False,
            "reason": mexc_reason,
        })
        return result

    if mexc_has_position:
        result.update({
            "guard_passed": False,
            "would_enter": False,
            "reason": mexc_reason,
        })
        return result

    if not LIVE_TRADING_ENABLED:
        result.update({
            "guard_passed": True,
            "would_enter": True,
            "reason": "guard passed, but live trading disabled",
        })
        return result

    result.update({
        "guard_passed": True,
        "would_enter": True,
        "reason": "guard passed and live trading enabled",
    })
    return result


# =====================================================
# Paper state machine
# =====================================================

def process_paper_event(payload: dict, is_test: bool):
    action = payload.get("action")
    base_action = clean_action(action)

    price = payload.get("price")
    stop = payload.get("stop")
    target = payload.get("target")
    qty = payload.get("qty")

    paper_state["event_count"] += 1

    if is_test:
        paper_state["last_action"] = action
        paper_state["last_reason"] = "test event ignored by paper trader"
        paper_state["updated_at_utc"] = utc_now()
        return {
            "paper_accepted": False,
            "paper_reason": "test event ignored by paper trader",
            "paper_state": paper_state.copy(),
        }

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
        "auto_tv_execution_enabled": AUTO_TV_EXECUTION_ENABLED,
        "mexc_contract_symbol": MEXC_CONTRACT_SYMBOL,
        "mexc_base_url": MEXC_CONTRACT_BASE_URL,
        "mexc_order_create_path": MEXC_ORDER_CREATE_PATH,
        "mexc_contract_size": MEXC_CONTRACT_SIZE,
        "mexc_min_contract_vol": MEXC_MIN_CONTRACT_VOL,
        "mexc_leverage": MEXC_LEVERAGE,
        "mexc_open_type": MEXC_OPEN_TYPE,
        "mexc_order_type": MEXC_ORDER_TYPE,
        "mexc_position_mode": MEXC_POSITION_MODE,
        "mexc_sl_price_type": MEXC_SL_PRICE_TYPE,
        "mexc_tp_price_type": MEXC_TP_PRICE_TYPE,
        "mexc_price_protect": MEXC_PRICE_PROTECT,
        "min_order_vol_btc": MIN_ORDER_VOL,
        "max_order_vol_btc": MAX_ORDER_VOL,
        "max_manual_test_vol_btc": MAX_MANUAL_TEST_VOL,
    }


@app.get("/state")
def get_state():
    return {
        "status": "ok",
        "paper_state": paper_state,
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "auto_tv_execution_enabled": AUTO_TV_EXECUTION_ENABLED,
    }


@app.get("/mexc-open-positions")
def mexc_open_positions(request: Request):
    secret = request.query_params.get("secret")

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    result = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)

    print(json.dumps({
        "event": "mexc_open_positions_check",
        "received_at_utc": utc_now(),
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "mexc_contract_symbol": MEXC_CONTRACT_SYMBOL,
        "mexc_base_url": MEXC_CONTRACT_BASE_URL,
        "result": result,
    }))

    return {
        "status": "ok",
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "mexc_contract_symbol": MEXC_CONTRACT_SYMBOL,
        "mexc_base_url": MEXC_CONTRACT_BASE_URL,
        "mexc_result": result,
    }


@app.get("/mexc-open-stop-orders")
def mexc_open_stop_orders(request: Request):
    """
    Checks unfinished TP/SL stop orders.
    Useful immediately after /manual-live-test.
    """
    secret = request.query_params.get("secret")

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    result = get_mexc_open_stop_orders(MEXC_CONTRACT_SYMBOL)

    print(json.dumps({
        "event": "mexc_open_stop_orders_check",
        "received_at_utc": utc_now(),
        "mexc_contract_symbol": MEXC_CONTRACT_SYMBOL,
        "result": result,
    }))

    return {
        "status": "ok",
        "mexc_contract_symbol": MEXC_CONTRACT_SYMBOL,
        "mexc_result": result,
    }


@app.get("/entry-guard-test")
def entry_guard_test(request: Request):
    secret = request.query_params.get("secret")
    side = request.query_params.get("side", "long").lower()

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    if side not in {"long", "short"}:
        raise HTTPException(status_code=400, detail="side must be long or short")

    simulated_action = "LONG_ENTRY" if side == "long" else "SHORT_ENTRY"

    simulated_payload = {
        "symbol": EXPECTED_SYMBOL,
        "action": simulated_action,
        "side": side.upper(),
        "price": 78000,
        "stop": 77220 if side == "long" else 78780,
        "target": 79638 if side == "long" else 76362,
        "qty": 0.001,
        "time": None,
        "timeframe": EXPECTED_TIMEFRAME,
    }

    mexc_snapshot = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)
    entry_guard = run_entry_guard(
        simulated_payload,
        is_test=False,
        mexc_position_snapshot=mexc_snapshot
    )

    proposed_order = None
    dry_run_result = None

    if entry_guard["guard_passed"] and entry_guard["would_enter"]:
        try:
            proposed_order = build_mexc_entry_order(simulated_payload)
            dry_run_result = dry_run_only(proposed_order["order_body"])
        except Exception as e:
            dry_run_result = {
                "live_order_sent": False,
                "reason": f"failed to build proposed order: {str(e)}",
            }

    event = {
        "event": "entry_guard_test",
        "received_at_utc": utc_now(),
        "simulated_action": simulated_action,
        "paper_state": paper_state.copy(),
        "entry_guard": entry_guard,
        "proposed_order": proposed_order,
        "dry_run_result": dry_run_result,
    }

    print(json.dumps(event))

    return {
        "status": "ok",
        "simulated_action": simulated_action,
        "paper_state": paper_state.copy(),
        "entry_guard": entry_guard,
        "proposed_order": proposed_order,
        "dry_run_result": dry_run_result,
    }


@app.get("/dry-run-order")
def dry_run_order(request: Request):
    """
    Dry-run attached SL/TP order. Never submits to MEXC.

    Query params:
    - secret
    - side=long or short
    - qty=0.001
    - price=current reference price
    - stop=SL price
    - target=TP price
    """
    secret = request.query_params.get("secret")
    side = request.query_params.get("side", "long").lower()
    qty_param = request.query_params.get("qty")
    price_param = request.query_params.get("price")
    stop_param = request.query_params.get("stop")
    target_param = request.query_params.get("target")

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    if side not in {"long", "short"}:
        raise HTTPException(status_code=400, detail="side must be long or short")

    simulated_action = "LONG_ENTRY" if side == "long" else "SHORT_ENTRY"

    simulated_qty = 0.001 if qty_param is None else to_float(qty_param, "qty")
    simulated_price = 78000 if price_param is None else to_float(price_param, "price")

    if stop_param is None:
        simulated_stop = simulated_price * 0.99 if side == "long" else simulated_price * 1.01
    else:
        simulated_stop = to_float(stop_param, "stop")

    if target_param is None:
        simulated_target = simulated_price * 1.02 if side == "long" else simulated_price * 0.98
    else:
        simulated_target = to_float(target_param, "target")

    simulated_payload = {
        "symbol": EXPECTED_SYMBOL,
        "action": simulated_action,
        "side": side.upper(),
        "price": simulated_price,
        "stop": simulated_stop,
        "target": simulated_target,
        "qty": simulated_qty,
        "time": None,
        "timeframe": EXPECTED_TIMEFRAME,
    }

    proposed_order = None
    dry_run_result = None

    try:
        proposed_order = build_mexc_entry_order(simulated_payload)
        dry_run_result = dry_run_only(proposed_order["order_body"])
    except Exception as e:
        dry_run_result = {
            "live_order_sent": False,
            "reason": f"failed to build proposed order: {str(e)}",
        }

    return {
        "status": "ok",
        "simulated_payload": simulated_payload,
        "proposed_order": proposed_order,
        "dry_run_result": dry_run_result,
    }


@app.get("/manual-entry-only-test")
def manual_entry_only_test(request: Request):
    """
    Controlled one-off live market entry test with NO attached SL/TP.

    Required query params:
    - secret
    - side=long or short
    - qty
    - price
    - stop
    - target
    - confirm=I_UNDERSTAND_THIS_PLACES_A_LIVE_ORDER
    """
    secret = request.query_params.get("secret")
    side = request.query_params.get("side", "").lower()
    confirm = request.query_params.get("confirm")

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    if confirm != MANUAL_LIVE_CONFIRM_PHRASE:
        raise HTTPException(
            status_code=400,
            detail="Missing or incorrect confirmation phrase. This endpoint can place a live entry-only order."
        )

    if not LIVE_TRADING_ENABLED:
        return {
            "status": "blocked",
            "reason": "LIVE_TRADING_ENABLED=false",
            "live_order_sent": False,
        }

    if side not in {"long", "short"}:
        raise HTTPException(status_code=400, detail="side must be long or short")

    qty = to_float(request.query_params.get("qty"), "qty")
    price = to_float(request.query_params.get("price"), "price")
    stop = to_float(request.query_params.get("stop"), "stop")
    target = to_float(request.query_params.get("target"), "target")

    if qty > MAX_MANUAL_TEST_VOL:
        return {
            "status": "blocked",
            "reason": f"qty {qty} exceeds MAX_MANUAL_TEST_VOL {MAX_MANUAL_TEST_VOL}",
            "live_order_sent": False,
        }

    simulated_action = "LONG_ENTRY" if side == "long" else "SHORT_ENTRY"

    payload = {
        "symbol": EXPECTED_SYMBOL,
        "action": simulated_action,
        "side": side.upper(),
        "price": price,
        "stop": stop,
        "target": target,
        "qty": qty,
        "time": None,
        "timeframe": EXPECTED_TIMEFRAME,
    }

    mexc_snapshot = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)
    entry_guard = run_entry_guard(
        payload,
        is_test=False,
        mexc_position_snapshot=mexc_snapshot
    )

    if not entry_guard["guard_passed"] or not entry_guard["would_enter"]:
        return {
            "status": "blocked",
            "reason": "entry guard blocked manual entry-only test",
            "entry_guard": entry_guard,
            "live_order_sent": False,
        }

    try:
        proposed_order = build_mexc_entry_only_order(payload)
    except Exception as e:
        return {
            "status": "blocked",
            "reason": f"failed to build entry-only order: {str(e)}",
            "live_order_sent": False,
        }

    live_order_result = place_live_order_only_if_armed(proposed_order["order_body"])

    event = {
        "event": "manual_entry_only_test",
        "received_at_utc": utc_now(),
        "payload": payload,
        "entry_guard": entry_guard,
        "proposed_order": proposed_order,
        "live_order_result": live_order_result,
    }

    print(json.dumps(event))

    return {
        "status": "ok",
        "payload": payload,
        "entry_guard": entry_guard,
        "proposed_order": proposed_order,
        "live_order_result": live_order_result,
        "warning": "ENTRY ONLY: no SL/TP attached. Manually close immediately."
    }


@app.get("/manual-live-test")
def manual_live_test(request: Request):
    """
    Controlled one-off live order test WITH attached SL/TP.

    Required query params:
    - secret
    - side=long or short
    - qty
    - price
    - stop
    - target
    - confirm=I_UNDERSTAND_THIS_PLACES_A_LIVE_ORDER
    """
    secret = request.query_params.get("secret")
    side = request.query_params.get("side", "").lower()
    confirm = request.query_params.get("confirm")

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    if confirm != MANUAL_LIVE_CONFIRM_PHRASE:
        raise HTTPException(
            status_code=400,
            detail="Missing or incorrect confirmation phrase. This endpoint can place a live order."
        )

    if not LIVE_TRADING_ENABLED:
        return {
            "status": "blocked",
            "reason": "LIVE_TRADING_ENABLED=false",
            "live_order_sent": False,
        }

    if side not in {"long", "short"}:
        raise HTTPException(status_code=400, detail="side must be long or short")

    qty = to_float(request.query_params.get("qty"), "qty")
    price = to_float(request.query_params.get("price"), "price")
    stop = to_float(request.query_params.get("stop"), "stop")
    target = to_float(request.query_params.get("target"), "target")

    if qty > MAX_MANUAL_TEST_VOL:
        return {
            "status": "blocked",
            "reason": f"qty {qty} exceeds MAX_MANUAL_TEST_VOL {MAX_MANUAL_TEST_VOL}",
            "live_order_sent": False,
        }

    simulated_action = "LONG_ENTRY" if side == "long" else "SHORT_ENTRY"

    payload = {
        "symbol": EXPECTED_SYMBOL,
        "action": simulated_action,
        "side": side.upper(),
        "price": price,
        "stop": stop,
        "target": target,
        "qty": qty,
        "time": None,
        "timeframe": EXPECTED_TIMEFRAME,
    }

    mexc_snapshot = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)
    entry_guard = run_entry_guard(
        payload,
        is_test=False,
        mexc_position_snapshot=mexc_snapshot
    )

    if not entry_guard["guard_passed"] or not entry_guard["would_enter"]:
        return {
            "status": "blocked",
            "reason": "entry guard blocked manual live test",
            "entry_guard": entry_guard,
            "live_order_sent": False,
        }

    try:
        proposed_order = build_mexc_entry_order(payload)
    except Exception as e:
        return {
            "status": "blocked",
            "reason": f"failed to build proposed order: {str(e)}",
            "live_order_sent": False,
        }

    live_order_result = place_live_order_only_if_armed(proposed_order["order_body"])

    event = {
        "event": "manual_live_test",
        "received_at_utc": utc_now(),
        "payload": payload,
        "entry_guard": entry_guard,
        "proposed_order": proposed_order,
        "live_order_result": live_order_result,
    }

    print(json.dumps(event))

    return {
        "status": "ok",
        "payload": payload,
        "entry_guard": entry_guard,
        "proposed_order": proposed_order,
        "live_order_result": live_order_result,
        "next_check": "If success=true, immediately check /mexc-open-positions and /mexc-open-stop-orders.",
    }


@app.post("/reset-state")
def reset_state(request: Request):
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
    entry_guard = {
        "guard_checked": False,
        "guard_passed": False,
        "would_enter": False,
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "auto_tv_execution_enabled": AUTO_TV_EXECUTION_ENABLED,
        "reason": "webhook validation failed",
        "mexc_position_snapshot": None,
    }

    proposed_order = None
    tv_execution_result = None

    if accepted:
        base_action = clean_action(action)

        if base_action in {"LONG_ENTRY", "SHORT_ENTRY"}:
            mexc_position_snapshot = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)

            entry_guard = run_entry_guard(
                payload,
                is_test=is_test,
                mexc_position_snapshot=mexc_position_snapshot
            )

            if entry_guard["guard_passed"] and entry_guard["would_enter"]:
                try:
                    proposed_order = build_mexc_entry_order(payload)

                    if LIVE_TRADING_ENABLED and AUTO_TV_EXECUTION_ENABLED:
                        tv_execution_result = place_live_order_only_if_armed(proposed_order["order_body"])
                    else:
                        tv_execution_result = {
                            "live_order_sent": False,
                            "reason": "TradingView auto execution disabled. No live order submitted.",
                            "live_trading_enabled": LIVE_TRADING_ENABLED,
                            "auto_tv_execution_enabled": AUTO_TV_EXECUTION_ENABLED,
                            "would_send_order": proposed_order["order_body"],
                        }

                except Exception as e:
                    tv_execution_result = {
                        "live_order_sent": False,
                        "reason": f"failed to build proposed order: {str(e)}",
                    }

        paper_result = process_paper_event(payload, is_test)

    event = {
        "received_at_utc": utc_now(),
        "webhook_accepted": accepted,
        "webhook_reason": reason,
        "is_test": is_test,
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "auto_tv_execution_enabled": AUTO_TV_EXECUTION_ENABLED,
        "payload": payload,
        "entry_guard": entry_guard,
        "proposed_order": proposed_order,
        "tv_execution_result": tv_execution_result,
        "paper_result": paper_result,
        "mexc_position_snapshot": mexc_position_snapshot,
    }

    print(json.dumps(event))

    return {
        "status": "ok",
        "webhook_accepted": accepted,
        "webhook_reason": reason,
        "received_action": action,
        "is_test": is_test,
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "auto_tv_execution_enabled": AUTO_TV_EXECUTION_ENABLED,
        "entry_guard": entry_guard,
        "proposed_order": proposed_order,
        "tv_execution_result": tv_execution_result,
        "paper_result": paper_result,
        "mexc_position_snapshot": mexc_position_snapshot,
    }
