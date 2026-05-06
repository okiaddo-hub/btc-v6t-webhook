
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

LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
AUTO_TV_EXECUTION_ENABLED = os.getenv("AUTO_TV_EXECUTION_ENABLED", "false").lower() == "true"

MEXC_CONTRACT_SYMBOL = os.getenv("MEXC_CONTRACT_SYMBOL", "BTC_USDT")
MEXC_CONTRACT_BASE_URL = os.getenv("MEXC_CONTRACT_BASE_URL", "https://api.mexc.com")

MEXC_ORDER_CREATE_PATH = os.getenv("MEXC_ORDER_CREATE_PATH", "/api/v1/private/order/create")
MEXC_STOP_ORDER_PLACE_PATH = os.getenv("MEXC_STOP_ORDER_PLACE_PATH", "/api/v1/private/stoporder/place")
MEXC_STOP_ORDER_CHANGE_PLAN_PRICE_PATH = os.getenv(
    "MEXC_STOP_ORDER_CHANGE_PLAN_PRICE_PATH",
    "/api/v1/private/stoporder/change_plan_price"
)

MEXC_LEVERAGE = int(os.getenv("MEXC_LEVERAGE", "4"))
MEXC_OPEN_TYPE = int(os.getenv("MEXC_OPEN_TYPE", "1"))       # 1 isolated, 2 cross
MEXC_ORDER_TYPE = int(os.getenv("MEXC_ORDER_TYPE", "5"))     # 5 market
MEXC_POSITION_MODE = int(os.getenv("MEXC_POSITION_MODE", "2"))  # 2 one-way, 1 dual-side

MEXC_SL_PRICE_TYPE = int(os.getenv("MEXC_SL_PRICE_TYPE", "1"))  # 1 latest
MEXC_TP_PRICE_TYPE = int(os.getenv("MEXC_TP_PRICE_TYPE", "1"))  # 1 latest
MEXC_PRICE_PROTECT = int(os.getenv("MEXC_PRICE_PROTECT", "0"))

MEXC_CONTRACT_SIZE = float(os.getenv("MEXC_CONTRACT_SIZE", "0.0001"))
MEXC_MIN_CONTRACT_VOL = int(os.getenv("MEXC_MIN_CONTRACT_VOL", "1"))

MAX_ORDER_VOL = float(os.getenv("MAX_ORDER_VOL", "0.02"))          # BTC qty
MIN_ORDER_VOL = float(os.getenv("MIN_ORDER_VOL", "0.0001"))        # BTC qty
MAX_MANUAL_TEST_VOL = float(os.getenv("MAX_MANUAL_TEST_VOL", "0.001"))

ENTRY_SETTLE_WAIT_SECONDS = float(os.getenv("ENTRY_SETTLE_WAIT_SECONDS", "1.0"))

STATE_FILE_PATH = os.getenv(
    "LIVE_TRADE_STATE_FILE",
    "/root/btc-v6t-webhook/live_trade_state.json"
)

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


# =====================================================
# General helpers
# =====================================================

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
    raw_vol = qty_btc / MEXC_CONTRACT_SIZE
    mexc_vol = int(round(raw_vol))

    if mexc_vol < MEXC_MIN_CONTRACT_VOL:
        raise ValueError(
            f"Calculated MEXC vol {mexc_vol} is below minimum {MEXC_MIN_CONTRACT_VOL}. "
            f"qty_btc={qty_btc}, contract_size={MEXC_CONTRACT_SIZE}"
        )

    reconstructed_qty = mexc_vol * MEXC_CONTRACT_SIZE
    conversion_error = abs(reconstructed_qty - qty_btc)

    if conversion_error > (MEXC_CONTRACT_SIZE / 10):
        raise ValueError(
            f"BTC qty does not convert cleanly to MEXC contract volume. "
            f"qty_btc={qty_btc}, mexc_vol={mexc_vol}, reconstructed_qty={reconstructed_qty}"
        )

    return mexc_vol


def short_external_oid():
    return "v6t" + str(uuid.uuid4()).replace("-", "")[:20]


# =====================================================
# Persistent live state helpers
# =====================================================

def load_live_state():
    if not os.path.exists(STATE_FILE_PATH):
        return {
            "status": "EMPTY",
            "reason": "state file does not exist",
            "updated_at_utc": utc_now(),
        }

    try:
        with open(STATE_FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {
            "status": "ERROR",
            "reason": f"failed to read state file: {str(e)}",
            "updated_at_utc": utc_now(),
        }


def save_live_state(state: dict):
    state["updated_at_utc"] = utc_now()

    tmp_path = STATE_FILE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)

    os.replace(tmp_path, STATE_FILE_PATH)
    return state


def clear_live_state(reason: str):
    return save_live_state({
        "status": "EMPTY",
        "reason": reason,
    })


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
    clean_params = {k: v for k, v in params.items() if v is not None and v != ""}
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
            return {"ok": True, "status_code": response.status, "data": json.loads(raw)}

    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw

        return {"ok": False, "status_code": e.code, "error": parsed}

    except Exception as e:
        return {"ok": False, "error": str(e)}


def mexc_post_private(path: str, body):
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
            return {"ok": True, "status_code": response.status, "data": json.loads(raw)}

    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw

        return {"ok": False, "status_code": e.code, "error": parsed}

    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_mexc_open_positions(symbol=None):
    params = {}
    if symbol:
        params["symbol"] = symbol

    return mexc_get_private("/api/v1/private/position/open_positions", params=params)


def get_mexc_open_stop_orders(symbol=None, position_id=None):
    params = {
        "is_finished": 0,
        "page_num": 1,
        "page_size": 20,
    }

    if symbol:
        params["symbol"] = symbol

    if position_id:
        params["positionId"] = position_id

    return mexc_get_private("/api/v1/private/stoporder/list/orders", params=params)


# =====================================================
# MEXC response helpers
# =====================================================

def extract_positions(mexc_result: dict):
    if not mexc_result.get("ok"):
        return []

    data_wrapper = mexc_result.get("data", {})
    if not isinstance(data_wrapper, dict):
        return []

    positions = data_wrapper.get("data", [])

    if positions is None:
        return []

    if isinstance(positions, dict):
        if isinstance(positions.get("resultList"), list):
            return positions["resultList"]
        return [positions]

    if isinstance(positions, list):
        return positions

    return []


def extract_position_id(position: dict):
    for key in ["positionId", "position_id", "id"]:
        value = position.get(key)
        if value not in [None, "", 0, "0"]:
            return value
    return None


def extract_hold_vol(position: dict):
    for key in ["holdVol", "hold_vol", "volume", "vol"]:
        value = position.get(key)
        if value is not None:
            try:
                return float(value)
            except Exception:
                return None
    return None


def position_is_open(position: dict):
    hold_vol = extract_hold_vol(position)
    state = position.get("state", None)

    if hold_vol is not None and hold_vol > 0:
        return True

    if hold_vol is None and state not in [None, 0, "0", 3, "3", "closed", "Closed"]:
        return True

    return False


def find_open_position(mexc_result: dict, symbol: str, base_action: str = None):
    positions = extract_positions(mexc_result)

    expected_position_type = None
    if base_action == "LONG_ENTRY":
        expected_position_type = 1
    elif base_action == "SHORT_ENTRY":
        expected_position_type = 2

    candidates = []

    for pos in positions:
        if not isinstance(pos, dict):
            continue

        if pos.get("symbol") != symbol:
            continue

        if not position_is_open(pos):
            continue

        if expected_position_type is not None:
            pos_type = pos.get("positionType", pos.get("position_type"))
            try:
                pos_type = int(pos_type)
            except Exception:
                pos_type = None

            if pos_type is not None and pos_type != expected_position_type:
                continue

        candidates.append(pos)

    if len(candidates) == 1:
        return candidates[0], None

    if len(candidates) == 0:
        return None, "No matching open position found"

    return None, f"Multiple matching open positions found: {candidates}"


def has_open_mexc_position(mexc_result: dict):
    if not mexc_result.get("ok"):
        return False, f"MEXC position check failed: {mexc_result}"

    data_wrapper = mexc_result.get("data", {})
    if not isinstance(data_wrapper, dict):
        return False, f"Unexpected MEXC response format: {data_wrapper}"

    if not data_wrapper.get("success"):
        return False, f"MEXC returned success=false: {data_wrapper}"

    open_positions = [p for p in extract_positions(mexc_result) if isinstance(p, dict) and position_is_open(p)]

    if open_positions:
        return True, f"MEXC already has open position(s): {open_positions}"

    return False, "MEXC shows no open position"


def extract_stop_orders(mexc_result: dict):
    if not mexc_result.get("ok"):
        return []

    data_wrapper = mexc_result.get("data", {})
    if not isinstance(data_wrapper, dict):
        return []

    data = data_wrapper.get("data", [])

    if isinstance(data, dict):
        if isinstance(data.get("resultList"), list):
            return data["resultList"]
        return [data]

    if isinstance(data, list):
        return data

    return []


