"""Delta India SuperTrend reversal bot for ETH, SOL, XRP, BNB, SNDKB, SOXL, and MVLL perpetuals.

Signals use Delta mark-price candles and SuperTrend(15, 3) on the selected timeframe.
The script only evaluates completed candles. Live order placement is disabled
unless ENABLE_LIVE_TRADING=true is set in .env.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import signal
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import pandas as pd
import requests
from dotenv import load_dotenv


load_dotenv()

API_BASE = os.getenv("DELTA_API_BASE", "https://api.india.delta.exchange").rstrip("/")
API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()
LIVE_TRADING = os.getenv("ENABLE_LIVE_TRADING", "false").strip().lower() == "true"
SUPPORTED_TIMEFRAMES = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
TIMEFRAME = os.getenv("TIMEFRAME", "1m").strip().lower()
ATR_LENGTH = 15
FACTOR = 3.0
CANDLE_LIMIT = 500
# Check pending maker orders every 2 seconds, reprice five times, then use the
# market fallback only after 12 seconds if any remainder is still unfilled.
POLL_SECONDS = 2
MAKER_TIMEOUT_SECONDS = 12
MAKER_REPRICE_SECONDS = (2, 4, 6, 8, 10)
SUPERTREND_STOP_OFFSET = Decimal("0.0035")
STOP_ORDER_DELAY_SECONDS = 10
PRODUCT_TICK_SIZES: dict[str, Decimal] = {}
STOP_REQUESTED = False
ORDER_REPRICE_ATTEMPTS: dict[str, int] = {}
STOP_POSITION_SIGNS: dict[str, int] = {}
STOP_FIRST_SEEN_AT: dict[str, float] = {}
STOP_ACTIVE_ORDER_IDS: dict[str, str] = {}
STOP_REENTRY_AFTER_CANDLE: dict[str, int] = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("delta-supertrend")
SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json", "User-Agent": "delta-supertrend-python"})
PAPER_POSITIONS: dict[str, int] = {}
LAST_KNOWN_POSITIONS: dict[str, int] = {}
TRADE_TIMEZONE = timezone(timedelta(hours=5, minutes=30))


def log_trade_event(message: str) -> None:
    event_time = datetime.now(TRADE_TIMEZONE).strftime("%H:%M:%S")
    log.info("TRADE_EVENT|%s|%s", event_time, message)


@dataclass(frozen=True)
class Contract:
    symbol: str
    size: int


def configured_contracts() -> list[Contract]:
    if TIMEFRAME not in SUPPORTED_TIMEFRAMES:
        allowed = ", ".join(SUPPORTED_TIMEFRAMES)
        raise ValueError(f"TIMEFRAME must be one of: {allowed}")
    all_contracts = {
        "ETHUSD": Contract("ETHUSD", int(os.getenv("LOTS_PER_SEGMENT", os.getenv("ETH_SIZE", "1")))),
        "SOLUSD": Contract("SOLUSD", int(os.getenv("LOTS_PER_SEGMENT", os.getenv("SOL_SIZE", "1")))),
        "XRPUSD": Contract("XRPUSD", int(os.getenv("LOTS_PER_SEGMENT", os.getenv("XRP_SIZE", "1")))),
        "BNBUSD": Contract("BNBUSD", int(os.getenv("LOTS_PER_SEGMENT", os.getenv("BNB_SIZE", "1")))),
        "SNDKBUSD": Contract("SNDKBUSD", int(os.getenv("LOTS_PER_SEGMENT", os.getenv("SNDKB_SIZE", "1")))),
        "SOXLBUSD": Contract("SOXLBUSD", int(os.getenv("LOTS_PER_SEGMENT", os.getenv("SOXLB_SIZE", "1")))),
        "MVLLBUSD": Contract("MVLLBUSD", int(os.getenv("LOTS_PER_SEGMENT", os.getenv("MVLL_SIZE", "1")))),
    }
    selected_symbols = [
        symbol.strip().upper()
        for symbol in os.getenv("TRADE_SYMBOLS", "ETHUSD,SOLUSD,XRPUSD,BNBUSD,SNDKBUSD,SOXLBUSD,MVLLBUSD").split(",")
        if symbol.strip()
    ]
    if not selected_symbols or any(symbol not in all_contracts for symbol in selected_symbols):
        raise ValueError("TRADE_SYMBOLS must contain ETHUSD, SOLUSD, XRPUSD, BNBUSD, SNDKBUSD, SOXLBUSD, and/or MVLLBUSD")
    contracts = [all_contracts[symbol] for symbol in dict.fromkeys(selected_symbols)]
    if any(contract.size <= 0 for contract in contracts):
        raise ValueError("All configured segment lot sizes must be positive integers")
    return contracts


def _query_string(params: dict[str, Any] | None) -> str:
    if not params:
        return ""
    return "?" + urlencode(params)


def _auth_headers(method: str, path: str, query: str = "", body: str = "") -> dict[str, str]:
    if not API_KEY or not API_SECRET:
        raise RuntimeError("Set DELTA_API_KEY and DELTA_API_SECRET in your .env file")
    timestamp = str(int(time.time()))
    prehash = method.upper() + timestamp + path + query + body
    signature = hmac.new(API_SECRET.encode(), prehash.encode(), hashlib.sha256).hexdigest()
    return {
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "User-Agent": "delta-supertrend-python",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _check_response(response: requests.Response) -> dict[str, Any]:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        try:
            details: Any = response.json()
        except ValueError:
            details = response.text[:500]
        raise RuntimeError(f"Delta HTTP {response.status_code}: {details}") from exc
    data = response.json()
    if not data.get("success", False):
        raise RuntimeError(f"Delta API error: {data}")
    return data


def get_mark_candles(symbol: str, timeframe: str | None = None) -> pd.DataFrame:
    """Fetch recent Delta mark-price candles and discard the still-forming bar."""
    timeframe = (timeframe or TIMEFRAME).strip().lower()
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    now = int(time.time())
    params = {
        "resolution": timeframe,
        "symbol": f"MARK:{symbol}",
        "start": now - CANDLE_LIMIT * SUPPORTED_TIMEFRAMES[timeframe],
        "end": now,
    }
    response = SESSION.get(f"{API_BASE}/v2/history/candles", params=params, timeout=15)
    data = _check_response(response)
    candles = data.get("result", [])
    if not candles:
        raise RuntimeError(f"No candle data returned for MARK:{symbol}")
    frame = pd.DataFrame(candles)
    frame = frame.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    # Delta timestamps candle opening time in Unix seconds. Only use bars whose
    # full selected candle interval has elapsed, avoiding intrabar signals.
    frame = frame[frame["time"].astype(int) + SUPPORTED_TIMEFRAMES[timeframe] <= now].copy()
    if len(frame) < ATR_LENGTH + 2:
        raise RuntimeError(f"Not enough completed candles for MARK:{symbol}")
    for column in ("high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    return frame.reset_index(drop=True)


def add_supertrend(frame: pd.DataFrame) -> pd.DataFrame:
    """Calculate Wilder ATR and conventional SuperTrend bands/direction."""
    result = frame.copy()
    previous_close = result["close"].shift(1)
    true_range = pd.concat(
        [
            result["high"] - result["low"],
            (result["high"] - previous_close).abs(),
            (result["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(alpha=1 / ATR_LENGTH, adjust=False, min_periods=ATR_LENGTH).mean()
    midpoint = (result["high"] + result["low"]) / 2
    basic_upper = midpoint + FACTOR * atr
    basic_lower = midpoint - FACTOR * atr

    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()
    direction = pd.Series(index=result.index, dtype="int64")
    supertrend = pd.Series(index=result.index, dtype="float64")

    first_valid = atr.first_valid_index()
    if first_valid is None:
        raise RuntimeError("Could not calculate ATR")
    direction.loc[first_valid] = 1
    supertrend.loc[first_valid] = basic_lower.loc[first_valid]

    for i in range(first_valid + 1, len(result)):
        prior = i - 1
        if basic_upper.iloc[i] < final_upper.iloc[prior] or result["close"].iloc[prior] > final_upper.iloc[prior]:
            final_upper.iloc[i] = basic_upper.iloc[i]
        else:
            final_upper.iloc[i] = final_upper.iloc[prior]

        if basic_lower.iloc[i] > final_lower.iloc[prior] or result["close"].iloc[prior] < final_lower.iloc[prior]:
            final_lower.iloc[i] = basic_lower.iloc[i]
        else:
            final_lower.iloc[i] = final_lower.iloc[prior]

        if direction.iloc[prior] == -1:
            direction.iloc[i] = 1 if result["close"].iloc[i] > final_upper.iloc[i] else -1
        else:
            direction.iloc[i] = -1 if result["close"].iloc[i] < final_lower.iloc[i] else 1
        supertrend.iloc[i] = final_lower.iloc[i] if direction.iloc[i] == 1 else final_upper.iloc[i]

    result["direction"] = direction
    result["supertrend"] = supertrend
    return result.dropna(subset=["direction", "supertrend"]).reset_index(drop=True)


def signed_request(method: str, path: str, params: dict[str, Any] | None = None,
                   body_obj: dict[str, Any] | None = None) -> dict[str, Any]:
    query = _query_string(params)
    body = json.dumps(body_obj, separators=(",", ":")) if body_obj is not None else ""
    headers = _auth_headers(method, path, query, body)
    response = SESSION.request(
        method.upper(), f"{API_BASE}{path}", params=params,
        data=body if body else None, headers=headers, timeout=15,
    )
    return _check_response(response)


def get_position(product_id: int) -> int:
    data = signed_request("GET", "/v2/positions", {"product_id": product_id})
    position = data.get("result") or {}
    return int(position.get("size", 0))


def get_product_id(symbol: str) -> int:
    response = SESSION.get(f"{API_BASE}/v2/products/{symbol}", timeout=15)
    data = _check_response(response)
    return int(data["result"]["id"])


def get_product_tick_size(symbol: str) -> Decimal:
    if symbol not in PRODUCT_TICK_SIZES:
        response = SESSION.get(f"{API_BASE}/v2/products/{symbol}", timeout=15)
        data = _check_response(response)
        tick_size = (data.get("result") or {}).get("tick_size")
        if not tick_size:
            raise RuntimeError(f"Delta did not provide a tick size for {symbol}")
        PRODUCT_TICK_SIZES[symbol] = Decimal(str(tick_size))
    return PRODUCT_TICK_SIZES[symbol]


def get_best_quote(symbol: str, side: str) -> str:
    response = SESSION.get(f"{API_BASE}/v2/tickers/{symbol}", timeout=10)
    data = _check_response(response)
    ticker = data.get("result") or {}
    quotes = ticker.get("quotes") or {}
    quote = quotes.get("best_bid" if side == "buy" else "best_ask")
    if quote is None:
        raise RuntimeError(f"Delta returned no passive quote for {symbol} {side}")
    return str(quote)


def get_strategy_open_orders(product_id: int) -> list[dict[str, Any]]:
    if not LIVE_TRADING:
        return []
    data = signed_request("GET", "/v2/orders", {"product_ids": product_id, "states": "open,pending"})
    orders = data.get("result", [])
    return [order for order in orders if str(order.get("client_order_id", "")).startswith("stbot-")]


def get_strategy_open_stop_orders(product_id: int) -> list[dict[str, Any]]:
    """Return only this bot's independent SuperTrend stop orders."""
    if not LIVE_TRADING:
        return []
    data = signed_request("GET", "/v2/orders", {"product_ids": product_id, "states": "open,pending"})
    orders = data.get("result", [])
    return [order for order in orders
            if str(order.get("client_order_id", "")).startswith("stsl-")
            and order.get("stop_order_type") == "stop_loss_order"]


