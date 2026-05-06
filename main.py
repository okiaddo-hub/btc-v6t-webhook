
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

        if base_action in {"LONG_ENTRY", "SHORT_ENTRY"}:
            mexc_position_snapshot = get_mexc_open_positions(MEXC_CONTRACT_SYMBOL)
            entry_guard = run_entry_guard(payload, is_test=is_test, mexc_position_snapshot=mexc_position_snapshot)

            if entry_guard["guard_passed"] and entry_guard["would_enter"]:
                if LIVE_TRADING_ENABLED and AUTO_TV_EXECUTION_ENABLED:
                    workflow_result = entry_then_sltp_workflow(payload)
                else:
                    try:
                        proposed_entry = build_mexc_entry_only_order(payload)
                        workflow_result = {
                            "live_order_sent": False,
                            "reason": "TradingView auto execution disabled. No live order submitted.",
                            "live_trading_enabled": LIVE_TRADING_ENABLED,
                            "auto_tv_execution_enabled": AUTO_TV_EXECUTION_ENABLED,
                            "would_send_entry_order": proposed_entry["order_body"],
                            "next_step_if_auto_enabled": "entry-only -> read positionId -> stoporder/place",
                        }
                    except Exception as e:
                        workflow_result = {"live_order_sent": False, "reason": f"failed to build proposed order: {str(e)}"}

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