def stop_orders_for_position(mexc_result: dict, position_id):
    orders = extract_stop_orders(mexc_result)
    position_id_str = str(position_id)

    matched = []
    for order in orders:
        if not isinstance(order, dict):
            continue

        if str(order.get("positionId")) == position_id_str:
            matched.append(order)

    return matched


# =====================================================
# Order validation and builders
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


def build_mexc_entry_only_order(payload: dict):
    validated = validate_entry_payload(payload)

    qty_btc = validated["qty"]
    mexc_vol = btc_qty_to_mexc_vol(qty_btc)
    base_action = validated["base_action"]
    side = 1 if base_action == "LONG_ENTRY" else 3

    order_body = {
        "symbol": MEXC_CONTRACT_SYMBOL,
        "price": 0,
        "vol": mexc_vol,
        "leverage": MEXC_LEVERAGE,
        "side": side,
        "type": MEXC_ORDER_TYPE,
        "openType": MEXC_OPEN_TYPE,
        "externalOid": short_external_oid(),
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
            "max_manual_test_vol_btc": MAX_MANUAL_TEST_VOL,
            "entry_only": True,
            "attached_sl_tp": False,
            "mexc_order_path": MEXC_ORDER_CREATE_PATH,
        },
        "warnings": [
            "ENTRY ONLY: no SL/TP attached at entry.",
            "This is intended to be followed immediately by position-level TP/SL.",
        ],
    }


def build_mexc_attached_sltp_entry_order(payload: dict):
    """
    Keeps the previous experimental attached-SLTP builder available for dry-runs.
    Do not use for live testing unless deliberately retesting MEXC 5003 behavior.
    """
    proposed = build_mexc_entry_only_order(payload)
    validated = validate_entry_payload(payload)

    proposed["order_body"]["stopLossPrice"] = validated["stop"]
    proposed["order_body"]["takeProfitPrice"] = validated["target"]
    proposed["order_body"]["lossTrend"] = MEXC_SL_PRICE_TYPE
    proposed["order_body"]["profitTrend"] = MEXC_TP_PRICE_TYPE
    proposed["order_body"]["priceProtect"] = MEXC_PRICE_PROTECT
    proposed["validation"]["attached_sl_tp"] = True
    proposed["warnings"] = [
        "This attempts attached SL/TP at entry and may return MEXC 5003.",
        "Preferred workflow is entry-only, then stoporder/place by positionId.",
    ]
    return proposed


def build_position_sltp_order(position_id, mexc_vol: int, stop: float, target: float):
    """
    Builds MEXC TP/SL order by existing position.

    Uses market TP/SL:
    takeProfitType = 0
    stopLossType = 0

    Uses SAME quantity for TP and SL:
    profitLossVolType = SAME
    """
    body = {
        "positionId": int(position_id),
        "vol": mexc_vol,
        "lossTrend": MEXC_SL_PRICE_TYPE,
        "profitTrend": MEXC_TP_PRICE_TYPE,
        "stopLossPrice": stop,
        "takeProfitPrice": target,
        "priceProtect": MEXC_PRICE_PROTECT,
        "profitLossVolType": "SAME",
        "volType": 1,
        "takeProfitReverse": 2,
        "stopLossReverse": 2,
        "takeProfitType": 0,
        "stopLossType": 0,
    }

    return body


def get_stop_plan_order_id_from_orders(stop_orders):
    """
    MEXC's /stoporder/change_plan_price endpoint uses stopPlanOrderId.
    In /stoporder/list/orders, that value appears as the stop order object's "id".
    """
    if not isinstance(stop_orders, list):
        return None

    for order in stop_orders:
        if not isinstance(order, dict):
            continue
        value = order.get("id", order.get("stopPlanOrderId", order.get("stop_plan_order_id")))
        if value not in [None, "", 0, "0"]:
            return value

    return None


def build_change_plan_price_order(stop_plan_order_id, stop: float = None, target: float = None):
    """
    Builds body for:
    POST /api/v1/private/stoporder/change_plan_price

    Required:
    - stopPlanOrderId
    - at least one of stopLossPrice / takeProfitPrice must be > 0

    We send both prices during trail updates so TP remains explicitly preserved.
    """
    if stop_plan_order_id in [None, "", 0, "0"]:
        raise ValueError("stopPlanOrderId is required for change_plan_price")

    if stop is None and target is None:
        raise ValueError("At least one of stopLossPrice or takeProfitPrice is required")

    body = {
        "stopPlanOrderId": int(stop_plan_order_id),
        "lossTrend": MEXC_SL_PRICE_TYPE,
        "profitTrend": MEXC_TP_PRICE_TYPE,
        "stopLossReverse": 2,
        "takeProfitReverse": 2,
    }

    if stop is not None:
        if float(stop) <= 0:
            raise ValueError("stopLossPrice must be > 0")
        body["stopLossPrice"] = float(stop)

    if target is not None:
        if float(target) <= 0:
            raise ValueError("takeProfitPrice must be > 0")
        body["takeProfitPrice"] = float(target)

    return body


def validate_trail_update_payload(payload: dict):
    action = payload.get("action")
    base_action = clean_action(action)

    if base_action not in {"LONG_TRAIL_UPDATE", "SHORT_TRAIL_UPDATE"}:
        raise ValueError(f"Cannot validate non-trail action: {action}")

    price = to_float(payload.get("price"), "price")
    stop = to_float(payload.get("stop"), "stop")

    target_raw = payload.get("target")
    target = None if target_raw in [None, "", "null"] else to_float(target_raw, "target")

    qty_raw = payload.get("qty")
    qty = None if qty_raw in [None, "", "null"] else to_float(qty_raw, "qty")

    if price <= 0:
        raise ValueError(f"price must be > 0, got {price}")
    if stop <= 0:
        raise ValueError(f"stop must be > 0, got {stop}")
    if target is not None and target <= 0:
        raise ValueError(f"target must be > 0, got {target}")
    if qty is not None and qty > MAX_ORDER_VOL:
        raise ValueError(f"qty {qty} exceeds MAX_ORDER_VOL {MAX_ORDER_VOL}")

    if base_action == "LONG_TRAIL_UPDATE":
        if not stop < price:
            raise ValueError(f"LONG trail geometry invalid: stop {stop} must be below price {price}")
        if target is not None and not target > price:
            raise ValueError(f"LONG trail geometry invalid: target {target} must be above price {price}")

    if base_action == "SHORT_TRAIL_UPDATE":
        if not stop > price:
            raise ValueError(f"SHORT trail geometry invalid: stop {stop} must be above price {price}")
        if target is not None and not target < price:
            raise ValueError(f"SHORT trail geometry invalid: target {target} must be below price {price}")

    return {
        "price": price,
        "stop": stop,
        "target": target,
        "qty": qty,
        "base_action": base_action,
    }


# =====================================================
# Live order execution helpers
# =====================================================

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
        "reason": f"LIVE_TRADING_ENABLED=true - submitting entry order to MEXC path {MEXC_ORDER_CREATE_PATH}",
        "mexc_order_path": MEXC_ORDER_CREATE_PATH,
        "mexc_response": mexc_post_private(MEXC_ORDER_CREATE_PATH, order_body),
    }


def place_position_sltp_only_if_armed(sltp_body: dict):
    if not LIVE_TRADING_ENABLED:
        return {
            "sltp_order_sent": False,
            "reason": "blocked - LIVE_TRADING_ENABLED=false",
            "would_send_sltp": sltp_body,
        }

    return {
        "sltp_order_sent": True,
        "reason": f"LIVE_TRADING_ENABLED=true - submitting TP/SL to MEXC path {MEXC_STOP_ORDER_PLACE_PATH}",
        "mexc_stop_order_path": MEXC_STOP_ORDER_PLACE_PATH,
        "mexc_response": mexc_post_private(MEXC_STOP_ORDER_PLACE_PATH, sltp_body),
    }


def change_plan_price_only_if_armed(change_body: dict):
    if not LIVE_TRADING_ENABLED:
        return {
            "change_sent": False,
            "reason": "blocked - LIVE_TRADING_ENABLED=false",
            "would_send_change": change_body,
        }

    return {
        "change_sent": True,
        "reason": f"LIVE_TRADING_ENABLED=true - modifying TP/SL via {MEXC_STOP_ORDER_CHANGE_PLAN_PRICE_PATH}",
        "mexc_stop_order_change_plan_price_path": MEXC_STOP_ORDER_CHANGE_PLAN_PRICE_PATH,
        "exit_confirm_phrase_required": "I_UNDERSTAND_THIS_CLOSES_LIVE_POSITION",
        "mexc_response": mexc_post_private(MEXC_STOP_ORDER_CHANGE_PLAN_PRICE_PATH, change_body),
    }