def cancel_order(product_id: int, order: dict[str, Any]) -> None:
    if not LIVE_TRADING:
        return
    signed_request("DELETE", "/v2/orders", body_obj={"id": order["id"], "product_id": product_id})
    log.info("Cancelled strategy order %s for %s", order.get("id"), order.get("product_symbol"))


def supertrend_stop_price(symbol: str, direction: int, supertrend: float) -> str:
    """Place the stop 0.35% beyond the SuperTrend, rounded away from the market."""
    tick = get_product_tick_size(symbol)
    level = Decimal(str(supertrend))
    raw_price = level * (Decimal("1") - SUPERTREND_STOP_OFFSET
                         if direction == 1 else Decimal("1") + SUPERTREND_STOP_OFFSET)
    ticks = raw_price / tick
    rounded_ticks = ticks.to_integral_value(rounding=ROUND_FLOOR if direction == 1 else ROUND_CEILING)
    return format(rounded_ticks * tick, "f")


def place_supertrend_stop(
    contract: Contract, product_id: int, position: int, stop_price: str
) -> str | None:
    side = "sell" if position > 0 else "buy"
    payload = {
        "product_id": product_id,
        "size": abs(position),
        "side": side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": stop_price,
        "stop_trigger_method": "mark_price",
        "reduce_only": True,
        "time_in_force": "gtc",
        "client_order_id": f"stsl-{contract.symbol}-{int(time.time())}-{uuid.uuid4().hex[:5]}",
    }
    data = signed_request("POST", "/v2/orders", body_obj=payload)
    result = data.get("result", data)
    log.warning("Placed separate reduce-only SuperTrend stop id=%s %s %s lots=%d at %s (0.35%% offset)",
                result.get("id"), contract.symbol, side, abs(position), stop_price)
    return str(result["id"]) if result.get("id") is not None else None


