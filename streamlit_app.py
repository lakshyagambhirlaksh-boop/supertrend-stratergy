"""Streamlit Cloud dashboard for the Delta SuperTrend strategy."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import streamlit as st


ROOT = Path(__file__).resolve().parent
BOT_PATH = ROOT / "supertrend_delta_bot.py"
PID_PATH = Path("/tmp/delta_supertrend_bot.pid")
LOG_PATH = Path("/tmp/delta_supertrend_bot.log")
TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h")
SYMBOLS = {"BTC only": "BTCUSD", "ETH only": "ETHUSD", "BTC + ETH": "BTCUSD,ETHUSD"}


def _load_secrets_into_environment() -> None:
    """Accept Streamlit Cloud secrets, while still supporting a local .env."""
    try:
        secrets = st.secrets
    except Exception:
        return
    mapping = {
        "DELTA_API_KEY": "DELTA_API_KEY",
        "DELTA_API_SECRET": "DELTA_API_SECRET",
        "DELTA_API_BASE": "DELTA_API_BASE",
    }
    for env_name, secret_name in mapping.items():
        value = secrets.get(secret_name)
        if value:
            os.environ[env_name] = str(value)
    # Cloud deployment is explicitly live-capable; the bot itself still starts
    # only after the user presses the Start button in this page.
    os.environ["ENABLE_LIVE_TRADING"] = "true"


_load_secrets_into_environment()

import supertrend_delta_bot as bot  # noqa: E402


def _read_pid() -> int | None:
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        # Guard against stale PID files and a PID reused by another Cloud process.
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        if b"supertrend_delta_bot.py" not in command:
            PID_PATH.unlink(missing_ok=True)
            return None
        return pid
    except (OSError, ValueError):
        PID_PATH.unlink(missing_ok=True)
        return None


def _start(timeframe: str, segment: str, btc_lots: int, eth_lots: int) -> None:
    if not bot.API_KEY or not bot.API_SECRET:
        raise RuntimeError("Set DELTA_API_KEY and DELTA_API_SECRET in Streamlit Cloud app secrets.")
    if _read_pid():
        raise RuntimeError("A strategy process is already running.")
    env = os.environ.copy()
    env.update({
        "TIMEFRAME": timeframe,
        "TRADE_SYMBOLS": segment,
        "BTC_SIZE": str(btc_lots),
        "ETH_SIZE": str(eth_lots),
        "ENABLE_LIVE_TRADING": "true",
    })
    log_file = LOG_PATH.open("a", encoding="utf-8")
    try:
        child = subprocess.Popen(
            [sys.executable, "-u", str(BOT_PATH)],
            cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
            stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_file.close()
    PID_PATH.write_text(str(child.pid), encoding="utf-8")


def _stop() -> None:
    pid = _read_pid()
    if pid is None:
        return
    os.kill(pid, signal.SIGTERM)
    # Bot handles SIGTERM, cancels its pending strategy orders, and leaves
    # existing positions open on Delta.


def _latest_activity() -> str:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "No bot activity yet."
    for line in reversed(lines):
        if "TRADE_EVENT|" in line:
            try:
                event = line.split("TRADE_EVENT|", 1)[1]
                clock, message = event.split("|", 1)
                return f"{message.strip()} · {clock}"
            except ValueError:
                return line[-180:]
    return "No trade action yet."


def _live_data() -> tuple[list[dict[str, Any]], str | None, str | None]:
    rows: list[dict[str, Any]] = []
    error: str | None = None
    delta_ip: str | None = None
    if not bot.API_KEY or not bot.API_SECRET:
        return rows, "Add Delta API credentials to Streamlit app secrets.", None
    try:
        products = {symbol: bot.get_product_id(symbol) for symbol in ("BTCUSD", "ETHUSD")}
        margined: dict[str, dict[str, Any]] = {}
        try:
            payload = bot.signed_request("GET", "/v2/positions/margined", {
                "product_ids": ",".join(str(value) for value in products.values()),
            })
            margined = {str(p.get("product_symbol")): p for p in payload.get("result", [])}
        except Exception:
            pass
        for symbol, product_id in products.items():
            position = bot.get_position(product_id)
            ticker_response = bot.SESSION.get(f"{bot.API_BASE}/v2/tickers/{symbol}", timeout=8)
            ticker_response.raise_for_status()
            ticker_data = ticker_response.json()
            if not ticker_data.get("success"):
                raise RuntimeError(f"Delta ticker API error: {ticker_data}")
            ticker = ticker_data.get("result") or {}
            orders_data = bot.signed_request("GET", "/v2/orders", {
                "product_ids": product_id, "states": "open,pending",
            })
            orders = orders_data.get("result", [])
            pending = next((o for o in orders if str(o.get("client_order_id", "")).startswith("stbot-")), None)
            stop_loss = next((o.get("bracket_stop_loss_price") or o.get("stop_price")
                              for o in orders
                              if o.get("stop_order_type") == "stop_loss_order"
                              or o.get("bracket_stop_loss_price")), None)
            rows.append({
                "symbol": symbol,
                "position": position,
                "mark": ticker.get("mark_price"),
                "pnl": margined.get(symbol, {}).get("unrealized_pnl"),
                "stop": stop_loss,
                "pending": pending,
            })
    except Exception as exc:
        error = str(exc)
        # Delta sometimes returns its observed egress IP in the API error.
        marker = "client_ip"
        if marker in error:
            try:
                delta_ip = error.split(marker, 1)[1].split(":", 1)[1].split(",", 1)[0].strip(" '\"}]")
            except (IndexError, ValueError):
                pass
    return rows, error, delta_ip


def run() -> None:
    st.set_page_config(page_title="Delta SuperTrend", page_icon="📈", layout="wide")
    st.markdown("""
    <style>
    .stApp { background: #f6c7df; color: #45243a; }
    [data-testid="stHeader"] { background: rgba(246,199,223,.92); }
    [data-testid="stMetric"] { background: #fff7fb; border: 1px solid #e9a6c7;
      border-radius: 12px; padding: 14px; }
    div.stButton > button { background: #008b69; color: white; border: 0; font-weight: 700; }
    div.stButton > button:hover { background: #007454; color: white; }
    </style>
    """, unsafe_allow_html=True)

    st.title("Delta SuperTrend")
    st.caption("BTCUSD / ETHUSD perpetuals · SuperTrend length 15, factor 3 · Delta mark-price candles")

    with st.sidebar:
        st.subheader("Delta API key")
        st.caption("Credentials are read from Streamlit Cloud → App settings → Secrets.")
        st.write("CONNECTED" if bot.API_KEY and bot.API_SECRET else "DISCONNECTED")
        st.caption("Cloud egress IP for Delta API whitelist")
        try:
            ip_response = bot.SESSION.get("https://api.ipify.org", timeout=5)
            st.code(ip_response.text.strip())
        except Exception:
            st.code("Unavailable")

    left, right = st.columns([1, 1.35])
    with left:
        st.subheader("Strategy controls")
        timeframe = st.selectbox("Timeframe", TIMEFRAMES, index=0)
        segment_label = st.selectbox("Trading segment", tuple(SYMBOLS))
        active_symbols = SYMBOLS[segment_label].split(",")
        size_cols = st.columns(2)
        btc_lots = size_cols[0].number_input("BTC lots", min_value=1, value=1, step=1,
                                             disabled="BTCUSD" not in active_symbols)
        eth_lots = size_cols[1].number_input("ETH lots", min_value=1, value=1, step=1,
                                             disabled="ETHUSD" not in active_symbols)
        st.info("Signals use completed candles. Maker limits switch to market after 10 seconds if unfilled.")
        st.warning("Start submits live futures orders. Stop leaves open positions as-is.")
        status_col, control_col = st.columns(2)
        running = _read_pid() is not None
        status_col.metric("Strategy status", "RUNNING" if running else "STOPPED")
        if control_col.button("Stop strategy", disabled=not running, use_container_width=True):
            _stop()
            st.rerun()
        if st.button("Start live strategy", disabled=running, use_container_width=True):
            try:
                _start(timeframe, SYMBOLS[segment_label], int(btc_lots), int(eth_lots))
                st.success("Live strategy started.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

    with right:
        st.subheader("Current Delta positions")
        rows, error, delta_ip = _live_data()
        if error:
            st.error(f"Delta data unavailable: {error}")
            if delta_ip:
                st.caption(f"Delta-observed IP to whitelist: {delta_ip}")
        if rows:
            for row in rows:
                with st.container(border=True):
                    cols = st.columns(5)
                    cols[0].metric(row["symbol"], "LONG" if row["position"] > 0 else "SHORT" if row["position"] < 0 else "FLAT")
                    cols[1].metric("Lots", abs(row["position"]))
                    try:
                        mark = f"{float(row['mark']):.2f}"
                    except (ValueError, TypeError):
                        mark = "—"
                    cols[2].metric("Mark price", mark)
                    pnl = row["pnl"]
                    cols[3].metric("Running P&L", f"{pnl}" if pnl is not None else "—")
                    cols[4].metric("SL", f"{row['stop']}" if row["stop"] else "Not set")
                    pending = row["pending"]
                    if pending:
                        st.caption(f"Pending: {pending.get('side', '').upper()} · {pending.get('unfilled_size', pending.get('size', 0))} lots · limit {pending.get('limit_price', '—')}")
        st.subheader("Recent bot activity")
        st.write(_latest_activity())

    # Keep prices, positions and pending orders fresh without blocking the UI.
    time.sleep(2)
    st.rerun()


if __name__ == "__main__":
    run()