def mexc_success(result: dict):
    if not result.get("ok"):
        return False

    data = result.get("data")
    if not isinstance(data, dict):
        return False

    return data.get("success") is True and data.get("code") == 0


def entry_then_sltp_workflow(payload: dict):
    """
    Manual-only workflow:
    1. entry-only market order
    2. wait
    3. read open position
    4. extract positionId
    5. place position-level TP/SL
    6. verify stop order list
    7. save state
    """
    validated = validate_entry_payload(payload)
    base_action = validated["base_action"]
    qty_btc = validated["qty"]
    stop = validated["stop"]
    target = validated["target"]
    mexc_vol = btc_qty_to_mexc_vol(qty_btc)

    proposed_entry = build_mexc_entry_only_order(payload)

    entry_result = place_live_order_only_if_armed(proposed_entry["order_body"])

    if not mexc_success(entry_result.get("mexc_response", {})):
        return {
            "status": "entry_failed",
            "entry_order_sent": entry_result.get("live_order_sent", False),
            "proposed_entry": proposed_entry,
            "entry_result": entry_result,
            "warning": "No SL/TP attempted because entry did not succeed.",
        }

    entry_order_id = entry_result["mexc_response"]["data"].get("data")

    time.sleep(ENTRY_SETTLE_WAIT_SECONDS)

    position_snapshot = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)
    position, position_error = find_open_position(
        position_snapshot,
        MEXC_CONTRACT_SYMBOL,
        base_action=base_action
    )

    if position_error:
        emergency_state = save_live_state({
            "status": "ENTRY_OPEN_BUT_POSITION_NOT_FOUND",
            "symbol": MEXC_CONTRACT_SYMBOL,
            "side": "LONG" if base_action == "LONG_ENTRY" else "SHORT",
            "entryOrderId": entry_order_id,
            "qty_btc": qty_btc,
            "mexc_vol": mexc_vol,
            "stop": stop,
            "target": target,
            "position_snapshot": position_snapshot,
            "error": position_error,
            "danger": "Entry may be open without backend-managed SL/TP. Check MEXC immediately.",
        })

        return {
            "status": "entry_succeeded_but_position_lookup_failed",
            "entry_result": entry_result,
            "position_snapshot": position_snapshot,
            "state_saved": emergency_state,
            "danger": "Check MEXC immediately. Position may be open without SL/TP.",
        }

    position_id = extract_position_id(position)

    if not position_id:
        emergency_state = save_live_state({
            "status": "ENTRY_OPEN_BUT_POSITION_ID_MISSING",
            "symbol": MEXC_CONTRACT_SYMBOL,
            "side": "LONG" if base_action == "LONG_ENTRY" else "SHORT",
            "entryOrderId": entry_order_id,
            "qty_btc": qty_btc,
            "mexc_vol": mexc_vol,
            "stop": stop,
            "target": target,
            "position": position,
            "danger": "Entry may be open without backend-managed SL/TP. Check MEXC immediately.",
        })

        return {
            "status": "entry_succeeded_but_position_id_missing",
            "entry_result": entry_result,
            "position": position,
            "state_saved": emergency_state,
            "danger": "Check MEXC immediately. Position may be open without SL/TP.",
        }

    sltp_body = build_position_sltp_order(
        position_id=position_id,
        mexc_vol=mexc_vol,
        stop=stop,
        target=target
    )

    sltp_result = place_position_sltp_only_if_armed(sltp_body)

    time.sleep(0.5)

    stop_orders_snapshot = get_mexc_open_stop_orders(
        symbol=MEXC_CONTRACT_SYMBOL,
        position_id=position_id
    )
    matched_stop_orders = stop_orders_for_position(stop_orders_snapshot, position_id)

    sltp_success = mexc_success(sltp_result.get("mexc_response", {}))

    final_status = "OPEN_WITH_SLTP_PLACED" if sltp_success else "OPEN_BUT_SLTP_FAILED"

    state = save_live_state({
        "status": final_status,
        "symbol": MEXC_CONTRACT_SYMBOL,
        "side": "LONG" if base_action == "LONG_ENTRY" else "SHORT",
        "positionId": position_id,
        "entryOrderId": entry_order_id,
        "qty_btc": qty_btc,
        "mexc_vol": mexc_vol,
        "entry_reference_price": validated["price"],
        "currentStop": stop,
        "currentTarget": target,
        "position": position,
        "sltp_body": sltp_body,
        "sltp_success": sltp_success,
        "matched_stop_orders": matched_stop_orders,
        "danger": None if sltp_success else "Position is open but SL/TP placement failed. Check MEXC immediately.",
    })

    return {
        "status": final_status,
        "entry_result": entry_result,
        "position_snapshot": position_snapshot,
        "position": position,
        "positionId": position_id,
        "sltp_body": sltp_body,
        "sltp_result": sltp_result,
        "stop_orders_snapshot": stop_orders_snapshot,
        "matched_stop_orders": matched_stop_orders,
        "state_saved": state,
        "danger": None if sltp_success else "Position is open but SL/TP placement failed. Check MEXC immediately.",
    }


def trail_update_workflow(payload: dict):
    """
    Manual trail update workflow:
    1. read live state
    2. verify MEXC position is still open
    3. verify the stop improves
    4. modify existing TP/SL planned order via change_plan_price
    5. verify open stop order list
    6. save updated state
    """
    validated = validate_trail_update_payload(payload)
    base_action = validated["base_action"]
    new_stop = validated["stop"]
    new_target = validated["target"]

    live_state = load_live_state()

    if live_state.get("status") not in ["OPEN_WITH_SLTP_PLACED", "OPEN_WITH_SLTP_UPDATED"]:
        return {
            "status": "blocked",
            "reason": f"live state is not an open protected trade: {live_state.get('status')}",
            "live_state": live_state,
        }

    state_side = live_state.get("side")
    expected_action = "LONG_TRAIL_UPDATE" if state_side == "LONG" else "SHORT_TRAIL_UPDATE" if state_side == "SHORT" else None

    if expected_action is None or base_action != expected_action:
        return {
            "status": "blocked",
            "reason": f"trail action {base_action} does not match live state side {state_side}",
            "live_state": live_state,
        }

    current_stop = live_state.get("currentStop")
    current_target = live_state.get("currentTarget")

    if current_stop is None:
        return {
            "status": "blocked",
            "reason": "live state does not contain currentStop",
            "live_state": live_state,
        }

    current_stop = float(current_stop)
    effective_target = float(new_target if new_target is not None else current_target)

    if state_side == "LONG" and not new_stop > current_stop:
        return {
            "status": "blocked",
            "reason": f"LONG stop did not improve: old={current_stop}, new={new_stop}",
            "live_state": live_state,
        }

    if state_side == "SHORT" and not new_stop < current_stop:
        return {
            "status": "blocked",
            "reason": f"SHORT stop did not improve: old={current_stop}, new={new_stop}",
            "live_state": live_state,
        }

    position_id = live_state.get("positionId")
    if position_id in [None, "", 0, "0"]:
        return {
            "status": "blocked",
            "reason": "live state does not contain positionId",
            "live_state": live_state,
        }

    position_snapshot = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)
    matched_position = find_open_position_by_position_id(position_snapshot, position_id)

    if matched_position is None:
        state_after = save_live_state({
            **live_state,
            "status": "TRAIL_UPDATE_BLOCKED_POSITION_NOT_FOUND",
            "position_snapshot": position_snapshot,
            "danger": "Local state expected an open position, but MEXC did not show it. Reconcile before continuing.",
        })
        return {
            "status": "blocked",
            "reason": "MEXC open position not found for saved positionId",
            "position_snapshot": position_snapshot,
            "state_after": state_after,
            "danger": "Run /reconcile-live-state before continuing.",
        }

    stop_orders_snapshot_before = get_mexc_open_stop_orders(MEXC_CONTRACT_SYMBOL, position_id=position_id)
    matched_stop_orders_before = stop_orders_for_position(stop_orders_snapshot_before, position_id)

    stop_plan_order_id = get_stop_plan_order_id_from_orders(matched_stop_orders_before)

    if stop_plan_order_id is None:
        state_after = save_live_state({
            **live_state,
            "status": "TRAIL_UPDATE_BLOCKED_STOP_PLAN_ID_MISSING",
            "position": matched_position,
            "matched_stop_orders": matched_stop_orders_before,
            "danger": "Position exists but no stopPlanOrderId was found. Check MEXC immediately.",
        })
        return {
            "status": "blocked",
            "reason": "No stopPlanOrderId found for existing TP/SL planned order",
            "stop_orders_snapshot_before": stop_orders_snapshot_before,
            "matched_stop_orders_before": matched_stop_orders_before,
            "state_after": state_after,
            "danger": "Manual inspection required.",
        }

    change_body = build_change_plan_price_order(
        stop_plan_order_id=stop_plan_order_id,
        stop=new_stop,
        target=effective_target,
    )

    change_result = change_plan_price_only_if_armed(change_body)

    time.sleep(0.5)

    stop_orders_snapshot_after = get_mexc_open_stop_orders(MEXC_CONTRACT_SYMBOL, position_id=position_id)
    matched_stop_orders_after = stop_orders_for_position(stop_orders_snapshot_after, position_id)
    change_success = mexc_success(change_result.get("mexc_response", {}))

    final_status = "OPEN_WITH_SLTP_UPDATED" if change_success else "OPEN_BUT_TRAIL_UPDATE_FAILED"

    state_after = save_live_state({
        **live_state,
        "status": final_status,
        "symbol": MEXC_CONTRACT_SYMBOL,
        "side": state_side,
        "positionId": position_id,
        "position": matched_position,
        "currentStop": new_stop if change_success else current_stop,
        "currentTarget": effective_target,
        "lastTrailUpdate": {
            "action": base_action,
            "oldStop": current_stop,
            "newStop": new_stop,
            "target": effective_target,
            "price": validated["price"],
            "stopPlanOrderId": stop_plan_order_id,
            "change_body": change_body,
            "change_success": change_success,
            "updated_at_utc": utc_now(),
        },
        "matched_stop_orders": matched_stop_orders_after,
        "danger": None if change_success else "Position is open but SL/TP modification failed. Check MEXC immediately.",
    })

    return {
        "status": final_status,
        "live_state_before": live_state,
        "position": matched_position,
        "positionId": position_id,
        "stop_plan_order_id": stop_plan_order_id,
        "change_body": change_body,
        "change_result": change_result,
        "stop_orders_snapshot_before": stop_orders_snapshot_before,
        "matched_stop_orders_before": matched_stop_orders_before,
        "stop_orders_snapshot_after": stop_orders_snapshot_after,
        "matched_stop_orders_after": matched_stop_orders_after,
        "state_saved": state_after,
        "danger": None if change_success else "Position is open but SL/TP modification failed. Check MEXC immediately.",
    }