def sync_supertrend_stop(
    contract: Contract, product_id: int, position: int, direction: int,
    supertrend: float, candle_time: int,
) -> None:
    """Place a separate stop 10 seconds after fill; then only tighten it."""
    if not LIVE_TRADING:
        return
    stops = get_strategy_open_stop_orders(product_id)
    if position == 0:
        STOP_POSITION_SIGNS.pop(contract.symbol, None)
        STOP_FIRST_SEEN_AT.pop(contract.symbol, None)
        previous_stop_id = STOP_ACTIVE_ORDER_IDS.pop(contract.symbol, None)
        if previous_stop_id:
            try:
                data = signed_request("GET", f"/v2/orders/{previous_stop_id}")
                finished_stop = data.get("result") or {}
                if (finished_stop.get("state") == "closed"
                        and finished_stop.get("stop_order_type") == "stop_loss_order"):
                    interval = SUPPORTED_TIMEFRAMES[TIMEFRAME]
                    STOP_REENTRY_AFTER_CANDLE[contract.symbol] = candle_time + 2 * interval
                    log.warning("%s protective stop filled; waiting for the next full %s candle before entry",
                                contract.symbol, TIMEFRAME)
            except Exception as exc:
                log.warning("Could not confirm final state of %s stop order %s: %s",
                            contract.symbol, previous_stop_id, exc)
        for order in stops:
            cancel_order(product_id, order)
        return

    position_sign = 1 if position > 0 else -1
    if STOP_POSITION_SIGNS.get(contract.symbol) != position_sign:
        STOP_POSITION_SIGNS[contract.symbol] = position_sign
        STOP_FIRST_SEEN_AT[contract.symbol] = time.monotonic()

    expected_side = "sell" if position > 0 else "buy"
    matching = [order for order in stops
                if order.get("side") == expected_side and bool(order.get("reduce_only"))]
    keep = matching[0] if matching else None
    for order in stops:
        if keep is None or order.get("id") != keep.get("id"):
            cancel_order(product_id, order)
    if keep is not None and keep.get("id") is not None:
        STOP_ACTIVE_ORDER_IDS[contract.symbol] = str(keep["id"])

    # During a SuperTrend reversal, the existing position is being closed by the
    # strategy. Keep its last valid protective stop until it closes; the newly
    # flipped SuperTrend lies on the wrong side for calculating this position's stop.
    if direction != (1 if position > 0 else -1):
        return

    desired_price = supertrend_stop_price(contract.symbol, direction, supertrend)
    if keep is None:
        first_seen = STOP_FIRST_SEEN_AT.setdefault(contract.symbol, time.monotonic())
        elapsed = time.monotonic() - first_seen
        if elapsed < STOP_ORDER_DELAY_SECONDS:
            return
        order_id = place_supertrend_stop(contract, product_id, position, desired_price)
        if order_id:
            STOP_ACTIVE_ORDER_IDS[contract.symbol] = order_id
        STOP_FIRST_SEEN_AT.pop(contract.symbol, None)
        return

    STOP_FIRST_SEEN_AT.pop(contract.symbol, None)

    try:
        current_price = Decimal(str(keep.get("stop_price")))
        target_price = Decimal(desired_price)
    except Exception:
        current_price = Decimal("0")
        target_price = Decimal(desired_price)

    # A long stop can only rise; a short stop can only fall. Never widen risk.
    should_tighten = target_price > current_price if position > 0 else target_price < current_price
    current_size = int(keep.get("size", keep.get("unfilled_size", 0)) or 0)
    if should_tighten or current_size != abs(position):
        signed_request("PUT", "/v2/orders", body_obj={
            "id": keep["id"],
            "product_id": product_id,
            "size": abs(position),
            "stop_price": desired_price if should_tighten else str(current_price),
        })
        log.info("Updated SuperTrend stop for %s to %s (%d lots)",
                 contract.symbol, desired_price if should_tighten else current_price, abs(position))


def reprice_post_only_order(product_id: int, symbol: str, order: dict[str, Any]) -> None:
    """Move an existing maker order to the current passive best quote."""
    if not LIVE_TRADING:
        return
    side = str(order.get("side", ""))
    order_id = order.get("id")
    price = get_best_quote(symbol, side)
    old_price = order.get("limit_price")
    try:
        if old_price is not None and float(old_price) == float(price):
            log.info("Maker order %s already at current %s quote %s", order_id, side, price)
            return
    except (TypeError, ValueError):
        if str(old_price) == str(price):
            return
    payload = {
        "id": order_id,
        "product_id": product_id,
        "limit_price": price,
        "size": int(order.get("size", order.get("unfilled_size", 0))),
        "post_only": True,
    }
    data = signed_request("PUT", "/v2/orders", body_obj=payload)
    result = data.get("result", data)
    log.info("Repriced post-only order id=%s %s %s from %s to %s state=%s",
             order_id, symbol, side, old_price, price, result.get("state"))


def manage_pending_maker_order(
    contract: Contract, product_id: int, direction: int, order: dict[str, Any]
) -> None:
    """Reprice at 2, 4, 6, 8, and 10 seconds; market fallback at 12 seconds."""
    order_id = str(order.get("id") or order.get("client_order_id") or "")
    age = order_age_seconds(order)
    if age is None:
        return
    if age >= MAKER_TIMEOUT_SECONDS:
        log.warning("%s maker order %s unfilled for %.1fs; switching remaining quantity to market",
                    contract.symbol, order_id, age)
        if LIVE_TRADING:
            market_fallback_for_pending(contract, product_id, direction, order)
        ORDER_REPRICE_ATTEMPTS.pop(order_id, None)
        return

    completed_attempts = ORDER_REPRICE_ATTEMPTS.get(order_id, 0)
    next_attempt = completed_attempts + 1
    if next_attempt > len(MAKER_REPRICE_SECONDS) or age < MAKER_REPRICE_SECONDS[next_attempt - 1]:
        return
    # Mark the attempt before calling Delta so transient edit errors do not cause
    # repeated amend requests on every 2-second poll.
    ORDER_REPRICE_ATTEMPTS[order_id] = next_attempt
    try:
        reprice_post_only_order(product_id, contract.symbol, order)
    except Exception as exc:
        log.warning("Could not reprice maker order %s for %s: %s", order_id, contract.symbol, exc)