def validate_exit_payload(payload: dict):
    """
    Validates LONG_EXIT / SHORT_EXIT payloads.

    TradingView exit alerts may send stop/target/qty as null.
    The backend closes based on the actual MEXC open position, not the alert qty.
    """
    action = payload.get("action")
    base_action = clean_action(action)

    if base_action not in {"LONG_EXIT", "SHORT_EXIT"}:
        raise ValueError(f"Cannot validate non-exit action: {action}")

    price_raw = payload.get("price")
    price = None if price_raw in [None, "", "null"] else to_float(price_raw, "price")

    if price is not None and price <= 0:
        raise ValueError(f"price must be > 0 when provided, got {price}")

    return {
        "price": price,
        "base_action": base_action,
    }


def close_side_for_state_side(state_side: str):
    """
    MEXC futures side codes:
    2 = close short
    4 = close long
    """
    if state_side == "LONG":
        return 4
    if state_side == "SHORT":
        return 2
    raise ValueError(f"Unsupported state side for close: {state_side}")


def build_mexc_close_order_from_position(position: dict, state_side: str):
    """
    Builds a market close order for the current MEXC position.

    Uses the actual MEXC holdVol, not the TradingView alert qty.
    In one-way mode, reduceOnly=true is included as an additional safety flag.
    """
    position_id = extract_position_id(position)
    hold_vol = extract_hold_vol(position)

    if position_id in [None, "", 0, "0"]:
        raise ValueError("Cannot close: positionId missing from MEXC position")

    if hold_vol is None or hold_vol <= 0:
        raise ValueError(f"Cannot close: invalid holdVol {hold_vol}")

    # BTC_USDT uses integer contract volume.
    close_vol = int(round(float(hold_vol)))

    if close_vol < MEXC_MIN_CONTRACT_VOL:
        raise ValueError(f"Cannot close: close_vol {close_vol} below minimum {MEXC_MIN_CONTRACT_VOL}")

    body = {
        "symbol": MEXC_CONTRACT_SYMBOL,
        "price": 0,
        "vol": close_vol,
        "leverage": MEXC_LEVERAGE,
        "side": close_side_for_state_side(state_side),
        "type": MEXC_ORDER_TYPE,
        "openType": MEXC_OPEN_TYPE,
        "externalOid": short_external_oid(),
        "positionMode": MEXC_POSITION_MODE,
        "positionId": int(position_id),
        "reduceOnly": True,
    }

    return body


def close_position_only_if_armed(close_body: dict):
    if not LIVE_TRADING_ENABLED:
        return {
            "close_order_sent": False,
            "reason": "blocked - LIVE_TRADING_ENABLED=false",
            "would_send_close_order": close_body,
        }

    return {
        "close_order_sent": True,
        "reason": f"LIVE_TRADING_ENABLED=true - submitting close order to MEXC path {MEXC_ORDER_CREATE_PATH}",
        "mexc_order_path": MEXC_ORDER_CREATE_PATH,
        "mexc_response": mexc_post_private(MEXC_ORDER_CREATE_PATH, close_body),
    }