def place_post_only_limit_order(
    symbol: str, lots: int, side: str, reduce_only: bool = False
) -> None:
    limit_price = get_best_quote(symbol, side)
    payload = {
        "product_symbol": symbol,
        "size": int(lots),
        "side": side,
        "order_type": "limit_order",
        "limit_price": limit_price,
        "time_in_force": "gtc",
        "post_only": True,
        "reduce_only": reduce_only,
        "client_order_id": f"stbot-{symbol}-{int(time.time())}-{uuid.uuid4().hex[:6]}",
    }
    if not LIVE_TRADING:
        log.warning("PAPER MODE: would place post-only %s %s lots=%d price=%s reduce_only=%s",
                    symbol, side, lots, limit_price, reduce_only)
        return
    for attempt in range(4):
        payload["limit_price"] = limit_price
        try:
            data = signed_request("POST", "/v2/orders", body_obj=payload)
            result = data.get("result", data)
            log.info("Delta accepted post-only limit order id=%s %s %s lots=%d at %s state=%s",
                     result.get("id"), symbol, side, lots, limit_price, result.get("state"))
            return
        except RuntimeError as exc:
            if "immediate_execution_post_only" not in str(exc) or attempt == 3:
                raise
            tick = get_product_tick_size(symbol)
            quote = Decimal(get_best_quote(symbol, side))
            # Move one valid tick farther from the market before retrying.
            price_ticks = quote / tick
            if side == "buy":
                price_ticks = price_ticks.to_integral_value(rounding=ROUND_FLOOR) - 1
            else:
                price_ticks = price_ticks.to_integral_value(rounding=ROUND_CEILING) + 1
            limit_price = format(price_ticks * tick, "f")
            log.warning("Delta rejected a marketable post-only %s for %s; retrying farther from the market at %s (attempt %d/3)",
                        side, symbol, limit_price, attempt + 1)


def place_market_order(symbol: str, lots: int, side: str, reduce_only: bool = False) -> None:
    payload = {
        "product_symbol": symbol,
        "size": int(lots),
        "side": side,
        "order_type": "market_order",
        "reduce_only": reduce_only,
        "client_order_id": f"stbot-{symbol}-{int(time.time())}-{uuid.uuid4().hex[:6]}",
    }
    if not LIVE_TRADING:
        log.warning("PAPER MODE: would use market fallback %s %s lots=%d reduce_only=%s",
                    symbol, side, lots, reduce_only)
        return
    data = signed_request("POST", "/v2/orders", body_obj=payload)
    result = data.get("result", data)
    log.warning("Maker timeout: Delta accepted market fallback id=%s %s %s lots=%d reduce_only=%s",
                result.get("id"), symbol, side, lots, reduce_only)
    instrument = symbol.removesuffix("USD")
    trade_side = "long" if side == "buy" else "short"
    action = f"close {('long' if side == 'sell' else 'short')}" if reduce_only else f"{trade_side} market order"
    log_trade_event(f"{instrument} {action} order placed after the 12-second maker timeout")


def order_age_seconds(order: dict[str, Any]) -> float | None:
    created = order.get("created_at")
    try:
        timestamp = float(created)
    except (TypeError, ValueError):
        try:
            timestamp = float(str(order.get("client_order_id", "")).split("-")[2])
        except (IndexError, TypeError, ValueError):
            return None
    if timestamp > 1e14:  # Delta order timestamps are microseconds.
        timestamp /= 1_000_000
    elif timestamp > 1e11:  # tolerate millisecond timestamps
        timestamp /= 1_000
    return max(0.0, time.time() - timestamp)


def market_fallback_for_pending(
    contract: Contract, product_id: int, direction: int, order: dict[str, Any]
) -> None:
    """Cancel an expired maker order and market the still-needed quantity."""
    cancel_order(product_id, order)
    time.sleep(0.25)
    current = get_position(product_id)
    if bool(order.get("reduce_only")):
        if current == 0:
            log.info("%s position closed while cancelling maker order; no market fallback needed", contract.symbol)
            return
        side = "sell" if current > 0 else "buy"
        place_market_order(contract.symbol, abs(current), side, reduce_only=True)
        return

    desired_sign = 1 if direction == 1 else -1
    current_sign = 0 if current == 0 else (1 if current > 0 else -1)
    if current_sign not in (0, desired_sign):
        log.warning("%s position changed while cancelling maker order; skipping market fallback", contract.symbol)
        return
    remaining = contract.size - abs(current) if current_sign == desired_sign else contract.size
    if remaining > 0:
        side = "buy" if desired_sign == 1 else "sell"
        place_market_order(contract.symbol, remaining, side)


def market_fallback_for_reversal(
    contract: Contract, product_id: int, direction: int, orders: list[dict[str, Any]]
) -> None:
    """Cancel both reversal limits, then safely reconcile the net position at market."""
    for order in orders:
        try:
            cancel_order(product_id, order)
        except Exception:
            log.exception("Could not cancel reversal order %s for %s", order.get("id"), contract.symbol)
    time.sleep(0.25)
    remaining_orders = get_strategy_open_orders(product_id)
    if remaining_orders:
        log.warning("%s reversal fallback deferred because strategy orders remain open", contract.symbol)
        return

    desired_sign = 1 if direction == 1 else -1
    current = get_position(product_id)
    current_sign = 0 if current == 0 else (1 if current > 0 else -1)
    if current_sign not in (0, desired_sign):
        close_side = "sell" if current > 0 else "buy"
        place_market_order(contract.symbol, abs(current), close_side, reduce_only=True)
        time.sleep(0.5)
        current = get_position(product_id)
        current_sign = 0 if current == 0 else (1 if current > 0 else -1)

    if current_sign == desired_sign:
        remaining_lots = max(0, contract.size - abs(current))
    elif current == 0:
        remaining_lots = contract.size
    else:
        log.error("%s reversal fallback could not flatten the opposing position; leaving it unchanged", contract.symbol)
        return
    if remaining_lots:
        open_side = "buy" if desired_sign == 1 else "sell"
        place_market_order(contract.symbol, remaining_lots, open_side)