def exit_workflow(payload: dict):
    """
    Live exit/reconciliation workflow:
    1. validate exit action
    2. read local live state
    3. confirm action matches live state side
    4. check MEXC open position
    5. if already flat: clear state
    6. if open: submit market close order
    7. verify MEXC is flat
    8. clear or mark state based on result
    """
    validated = validate_exit_payload(payload)
    base_action = validated["base_action"]

    live_state = load_live_state()

    if live_state.get("status") in ["EMPTY", None]:
        position_snapshot = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)
        has_position, mexc_reason = has_open_mexc_position(position_snapshot)
        if not has_position:
            state_after = clear_live_state("exit received but local state and MEXC are already flat")
            return {
                "status": "ALREADY_FLAT",
                "reason": "Local state empty and MEXC has no open position.",
                "position_snapshot": position_snapshot,
                "state_after": state_after,
            }
        return {
            "status": "blocked",
            "reason": "Local state is empty but MEXC has an open position. Reconcile manually before closing via webhook.",
            "position_snapshot": position_snapshot,
            "mexc_reason": mexc_reason,
            "danger": "Manual inspection required before sending close order.",
        }

    state_side = live_state.get("side")
    expected_action = "LONG_EXIT" if state_side == "LONG" else "SHORT_EXIT" if state_side == "SHORT" else None

    if expected_action is None or base_action != expected_action:
        return {
            "status": "blocked",
            "reason": f"exit action {base_action} does not match live state side {state_side}",
            "live_state": live_state,
        }

    position_id = live_state.get("positionId")
    position_snapshot_before = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)
    matched_position = None

    if position_id not in [None, "", 0, "0"]:
        matched_position = find_open_position_by_position_id(position_snapshot_before, position_id)

    if matched_position is None:
        # MEXC is already flat for the saved position. Clear local state.
        state_after = clear_live_state("exit reconciliation: saved position is already closed on MEXC")
        stop_orders_snapshot = get_mexc_open_stop_orders(MEXC_CONTRACT_SYMBOL, position_id=position_id)
        return {
            "status": "ALREADY_CLOSED_ON_MEXC",
            "reason": "Saved positionId not found in open positions; local state cleared.",
            "position_snapshot_before": position_snapshot_before,
            "stop_orders_snapshot": stop_orders_snapshot,
            "state_after": state_after,
        }

    close_body = build_mexc_close_order_from_position(matched_position, state_side)
    close_result = close_position_only_if_armed(close_body)
    close_success = mexc_success(close_result.get("mexc_response", {}))

    time.sleep(1.0)

    position_snapshot_after = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)
    matched_position_after = find_open_position_by_position_id(position_snapshot_after, position_id)
    stop_orders_snapshot_after = get_mexc_open_stop_orders(MEXC_CONTRACT_SYMBOL, position_id=position_id)

    if close_success and matched_position_after is None:
        state_after = clear_live_state("exit workflow: close order succeeded and MEXC is flat")
        final_status = "CLOSED_AND_STATE_CLEARED"
        danger = None
    else:
        state_after = save_live_state({
            **live_state,
            "status": "EXIT_SUBMITTED_BUT_POSITION_STILL_OPEN" if close_success else "EXIT_FAILED",
            "lastExitAttempt": {
                "action": base_action,
                "price": validated["price"],
                "close_body": close_body,
                "close_success": close_success,
                "updated_at_utc": utc_now(),
            },
            "position_snapshot_after": position_snapshot_after,
            "stop_orders_snapshot_after": stop_orders_snapshot_after,
            "danger": "Close order failed or position still open. Check MEXC immediately.",
        })
        final_status = state_after.get("status")
        danger = "Close order failed or position still open. Check MEXC immediately."

    return {
        "status": final_status,
        "live_state_before": live_state,
        "position_before": matched_position,
        "positionId": position_id,
        "close_body": close_body,
        "close_result": close_result,
        "position_snapshot_before": position_snapshot_before,
        "position_snapshot_after": position_snapshot_after,
        "matched_position_after": matched_position_after,
        "stop_orders_snapshot_after": stop_orders_snapshot_after,
        "state_after": state_after,
        "danger": danger,
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
        result.update({"reason": f"not an entry action: {action}"})
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

    live_state = load_live_state()
    if live_state.get("status") not in ["EMPTY", None]:
        result.update({
            "guard_passed": False,
            "would_enter": False,
            "reason": f"live state is not empty: {live_state.get('status')}",
            "live_state": live_state,
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
            return {"paper_accepted": False, "paper_reason": paper_state["last_reason"], "paper_state": paper_state.copy()}

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

        return {"paper_accepted": True, "paper_reason": "paper long opened", "paper_state": paper_state.copy()}

    if base_action == "SHORT_ENTRY":
        if paper_state["position"] != "flat":
            paper_state["last_action"] = action
            paper_state["last_reason"] = f"rejected SHORT_ENTRY because paper position is already {paper_state['position']}"
            paper_state["updated_at_utc"] = utc_now()
            return {"paper_accepted": False, "paper_reason": paper_state["last_reason"], "paper_state": paper_state.copy()}

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

        return {"paper_accepted": True, "paper_reason": "paper short opened", "paper_state": paper_state.copy()}

    if base_action == "LONG_TRAIL_UPDATE":
        if paper_state["position"] != "long":
            paper_state["last_action"] = action
            paper_state["last_reason"] = f"rejected LONG_TRAIL_UPDATE because paper position is {paper_state['position']}"
            paper_state["updated_at_utc"] = utc_now()
            return {"paper_accepted": False, "paper_reason": paper_state["last_reason"], "paper_state": paper_state.copy()}

        old_stop = paper_state["stop"]

        if stop is None or old_stop is None or stop <= old_stop:
            paper_state["last_action"] = action
            paper_state["last_reason"] = f"rejected LONG_TRAIL_UPDATE because stop did not improve: old={old_stop}, new={stop}"
            paper_state["updated_at_utc"] = utc_now()
            return {"paper_accepted": False, "paper_reason": paper_state["last_reason"], "paper_state": paper_state.copy()}

        paper_state.update({
            "stop": stop,
            "target": target,
            "last_action": action,
            "last_reason": "paper long stop updated",
            "updated_at_utc": utc_now(),
        })

        return {"paper_accepted": True, "paper_reason": "paper long stop updated", "paper_state": paper_state.copy()}

    if base_action == "SHORT_TRAIL_UPDATE":
        if paper_state["position"] != "short":
            paper_state["last_action"] = action
            paper_state["last_reason"] = f"rejected SHORT_TRAIL_UPDATE because paper position is {paper_state['position']}"
            paper_state["updated_at_utc"] = utc_now()
            return {"paper_accepted": False, "paper_reason": paper_state["last_reason"], "paper_state": paper_state.copy()}

        old_stop = paper_state["stop"]

        if stop is None or old_stop is None or stop >= old_stop:
            paper_state["last_action"] = action
            paper_state["last_reason"] = f"rejected SHORT_TRAIL_UPDATE because stop did not improve: old={old_stop}, new={stop}"
            paper_state["updated_at_utc"] = utc_now()
            return {"paper_accepted": False, "paper_reason": paper_state["last_reason"], "paper_state": paper_state.copy()}

        paper_state.update({
            "stop": stop,
            "target": target,
            "last_action": action,
            "last_reason": "paper short stop updated",
            "updated_at_utc": utc_now(),
        })

        return {"paper_accepted": True, "paper_reason": "paper short stop updated", "paper_state": paper_state.copy()}

    if base_action in {"LONG_EXIT", "SHORT_EXIT"}:
        paper_state.update({
            "position": "flat",
            "entry": None,
            "stop": None,
            "target": None,
            "qty": None,
            "last_action": action,
            "last_reason": f"paper position closed at price={price}",
            "updated_at_utc": utc_now(),
        })

        return {"paper_accepted": True, "paper_reason": paper_state["last_reason"], "paper_state": paper_state.copy()}

    paper_state["last_action"] = action
    paper_state["last_reason"] = f"no paper rule for action: {action}"
    paper_state["updated_at_utc"] = utc_now()

    return {"paper_accepted": False, "paper_reason": paper_state["last_reason"], "paper_state": paper_state.copy()}


# =====================================================
# Live state reconciliation helpers
# =====================================================

CLEAR_LIVE_STATE_CONFIRM_PHRASE = "I_UNDERSTAND_THIS_CLEARS_LIVE_STATE"
TRAIL_UPDATE_CONFIRM_PHRASE = "I_UNDERSTAND_THIS_MODIFIES_LIVE_SLTP"
EXIT_CONFIRM_PHRASE = "I_UNDERSTAND_THIS_CLOSES_LIVE_POSITION"


def find_open_position_by_position_id(mexc_result: dict, position_id):
    position_id_str = str(position_id)
    for pos in extract_positions(mexc_result):
        if not isinstance(pos, dict):
            continue
        if pos.get("symbol") != MEXC_CONTRACT_SYMBOL:
            continue
        if not position_is_open(pos):
            continue
        if str(extract_position_id(pos)) == position_id_str:
            return pos
    return None


def get_open_positions_for_symbol(mexc_result: dict, symbol: str):
    matched = []
    for pos in extract_positions(mexc_result):
        if not isinstance(pos, dict):
            continue
        if pos.get("symbol") != symbol:
            continue
        if position_is_open(pos):
            matched.append(pos)
    return matched


def reconcile_live_state_workflow(clear_if_flat: bool = True):
    """
    Reconcile local live_trade_state.json with MEXC.

    If local state says a trade is open but MEXC is flat, clear local state by default.
    If MEXC still has the position open, refresh position and stop-order information.
    """
    previous_state = load_live_state()
    position_snapshot = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)
    open_positions = get_open_positions_for_symbol(position_snapshot, MEXC_CONTRACT_SYMBOL)

    state_status = previous_state.get("status")
    saved_position_id = previous_state.get("positionId")

    if state_status == "EMPTY" and not open_positions:
        return {
            "status": "ok",
            "reconciliation": "already_empty_and_mexc_flat",
            "previous_state": previous_state,
            "position_snapshot": position_snapshot,
            "open_positions": open_positions,
            "state_after": previous_state,
        }

    if not position_snapshot.get("ok"):
        return {
            "status": "error",
            "reconciliation": "could_not_read_mexc_positions",
            "previous_state": previous_state,
            "position_snapshot": position_snapshot,
            "danger": "Could not verify whether MEXC has an open position.",
        }

    if not open_positions:
        stop_orders_snapshot = get_mexc_open_stop_orders(MEXC_CONTRACT_SYMBOL, position_id=saved_position_id)

        if clear_if_flat:
            state_after = clear_live_state("reconciled: MEXC is flat; local state cleared")
            reconciliation = "mexc_flat_local_state_cleared"
        else:
            state_after = save_live_state({
                "status": "CLOSED_OR_FLAT_ON_MEXC",
                "reason": "reconciled: MEXC has no open position for symbol",
                "previous_state": previous_state,
                "position_snapshot": position_snapshot,
                "stop_orders_snapshot": stop_orders_snapshot,
            })
            reconciliation = "mexc_flat_local_state_marked_closed"

        return {
            "status": "ok",
            "reconciliation": reconciliation,
            "previous_state": previous_state,
            "position_snapshot": position_snapshot,
            "stop_orders_snapshot": stop_orders_snapshot,
            "state_after": state_after,
        }

    # MEXC has at least one open position.
    matched_position = None

    if saved_position_id not in [None, "", 0, "0"]:
        matched_position = find_open_position_by_position_id(position_snapshot, saved_position_id)

    if matched_position is None and len(open_positions) == 1:
        matched_position = open_positions[0]

    if matched_position is None:
        state_after = save_live_state({
            "status": "RECONCILE_AMBIGUOUS_OPEN_POSITIONS",
            "reason": "MEXC has multiple open positions or saved positionId does not match; manual inspection required",
            "previous_state": previous_state,
            "position_snapshot": position_snapshot,
            "open_positions": open_positions,
            "danger": "Do not open another trade until this is resolved.",
        })
        return {
            "status": "warning",
            "reconciliation": "ambiguous_open_positions",
            "previous_state": previous_state,
            "open_positions": open_positions,
            "state_after": state_after,
            "danger": "Manual inspection required.",
        }

    position_id = extract_position_id(matched_position)
    stop_orders_snapshot = get_mexc_open_stop_orders(MEXC_CONTRACT_SYMBOL, position_id=position_id)
    matched_stop_orders = stop_orders_for_position(stop_orders_snapshot, position_id)

    refreshed_status = "OPEN_WITH_SLTP_PLACED" if matched_stop_orders else "OPEN_BUT_NO_STOP_ORDERS_FOUND"

    state_after = previous_state.copy() if isinstance(previous_state, dict) else {}
    state_after.update({
        "status": refreshed_status,
        "symbol": MEXC_CONTRACT_SYMBOL,
        "positionId": position_id,
        "position": matched_position,
        "matched_stop_orders": matched_stop_orders,
        "reconciled_at_utc": utc_now(),
        "danger": None if matched_stop_orders else "MEXC position is open but no matching SL/TP stop orders were found.",
    })
    state_after = save_live_state(state_after)

    return {
        "status": "ok" if matched_stop_orders else "warning",
        "reconciliation": "mexc_position_open_state_refreshed",
        "previous_state": previous_state,
        "position": matched_position,
        "positionId": position_id,
        "stop_orders_snapshot": stop_orders_snapshot,
        "matched_stop_orders": matched_stop_orders,
        "state_after": state_after,
        "danger": None if matched_stop_orders else "MEXC position is open but no matching SL/TP stop orders were found.",
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
        "mexc_stop_order_place_path": MEXC_STOP_ORDER_PLACE_PATH,
        "mexc_stop_order_change_plan_price_path": MEXC_STOP_ORDER_CHANGE_PLAN_PRICE_PATH,
        "exit_confirm_phrase_required": "I_UNDERSTAND_THIS_CLOSES_LIVE_POSITION",
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
        "entry_settle_wait_seconds": ENTRY_SETTLE_WAIT_SECONDS,
        "state_file_path": STATE_FILE_PATH,
    }


@app.get("/state")
def get_state():
    return {
        "status": "ok",
        "paper_state": paper_state,
        "live_state": load_live_state(),
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "auto_tv_execution_enabled": AUTO_TV_EXECUTION_ENABLED,
    }




@app.get("/live-state")
def live_state(request: Request):
    secret = request.query_params.get("secret")

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    return {
        "status": "ok",
        "live_state": load_live_state(),
        "state_file_path": STATE_FILE_PATH,
    }


@app.post("/clear-live-state")
def clear_live_state_route(request: Request):
    secret = request.query_params.get("secret")
    confirm = request.query_params.get("confirm")

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    if confirm != CLEAR_LIVE_STATE_CONFIRM_PHRASE:
        raise HTTPException(
            status_code=400,
            detail="Missing or incorrect confirmation phrase. Use confirm=I_UNDERSTAND_THIS_CLEARS_LIVE_STATE"
        )

    state_after = clear_live_state("manual clear-live-state endpoint")

    event = {
        "event": "clear_live_state",
        "received_at_utc": utc_now(),
        "state_after": state_after,
    }
    print(json.dumps(event))

    return {
        "status": "ok",
        "message": "live state cleared",
        "state_after": state_after,
    }


@app.post("/reconcile-live-state")
def reconcile_live_state_route(request: Request):
    secret = request.query_params.get("secret")
    clear_if_flat_raw = request.query_params.get("clear_if_flat", "true").lower()

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    clear_if_flat = clear_if_flat_raw in ["1", "true", "yes", "y"]
    result = reconcile_live_state_workflow(clear_if_flat=clear_if_flat)

    event = {
        "event": "reconcile_live_state",
        "received_at_utc": utc_now(),
        "clear_if_flat": clear_if_flat,
        "result": result,
    }
    print(json.dumps(event))

    return result


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

    live_state = clear_live_state("manual reset")

    return {
        "status": "ok",
        "message": "paper and live state reset",
        "paper_state": paper_state,
        "live_state": live_state,
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
        "result": result,
    }))

    return {
        "status": "ok",
        "mexc_contract_symbol": MEXC_CONTRACT_SYMBOL,
        "mexc_result": result,
    }