def reconcile_position(
    contract: Contract, product_id: int, direction: int, supertrend: float, candle_time: int
) -> None:
    """Reconcile positions; submit close/open limits together on a trend reversal."""
    desired_sign = 1 if direction == 1 else -1
    current = get_position(product_id) if LIVE_TRADING else PAPER_POSITIONS.get(contract.symbol, 0)
    sync_supertrend_stop(contract, product_id, current, direction, supertrend, candle_time)
    previous = LAST_KNOWN_POSITIONS.setdefault(contract.symbol, current)
    if current != previous:
        instrument = contract.symbol.removesuffix("USD")
        trend = "green" if direction == 1 else "red"
        if previous == 0 and current != 0:
            position_side = "long" if current > 0 else "short"
            log_trade_event(f"{instrument} {position_side} opened at {abs(current)} lots because SuperTrend was {trend}")
        elif current == 0 and previous != 0:
            position_side = "long" if previous > 0 else "short"
            log_trade_event(f"{instrument} {position_side} closed because SuperTrend was {trend}")
        elif previous * current < 0:
            position_side = "long" if current > 0 else "short"
            log_trade_event(f"{instrument} reversed to {position_side} because SuperTrend was {trend}")
        else:
            position_side = "long" if current > 0 else "short"
            log_trade_event(f"{instrument} {position_side} size changed to {abs(current)} lots while SuperTrend was {trend}")
        LAST_KNOWN_POSITIONS[contract.symbol] = current
    existing_orders = get_strategy_open_orders(product_id)
    cooldown_candle = STOP_REENTRY_AFTER_CANDLE.get(contract.symbol)
    if current == 0 and cooldown_candle is not None:
        # Clear any still-pending entry/reversal makers so none can re-enter
        # before the cooldown candle has fully closed.
        for order in existing_orders:
            cancel_order(product_id, order)
        if candle_time < cooldown_candle:
            return
        STOP_REENTRY_AFTER_CANDLE.pop(contract.symbol, None)
    active_order_keys = {
        str(order.get("id") or order.get("client_order_id") or "")
        for order in existing_orders
    }
    for order_key in list(ORDER_REPRICE_ATTEMPTS):
        if order_key not in active_order_keys:
            ORDER_REPRICE_ATTEMPTS.pop(order_key, None)
    current_sign = 0 if current == 0 else (1 if current > 0 else -1)

    if current_sign and current_sign != desired_sign:
        close_side = "sell" if current > 0 else "buy"
        open_side = "buy" if desired_sign == 1 else "sell"
        close_order = next((order for order in existing_orders
                            if order.get("side") == close_side and bool(order.get("reduce_only"))), None)
        open_order = next((order for order in existing_orders
                           if order.get("side") == open_side and not bool(order.get("reduce_only"))), None)
        keep_ids = {
            order.get("id") for order in (close_order, open_order) if order is not None
        }
        for order in existing_orders:
            if order.get("id") not in keep_ids:
                cancel_order(product_id, order)

        reversal_orders = [order for order in (close_order, open_order) if order is not None]
        expired = [order for order in reversal_orders
                   if (order_age_seconds(order) or 0) >= MAKER_TIMEOUT_SECONDS]
        if expired and LIVE_TRADING:
            log.warning("%s reversal maker order timed out; cancelling the pair before market reconciliation",
                        contract.symbol)
            market_fallback_for_reversal(contract, product_id, direction, reversal_orders)
            return
        for order in reversal_orders:
            manage_pending_maker_order(contract, product_id, direction, order)

        trend = "green" if direction == 1 else "red"
        instrument = contract.symbol.removesuffix("USD")
        if close_order is None:
            log.info("%s SuperTrend is %s; place reduce-only close limit %s lots=%d",
                     contract.symbol, trend, close_side, abs(current))
            place_post_only_limit_order(contract.symbol, abs(current), close_side, reduce_only=True)
            log_trade_event(f"{instrument} close order pending because SuperTrend turned {trend}")
        if open_order is None:
            log.info("%s SuperTrend is %s; place new-side open limit %s lots=%d at the same time as close",
                     contract.symbol, trend, open_side, contract.size)
            place_post_only_limit_order(contract.symbol, contract.size, open_side, reduce_only=False)
            position_side = "long" if desired_sign == 1 else "short"
            log_trade_event(f"{instrument} {position_side} order pending alongside close because SuperTrend turned {trend}")
        if not LIVE_TRADING:
            PAPER_POSITIONS[contract.symbol] = desired_sign * contract.size
        return

    fully_aligned = current_sign == desired_sign and abs(current) >= contract.size
    if fully_aligned:
        for order in existing_orders:
            cancel_order(product_id, order)
        return

    side = "buy" if desired_sign == 1 else "sell"
    lots = contract.size - abs(current) if current_sign == desired_sign else contract.size
    reduce_only = False
    action = "open"

    matching = [
        order for order in existing_orders
        if order.get("side") == side
        and bool(order.get("reduce_only")) == reduce_only
        and (reduce_only or int(order.get("unfilled_size") or order.get("size", 0)) == lots)
    ]
    keep_id = matching[0].get("id") if matching else None
    for order in existing_orders:
        if order.get("id") != keep_id:
            cancel_order(product_id, order)
    if keep_id is not None:
        pending_order = next(order for order in matching if order.get("id") == keep_id)
        manage_pending_maker_order(contract, product_id, direction, pending_order)
        return

    log.info("%s SuperTrend is %s; place maker %s order %s lots=%d",
             contract.symbol, "green" if direction == 1 else "red", action, side, lots)
    place_post_only_limit_order(contract.symbol, lots, side, reduce_only=reduce_only)
    instrument = contract.symbol.removesuffix("USD")
    trend = "green" if direction == 1 else "red"
    if action == "open":
        position_side = "long" if direction == 1 else "short"
        log_trade_event(f"{instrument} {position_side} order pending at {lots} lots because SuperTrend was {trend}")
    else:
        position_side = "long" if current > 0 else "short"
        log_trade_event(f"{instrument} {position_side} close order pending because SuperTrend was {trend}")
    if not LIVE_TRADING:
        PAPER_POSITIONS[contract.symbol] = 0 if reduce_only else desired_sign * contract.size