@app.get("/mexc-open-stop-orders")
def mexc_open_stop_orders(request: Request):
    secret = request.query_params.get("secret")
    position_id = request.query_params.get("positionId")

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    result = get_mexc_open_stop_orders(MEXC_CONTRACT_SYMBOL, position_id=position_id)

    print(json.dumps({
        "event": "mexc_open_stop_orders_check",
        "received_at_utc": utc_now(),
        "positionId": position_id,
        "result": result,
    }))

    return {
        "status": "ok",
        "mexc_contract_symbol": MEXC_CONTRACT_SYMBOL,
        "positionId": position_id,
        "mexc_result": result,
    }


@app.get("/dry-run-order")
def dry_run_order(request: Request):
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
    simulated_price = 80000 if price_param is None else to_float(price_param, "price")
    simulated_stop = simulated_price * 0.99 if stop_param is None and side == "long" else simulated_price * 1.01 if stop_param is None else to_float(stop_param, "stop")
    simulated_target = simulated_price * 1.02 if target_param is None and side == "long" else simulated_price * 0.98 if target_param is None else to_float(target_param, "target")

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

    try:
        proposed_entry = build_mexc_entry_only_order(simulated_payload)
        proposed_sltp = build_position_sltp_order(
            position_id=123456789,
            mexc_vol=proposed_entry["conversion"]["mexc_vol_contracts"],
            stop=simulated_stop,
            target=simulated_target,
        )

        return {
            "status": "ok",
            "simulated_payload": simulated_payload,
            "proposed_entry_order": proposed_entry,
            "proposed_position_sltp_order_example": {
                "note": "positionId is fake in dry-run; real positionId is read after entry opens",
                "body": proposed_sltp,
                "path": MEXC_STOP_ORDER_PLACE_PATH,
            },
            "dry_run_result": dry_run_only(proposed_entry["order_body"]),
        }

    except Exception as e:
        return {
            "status": "blocked",
            "reason": f"failed to build dry-run order: {str(e)}",
            "simulated_payload": simulated_payload,
        }


@app.get("/manual-trail-update-test")
def manual_trail_update_test(request: Request):
    """
    Controlled manual test for modifying existing TP/SL planned order.

    Required query params:
    - secret
    - side=long or short
    - price
    - stop
    - target
    - confirm=I_UNDERSTAND_THIS_MODIFIES_LIVE_SLTP
    """
    secret = request.query_params.get("secret")
    side = request.query_params.get("side", "").lower()
    confirm = request.query_params.get("confirm")

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    if confirm != TRAIL_UPDATE_CONFIRM_PHRASE:
        raise HTTPException(
            status_code=400,
            detail="Missing or incorrect confirmation phrase. Use confirm=I_UNDERSTAND_THIS_MODIFIES_LIVE_SLTP"
        )

    if not LIVE_TRADING_ENABLED:
        return {
            "status": "blocked",
            "reason": "LIVE_TRADING_ENABLED=false",
            "change_sent": False,
        }

    if AUTO_TV_EXECUTION_ENABLED:
        return {
            "status": "blocked",
            "reason": "AUTO_TV_EXECUTION_ENABLED=true. For manual testing, keep it false.",
            "change_sent": False,
        }

    if side not in {"long", "short"}:
        raise HTTPException(status_code=400, detail="side must be long or short")

    price = to_float(request.query_params.get("price"), "price")
    stop = to_float(request.query_params.get("stop"), "stop")
    target = to_float(request.query_params.get("target"), "target")

    simulated_action = "LONG_TRAIL_UPDATE" if side == "long" else "SHORT_TRAIL_UPDATE"

    payload = {
        "symbol": EXPECTED_SYMBOL,
        "action": simulated_action,
        "side": side.upper(),
        "price": price,
        "stop": stop,
        "target": target,
        "qty": None,
        "time": None,
        "timeframe": EXPECTED_TIMEFRAME,
    }

    result = trail_update_workflow(payload)

    event = {
        "event": "manual_trail_update_test",
        "received_at_utc": utc_now(),
        "payload": payload,
        "result": result,
    }
    print(json.dumps(event))

    return {
        "status": "ok" if str(result.get("status", "")).endswith("UPDATED") else result.get("status", "unknown"),
        "payload": payload,
        "trail_update_result": result,
    }


@app.get("/manual-exit-test")
def manual_exit_test(request: Request):
    """
    Controlled one-off live exit test.

    Required query params:
    - secret
    - side=long or short
    - price=current/reference price, optional but recommended
    - confirm=I_UNDERSTAND_THIS_CLOSES_LIVE_POSITION
    """
    secret = request.query_params.get("secret")
    side = request.query_params.get("side", "").lower()
    confirm = request.query_params.get("confirm")

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    if confirm != EXIT_CONFIRM_PHRASE:
        raise HTTPException(
            status_code=400,
            detail="Missing or incorrect confirmation phrase. Use confirm=I_UNDERSTAND_THIS_CLOSES_LIVE_POSITION"
        )

    if side not in {"long", "short"}:
        raise HTTPException(status_code=400, detail="side must be long or short")

    if not LIVE_TRADING_ENABLED:
        return {
            "status": "blocked",
            "reason": "LIVE_TRADING_ENABLED=false",
            "live_order_sent": False,
        }

    price_raw = request.query_params.get("price")
    price = None if price_raw in [None, "", "null"] else to_float(price_raw, "price")

    action = "LONG_EXIT" if side == "long" else "SHORT_EXIT"

    payload = {
        "symbol": EXPECTED_SYMBOL,
        "action": action,
        "side": side.upper(),
        "price": price,
        "stop": None,
        "target": None,
        "qty": None,
        "time": None,
        "timeframe": EXPECTED_TIMEFRAME,
    }

    workflow_result = exit_workflow(payload)

    event = {
        "event": "manual_exit_test",
        "received_at_utc": utc_now(),
        "payload": payload,
        "workflow_result": workflow_result,
    }
    print(json.dumps(event))

    return {
        "status": "ok",
        "payload": payload,
        "workflow_result": workflow_result,
        "critical_next_step": "Check MEXC immediately. Confirm position is closed and no leftover SL/TP remains.",
    }



@app.get("/manual-close-variant-test")
def manual_close_variant_test(request: Request):
    """
    Controlled one-off live close-order variant test.

    Purpose:
    - Test MEXC close-order body variants after order/create returned code 2001.
    - Uses actual MEXC holdVol from the saved live position.
    - Clears live state only if MEXC confirms the position is flat after the attempt.

    Required query params:
    - secret
    - side=long or short
    - close_side=1|2|3|4
    - confirm=I_UNDERSTAND_THIS_CLOSES_LIVE_POSITION

    Optional booleans, defaults shown:
    - include_position_id=false
    - include_reduce_only=false
    - include_position_mode=false
    - include_open_type=true
    - include_leverage=true
    """
    secret = request.query_params.get("secret")
    side = request.query_params.get("side", "").lower()
    confirm = request.query_params.get("confirm")

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    if confirm != EXIT_CONFIRM_PHRASE:
        raise HTTPException(
            status_code=400,
            detail="Missing or incorrect confirmation phrase. Use confirm=I_UNDERSTAND_THIS_CLOSES_LIVE_POSITION"
        )

    if side not in {"long", "short"}:
        raise HTTPException(status_code=400, detail="side must be long or short")

    if not LIVE_TRADING_ENABLED:
        return {
            "status": "blocked",
            "reason": "LIVE_TRADING_ENABLED=false",
            "close_order_sent": False,
        }

    def bool_param(name: str, default: bool):
        raw = request.query_params.get(name)
        if raw is None:
            return default
        return str(raw).lower() in {"1", "true", "yes", "y"}

    close_side_raw = request.query_params.get("close_side")
    if close_side_raw is None:
        raise HTTPException(status_code=400, detail="close_side is required: use 1, 2, 3, or 4")

    try:
        close_side = int(close_side_raw)
    except Exception:
        raise HTTPException(status_code=400, detail="close_side must be integer 1, 2, 3, or 4")

    if close_side not in {1, 2, 3, 4}:
        raise HTTPException(status_code=400, detail="close_side must be 1, 2, 3, or 4")

    include_position_id = bool_param("include_position_id", False)
    include_reduce_only = bool_param("include_reduce_only", False)
    include_position_mode = bool_param("include_position_mode", False)
    include_open_type = bool_param("include_open_type", True)
    include_leverage = bool_param("include_leverage", True)

    live_state = load_live_state()

    if live_state.get("status") in ["EMPTY", None]:
        return {
            "status": "blocked",
            "reason": "Local live state is empty. Open a tiny test position first.",
            "live_state": live_state,
        }

    expected_state_side = "LONG" if side == "long" else "SHORT"
    state_side = live_state.get("side")

    if state_side != expected_state_side:
        return {
            "status": "blocked",
            "reason": f"requested side {expected_state_side} does not match live state side {state_side}",
            "live_state": live_state,
        }

    position_id = live_state.get("positionId")
    position_snapshot_before = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)
    matched_position = None

    if position_id not in [None, "", 0, "0"]:
        matched_position = find_open_position_by_position_id(position_snapshot_before, position_id)

    if matched_position is None:
        state_after = clear_live_state("manual close variant: saved position already closed on MEXC")
        return {
            "status": "ALREADY_CLOSED_ON_MEXC",
            "reason": "Saved positionId not found in open positions; local state cleared.",
            "position_snapshot_before": position_snapshot_before,
            "state_after": state_after,
        }

    actual_position_id = extract_position_id(matched_position)
    hold_vol = extract_hold_vol(matched_position)

    if hold_vol is None or hold_vol <= 0:
        return {
            "status": "blocked",
            "reason": f"Invalid MEXC holdVol: {hold_vol}",
            "matched_position": matched_position,
        }

    close_vol = int(round(float(hold_vol)))

    close_body = {
        "symbol": MEXC_CONTRACT_SYMBOL,
        "price": 0,
        "vol": close_vol,
        "side": close_side,
        "type": MEXC_ORDER_TYPE,
        "externalOid": short_external_oid(),
    }

    if include_leverage:
        close_body["leverage"] = MEXC_LEVERAGE
    if include_open_type:
        close_body["openType"] = MEXC_OPEN_TYPE
    if include_position_mode:
        close_body["positionMode"] = MEXC_POSITION_MODE
    if include_position_id:
        close_body["positionId"] = int(actual_position_id)
    if include_reduce_only:
        close_body["reduceOnly"] = True

    close_result = close_position_only_if_armed(close_body)
    close_success = mexc_success(close_result.get("mexc_response", {}))

    time.sleep(1.0)

    position_snapshot_after = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)
    matched_position_after = find_open_position_by_position_id(position_snapshot_after, actual_position_id)
    stop_orders_snapshot_after = get_mexc_open_stop_orders(MEXC_CONTRACT_SYMBOL, position_id=actual_position_id)

    if close_success and matched_position_after is None:
        state_after = clear_live_state("manual close variant succeeded and MEXC is flat")
        status = "CLOSED_AND_STATE_CLEARED"
        danger = None
    else:
        state_after = save_live_state({
            **live_state,
            "status": "CLOSE_VARIANT_SUBMITTED_BUT_POSITION_STILL_OPEN" if close_success else "CLOSE_VARIANT_FAILED",
            "lastCloseVariantAttempt": {
                "close_side": close_side,
                "include_position_id": include_position_id,
                "include_reduce_only": include_reduce_only,
                "include_position_mode": include_position_mode,
                "include_open_type": include_open_type,
                "include_leverage": include_leverage,
                "close_body": close_body,
                "close_success": close_success,
                "updated_at_utc": utc_now(),
            },
            "position_snapshot_after": position_snapshot_after,
            "stop_orders_snapshot_after": stop_orders_snapshot_after,
            "danger": "Close variant failed or position still open. Check MEXC immediately.",
        })
        status = state_after.get("status")
        danger = "Close variant failed or position still open. Check MEXC immediately."

    event = {
        "event": "manual_close_variant_test",
        "received_at_utc": utc_now(),
        "requested_side": side,
        "variant": {
            "close_side": close_side,
            "include_position_id": include_position_id,
            "include_reduce_only": include_reduce_only,
            "include_position_mode": include_position_mode,
            "include_open_type": include_open_type,
            "include_leverage": include_leverage,
        },
        "close_body": close_body,
        "close_result": close_result,
        "position_snapshot_after": position_snapshot_after,
        "matched_position_after": matched_position_after,
        "state_after": state_after,
        "danger": danger,
    }
    print(json.dumps(event))

    return {
        "status": status,
        "requested_side": side,
        "variant": {
            "close_side": close_side,
            "include_position_id": include_position_id,
            "include_reduce_only": include_reduce_only,
            "include_position_mode": include_position_mode,
            "include_open_type": include_open_type,
            "include_leverage": include_leverage,
        },
        "live_state_before": live_state,
        "position_before": matched_position,
        "positionId": actual_position_id,
        "close_body": close_body,
        "close_result": close_result,
        "position_snapshot_before": position_snapshot_before,
        "position_snapshot_after": position_snapshot_after,
        "matched_position_after": matched_position_after,
        "stop_orders_snapshot_after": stop_orders_snapshot_after,
        "state_after": state_after,
        "danger": danger,
    }