def main() -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = False
    contracts: list[Contract] = []
    product_ids: dict[str, int] = {}
    last_processed: dict[str, int] = {}
    retry_after: dict[str, float] = {}
    retry_delay: dict[str, int] = {}

    def wait_or_stop(seconds: int) -> None:
        deadline = time.monotonic() + seconds
        while not STOP_REQUESTED and time.monotonic() < deadline:
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))

    def report_status(status: str, message: str) -> None:
        log.info("BOT_STATUS|%s|%s", status, message.replace("|", "/").replace("\n", " ")[:240])

    def request_stop(signum: int, frame: Any) -> None:
        global STOP_REQUESTED
        del signum, frame
        STOP_REQUESTED = True

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_stop)
    try:
        # Startup errors keep the bot process alive but paused. It retries with
        # a bounded delay, so an API or configuration error cannot submit
        # orders, and a temporary Delta outage doesn't end the strategy.
        startup_delay = 5
        while not STOP_REQUESTED:
            try:
                if LIVE_TRADING and (not API_KEY or not API_SECRET):
                    raise RuntimeError("Live trading requires DELTA_API_KEY and DELTA_API_SECRET in .env")
                contracts = configured_contracts()
                if LIVE_TRADING:
                    log.warning("LIVE TRADING ENABLED. This script can submit real futures orders.")
                else:
                    log.info("Paper mode: no orders will be sent. Set ENABLE_LIVE_TRADING=true to enable live orders.")

                # Product lookups are independent. Resolve selected contracts
                # concurrently so startup doesn't wait through one network
                # timeout after another when several segments are selected.
                with ThreadPoolExecutor(max_workers=min(6, len(contracts))) as pool:
                    resolved_ids = list(pool.map(get_product_id, [c.symbol for c in contracts]))
                product_ids = {contract.symbol: product_id
                               for contract, product_id in zip(contracts, resolved_ids)}
                report_status("OK", "Connected; monitoring selected contracts")
                break
            except Exception as exc:
                log.exception("Strategy startup problem; keeping process alive")
                report_status("RETRYING", str(exc))
                log.warning("Strategy remains active but paused; retrying startup in %d seconds", startup_delay)
                wait_or_stop(startup_delay)
                startup_delay = min(startup_delay * 2, 60)

        while not STOP_REQUESTED:
            for contract in contracts:
                if time.monotonic() < retry_after.get(contract.symbol, 0):
                    continue
                try:
                    candles = add_supertrend(get_mark_candles(contract.symbol))
                    latest = candles.iloc[-1]
                    candle_time = int(latest["time"])
                    # Reconcile each poll so maker-order fills and open orders are
                    # tracked; only emit the candle summary once per completed bar.
                    reconcile_position(contract, product_ids[contract.symbol],
                                       int(latest["direction"]), float(latest["supertrend"]), candle_time)
                    had_error = contract.symbol in retry_delay
                    retry_delay.pop(contract.symbol, None)
                    retry_after.pop(contract.symbol, None)
                    if had_error:
                        report_status("OK", f"Recovered; monitoring {contract.symbol}")
                    if last_processed.get(contract.symbol) != candle_time:
                        last_processed[contract.symbol] = candle_time
                        log.info("%s close=%s supertrend=%s -> %s", contract.symbol,
                                 latest["close"], latest["supertrend"],
                                 "LONG (green)" if int(latest["direction"]) == 1 else "SHORT (red)")
                except KeyboardInterrupt:
                    STOP_REQUESTED = True
                    break
                except Exception as exc:
                    log.exception("Problem processing %s", contract.symbol)
                    delay = min(retry_delay.get(contract.symbol, 1) * 2, 60)
                    retry_delay[contract.symbol] = delay
                    retry_after[contract.symbol] = time.monotonic() + delay
                    report_status("RETRYING", f"{contract.symbol}: {exc}")
                    log.warning("Keeping strategy process running; retrying %s in %d seconds", contract.symbol, delay)
            time.sleep(POLL_SECONDS)
    finally:
        if LIVE_TRADING:
            def cancel_contract_orders(contract: Contract) -> None:
                try:
                    for order in get_strategy_open_orders(product_ids[contract.symbol]):
                        cancel_order(product_ids[contract.symbol], order)
                except Exception:
                    log.exception("Could not cancel pending strategy orders for %s during shutdown", contract.symbol)

            if contracts:
                with ThreadPoolExecutor(max_workers=min(6, len(contracts))) as pool:
                    list(pool.map(cancel_contract_orders, contracts))
        report_status("STOPPED", "Stopped by request; open positions were left as-is")
        log.info("Strategy stopped; open positions were left as-is")


if __name__ == "__main__":
    # Streamlit Cloud may still be configured with this file as its entry point.
    # Route that invocation to the UI; the UI starts this file again as a plain
    # Python child process when the user presses Start.
    if "streamlit" in sys.modules:
        from streamlit_app import run

        run()
    else:
        main()