@app.get("/manual-live-test-entry-then-sltp")
def manual_live_test_entry_then_sltp(request: Request):
    """
    Controlled manual test:
    entry-only market order -> read positionId -> place TP/SL by position.

    Required:
    secret
    side=long or short
    qty
    price
    stop
    target
    confirm=I_UNDERSTAND_THIS_PLACES_A_LIVE_ORDER
    """
    secret = request.query_params.get("secret")
    side = request.query_params.get("side", "").lower()
    confirm = request.query_params.get("confirm")

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    if confirm != MANUAL_LIVE_CONFIRM_PHRASE:
        raise HTTPException(
            status_code=400,
            detail="Missing or incorrect confirmation phrase. This endpoint can place a live entry and then SL/TP."
        )

    if not LIVE_TRADING_ENABLED:
        return {
            "status": "blocked",
            "reason": "LIVE_TRADING_ENABLED=false",
            "live_order_sent": False,
        }

    if AUTO_TV_EXECUTION_ENABLED:
        return {
            "status": "blocked",
            "reason": "AUTO_TV_EXECUTION_ENABLED=true. For manual testing, keep it false.",
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
    entry_guard = run_entry_guard(payload, is_test=False, mexc_position_snapshot=mexc_snapshot)

    if not entry_guard["guard_passed"] or not entry_guard["would_enter"]:
        return {
            "status": "blocked",
            "reason": "entry guard blocked manual entry-then-sltp test",
            "entry_guard": entry_guard,
            "live_order_sent": False,
        }

    result = entry_then_sltp_workflow(payload)

    event = {
        "event": "manual_live_test_entry_then_sltp",
        "received_at_utc": utc_now(),
        "payload": payload,
        "entry_guard": entry_guard,
        "workflow_result": result,
    }

    print(json.dumps(event))

    return {
        "status": "ok",
        "payload": payload,
        "entry_guard": entry_guard,
        "workflow_result": result,
        "critical_next_step": "Check MEXC immediately. Confirm position exists and SL/TP are visible.",
    }


@app.get("/manual-entry-only-test")
def manual_entry_only_test(request: Request):
    """
    Old controlled entry-only test retained as fallback.
    Prefer /manual-live-test-entry-then-sltp for next testing.
    """
    secret = request.query_params.get("secret")
    side = request.query_params.get("side", "").lower()
    confirm = request.query_params.get("confirm")

    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    if confirm != MANUAL_LIVE_CONFIRM_PHRASE:
        raise HTTPException(status_code=400, detail="Missing or incorrect confirmation phrase.")

    if not LIVE_TRADING_ENABLED:
        return {"status": "blocked", "reason": "LIVE_TRADING_ENABLED=false", "live_order_sent": False}

    if side not in {"long", "short"}:
        raise HTTPException(status_code=400, detail="side must be long or short")

    qty = to_float(request.query_params.get("qty"), "qty")
    price = to_float(request.query_params.get("price"), "price")
    stop = to_float(request.query_params.get("stop"), "stop")
    target = to_float(request.query_params.get("target"), "target")

    if qty > MAX_MANUAL_TEST_VOL:
        return {"status": "blocked", "reason": f"qty {qty} exceeds MAX_MANUAL_TEST_VOL {MAX_MANUAL_TEST_VOL}", "live_order_sent": False}

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
    entry_guard = run_entry_guard(payload, is_test=False, mexc_position_snapshot=mexc_snapshot)

    if not entry_guard["guard_passed"] or not entry_guard["would_enter"]:
        return {"status": "blocked", "reason": "entry guard blocked manual entry-only test", "entry_guard": entry_guard}

    proposed_order = build_mexc_entry_only_order(payload)
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
        "warning": "ENTRY ONLY: no SL/TP attached.",
    }


@app.post("/tv-webhook")
async def tradingview_webhook(request: Request):
    """
    TradingView webhook receiver.

    Current live-capable behavior:
    - LONG_ENTRY / SHORT_ENTRY can use entry_then_sltp_workflow only when both switches are true.
    - LONG_TRAIL_UPDATE / SHORT_TRAIL_UPDATE can use trail_update_workflow only when both switches are true.
    - TEST_ actions are always non-live.
    - EXIT actions are accepted but live execution is not implemented yet.
    """
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

    workflow_result = None

    if accepted:
        base_action = clean_action(action)

        # -----------------------------
        # Entry signals
        # -----------------------------
        if base_action in {"LONG_ENTRY", "SHORT_ENTRY"}:
            mexc_position_snapshot = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)
            entry_guard = run_entry_guard(
                payload,
                is_test=is_test,
                mexc_position_snapshot=mexc_position_snapshot,
            )

            if is_test:
                workflow_result = {
                    "live_order_sent": False,
                    "reason": "TEST entry event received; live execution intentionally blocked.",
                    "live_trading_enabled": LIVE_TRADING_ENABLED,
                    "auto_tv_execution_enabled": AUTO_TV_EXECUTION_ENABLED,
                }

            elif entry_guard["guard_passed"] and entry_guard["would_enter"]:
                if LIVE_TRADING_ENABLED and AUTO_TV_EXECUTION_ENABLED:
                    workflow_result = entry_then_sltp_workflow(payload)
                else:
                    try:
                        proposed_entry = build_mexc_entry_only_order(payload)
                        proposed_sltp_example = build_position_sltp_order(
                            position_id=123456789,
                            mexc_vol=proposed_entry["conversion"]["mexc_vol_contracts"],
                            stop=to_float(payload.get("stop"), "stop"),
                            target=to_float(payload.get("target"), "target"),
                        )
                        workflow_result = {
                            "live_order_sent": False,
                            "reason": "TradingView auto execution disabled. No live order submitted.",
                            "live_trading_enabled": LIVE_TRADING_ENABLED,
                            "auto_tv_execution_enabled": AUTO_TV_EXECUTION_ENABLED,
                            "would_send_entry_order": proposed_entry["order_body"],
                            "would_send_sltp_order_example": {
                                "note": "positionId is fake here; real positionId is read after entry opens",
                                "body": proposed_sltp_example,
                            },
                            "next_step_if_auto_enabled": "entry-only -> read positionId -> stoporder/place -> verify",
                        }
                    except Exception as e:
                        workflow_result = {
                            "live_order_sent": False,
                            "reason": f"failed to build proposed entry workflow: {str(e)}",
                        }

            else:
                workflow_result = {
                    "live_order_sent": False,
                    "reason": "entry guard did not pass; no live order submitted",
                    "entry_guard": entry_guard,
                }

        # -----------------------------
        # Trail-update signals
        # -----------------------------
        elif base_action in {"LONG_TRAIL_UPDATE", "SHORT_TRAIL_UPDATE"}:
            if is_test:
                workflow_result = {
                    "change_sent": False,
                    "reason": "TEST trail update received; live SL/TP modification intentionally blocked.",
                    "live_trading_enabled": LIVE_TRADING_ENABLED,
                    "auto_tv_execution_enabled": AUTO_TV_EXECUTION_ENABLED,
                    "live_state": load_live_state(),
                }

            elif LIVE_TRADING_ENABLED and AUTO_TV_EXECUTION_ENABLED:
                workflow_result = trail_update_workflow(payload)

            else:
                try:
                    validated = validate_trail_update_payload(payload)
                    workflow_result = {
                        "change_sent": False,
                        "reason": "TradingView auto execution disabled. No live SL/TP modification submitted.",
                        "live_trading_enabled": LIVE_TRADING_ENABLED,
                        "auto_tv_execution_enabled": AUTO_TV_EXECUTION_ENABLED,
                        "would_modify": {
                            "action": validated["base_action"],
                            "price": validated["price"],
                            "stop": validated["stop"],
                            "target": validated["target"],
                            "path": MEXC_STOP_ORDER_CHANGE_PLAN_PRICE_PATH,
                        },
                        "live_state": load_live_state(),
                    }
                except Exception as e:
                    workflow_result = {
                        "change_sent": False,
                        "reason": f"failed to validate proposed trail update: {str(e)}",
                    }

        # -----------------------------
        # Exit signals
        # -----------------------------
        elif base_action in {"LONG_EXIT", "SHORT_EXIT"}:
            if is_test:
                workflow_result = {
                    "close_order_sent": False,
                    "reason": "TEST exit received; live close intentionally blocked.",
                    "live_trading_enabled": LIVE_TRADING_ENABLED,
                    "auto_tv_execution_enabled": AUTO_TV_EXECUTION_ENABLED,
                    "live_state": load_live_state(),
                }

            elif LIVE_TRADING_ENABLED and AUTO_TV_EXECUTION_ENABLED:
                workflow_result = exit_workflow(payload)

            else:
                try:
                    validated = validate_exit_payload(payload)
                    workflow_result = {
                        "close_order_sent": False,
                        "reason": "TradingView auto execution disabled. No live close order submitted.",
                        "live_trading_enabled": LIVE_TRADING_ENABLED,
                        "auto_tv_execution_enabled": AUTO_TV_EXECUTION_ENABLED,
                        "would_close": {
                            "action": validated["base_action"],
                            "price": validated["price"],
                            "path": MEXC_ORDER_CREATE_PATH,
                            "note": "Actual close volume is read from MEXC open position, not TradingView qty.",
                        },
                        "live_state": load_live_state(),
                    }
                except Exception as e:
                    workflow_result = {
                        "close_order_sent": False,
                        "reason": f"failed to validate proposed exit: {str(e)}",
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
        "workflow_result": workflow_result,
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
        "workflow_result": workflow_result,
        "paper_result": paper_result,
        "mexc_position_snapshot": mexc_position_snapshot,
    }
