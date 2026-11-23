"""Streamlit Cloud dashboard for the Delta SuperTrend strategy."""

from __future__ import annotations

import ipaddress
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
BOT_PATH = ROOT / "supertrend_delta_bot.py"
PID_PATH = Path("/tmp/delta_supertrend_bot.pid")
LOG_PATH = Path("/tmp/delta_supertrend_bot.log")
TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h")
TRADE_SYMBOLS = ("ETHUSD", "SOLUSD", "BNBUSD", "SNDKBUSD", "CROSSUSD")
POSITIONS_SYMBOLS = ("BTCUSD",) + TRADE_SYMBOLS
SECRETS_CONFIG_ERROR: str | None = None


def _load_secrets_into_environment() -> None:
    """Accept Streamlit Cloud secrets, while still supporting a local .env."""
    global SECRETS_CONFIG_ERROR
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
        try:
            value = secrets.get(secret_name)
        except Exception:
            # Do not expose secret values or parser details in the dashboard.
            SECRETS_CONFIG_ERROR = "Streamlit Secrets could not be read. Check TOML syntax and put each value on the same line as its name."
            value = None
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


def _start(timeframe: str, segment: str, lots_per_segment: int) -> None:
    if not bot.API_KEY or not bot.API_SECRET:
        raise RuntimeError("Set DELTA_API_KEY and DELTA_API_SECRET in Streamlit Cloud app secrets.")
    if _read_pid():
        raise RuntimeError("A strategy process is already running.")
    env = os.environ.copy()
    env.update({
        "TIMEFRAME": timeframe,
        "TRADE_SYMBOLS": segment,
        "LOTS_PER_SEGMENT": str(lots_per_segment),
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


def _live_data() -> tuple[list[dict[str, Any]], str | None, str | None, bool]:
    rows: list[dict[str, Any]] = []
    error: str | None = None
    delta_ip: str | None = None
    api_authenticated = False
    if not bot.API_KEY or not bot.API_SECRET:
        return rows, "Add Delta API credentials to Streamlit app secrets.", None, False
    try:
        products = {symbol: bot.get_product_id(symbol) for symbol in POSITIONS_SYMBOLS}
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
            api_authenticated = True
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
        client_ip_match = re.search(r"['\"]client_ip['\"]\s*:\s*['\"]([^'\"]+)", error)
        if client_ip_match:
            delta_ip = client_ip_match.group(1)
        else:
            plain_ip_match = re.search(r"Your IP:\s*([0-9a-fA-F:.]+)", error)
            if plain_ip_match:
                delta_ip = plain_ip_match.group(1)
    return rows, error, delta_ip, api_authenticated


@st.cache_data(ttl=10, show_spinner=False)
def _public_egress_ips() -> tuple[str, str]:
    services = {
        4: ("https://api.ipify.org", "https://checkip.amazonaws.com", "https://ipv4.icanhazip.com"),
        6: ("https://api6.ipify.org", "https://ipv6.icanhazip.com"),
    }
    values: list[str] = []
    for version in (4, 6):
        value = "Unavailable"
        for url in services[version]:
            try:
                response = bot.SESSION.get(url, timeout=3)
                response.raise_for_status()
                candidate = response.text.strip()
                if ipaddress.ip_address(candidate).version == version:
                    value = candidate
                    break
            except Exception:
                continue
        values.append(value)
    return values[0], values[1]


def run() -> None:
    st.set_page_config(page_title="Delta SuperTrend", layout="wide")
    st.markdown("""
    <style>
    .stApp { background: #f4c3dc; color: #62384f; }
    [data-testid="stHeader"] { background: rgba(246,199,223,.92); }
    [data-testid="stSidebar"] { display: none; }
    .block-container { max-width: 1220px; padding-top: 2rem; }
    [data-testid="stVerticalBlockBorderWrapper"] { background: #fff8fc;
      border: 1px solid #edb2ce; border-radius: 15px; padding: 18px; }
    [data-testid="stMetric"] { background: #fff8fc; border: 0; padding: 4px; }
    [data-testid="stMetricLabel"] { color: #83536e; }
    [data-testid="stMetricValue"] { color: #583247; }
    div.stButton > button { background: #008b69; color: white; border: 0; font-weight: 700; }
    div.stButton > button:hover { background: #007454; color: white; }
    div.stButton > button:disabled { background: #df9cb7; color: white; }
    </style>
    """, unsafe_allow_html=True)

    rows, error, delta_ip, api_authenticated = _live_data()
    public_ipv4, public_ipv6 = _public_egress_ips()

    title_col, api_col = st.columns([4, 1])
    with title_col:
        st.title("Delta SuperTrend")
        st.caption("ETHUSD / SOLUSD / BNBUSD / SNDKBUSD / CROSSUSD perpetuals | SuperTrend 15, 3 | Delta mark-price candles")
    with api_col:
        if api_authenticated:
            label, color = "CONNECTED", "#008b69"
        elif not bot.API_KEY or not bot.API_SECRET:
            label, color = "DISCONNECTED", "#b64f66"
        else:
            label, color = "API ERROR", "#b64f66"
        st.markdown(f"<div style='text-align:center;background:#fff8fc;border:1px solid #e9a6c7;border-radius:14px;padding:10px'><small>Delta API key</small><br><b style='color:{color}'>{label}</b></div>", unsafe_allow_html=True)
    delta_ip_note = (
        f"<span><b>Delta saw:</b> <code>{delta_ip}</code></span>"
        if delta_ip else ""
    )
    st.markdown(
        "<div style='margin:5px 0 15px;background:#fff8fc;border:1px solid #edb2ce;"
        "border-radius:10px;padding:8px 12px;display:flex;flex-wrap:wrap;gap:8px 22px;"
        "align-items:center;font-size:.9rem'>"
        f"<b>Whitelist IPs</b><span>IPv4: <code>{public_ipv4}</code></span>"
        f"<span>IPv6: <code>{public_ipv6}</code></span>{delta_ip_note}</div>",
        unsafe_allow_html=True,
    )
    if SECRETS_CONFIG_ERROR and (not bot.API_KEY or not bot.API_SECRET):
        st.warning(SECRETS_CONFIG_ERROR)

    left, right = st.columns([1, 1.35])
    with left:
        with st.container(border=True):
            st.subheader("Strategy status")
            running = _read_pid() is not None
            state_color = "#008b69" if running else "#986078"
            st.markdown(f"<h3 style='margin-top:0;color:{state_color}'>{'RUNNING' if running else 'STOPPED'}</h3>", unsafe_allow_html=True)
            st.caption("Live order placement is enabled when you start the strategy.")
            timeframe = st.selectbox("Timeframe", TIMEFRAMES, index=0)
            if "selected_trade_symbols" not in st.session_state:
                configured = [s.strip().upper() for s in os.getenv("TRADE_SYMBOLS", ",".join(TRADE_SYMBOLS)).split(",")]
                st.session_state.selected_trade_symbols = [s for s in TRADE_SYMBOLS if s in configured]
            for symbol in TRADE_SYMBOLS:
                key = f"segment_{symbol}"
                if key not in st.session_state:
                    st.session_state[key] = symbol in st.session_state.selected_trade_symbols
            if "select_all_segments" not in st.session_state:
                st.session_state.select_all_segments = all(st.session_state[f"segment_{s}"] for s in TRADE_SYMBOLS)

            def sync_all_segments() -> None:
                for selected_symbol in TRADE_SYMBOLS:
                    st.session_state[f"segment_{selected_symbol}"] = st.session_state.select_all_segments

            def sync_master_state() -> None:
                st.session_state.select_all_segments = all(
                    st.session_state[f"segment_{selected_symbol}"] for selected_symbol in TRADE_SYMBOLS
                )

            st.caption("Trading segments")
            st.checkbox("Select all", key="select_all_segments", disabled=running, on_change=sync_all_segments)
            segment_cols = st.columns(len(TRADE_SYMBOLS))
            for index, symbol in enumerate(TRADE_SYMBOLS):
                segment_cols[index].checkbox(symbol, key=f"segment_{symbol}", disabled=running, on_change=sync_master_state)
            active_symbols = [s for s in TRADE_SYMBOLS if st.session_state[f"segment_{s}"]]
            lots_per_segment = st.number_input(
                "Lots for each selected segment", min_value=1, max_value=10000,
                value=max(1, int(os.getenv("LOTS_PER_SEGMENT", "1"))), step=1, disabled=running,
            )
            button_cols = st.columns(2)
            if button_cols[0].button("Start live strategy", disabled=running, use_container_width=True):
                try:
                    if not active_symbols:
                        raise RuntimeError("Select at least one trading segment.")
                    _start(timeframe, ",".join(active_symbols), int(lots_per_segment))
                    st.success("Live strategy started.")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
            if button_cols[1].button("Stop strategy", disabled=not running, use_container_width=True):
                _stop()
                st.rerun()
            st.caption("Unfilled maker orders switch to market after a fixed 10 seconds. Stop leaves open positions as-is.")

    with right:
        with st.container(border=True):
            st.subheader("Current Delta positions and mark prices")
            if error:
                st.error(f"Delta data unavailable: {error}")
            for row in rows:
                cols = st.columns([1.1, 1.1, 1.2, 1.4, 1])
                cols[0].markdown(f"**{row['symbol']}**")
                cols[1].markdown("LONG" if row["position"] > 0 else "SHORT" if row["position"] < 0 else "FLAT")
                cols[2].caption(f"Mark price: {float(row['mark']):.2f}" if row["mark"] is not None else "Mark price: N/A")
                pnl_text = str(row["pnl"]) if row["pnl"] is not None else "N/A"
                cols[3].caption(f"Running P&L: {pnl_text} | SL: {row['stop'] or 'Not set'}")
                cols[4].markdown(f"**{abs(row['position'])} lots**")
                pending = row["pending"]
                if pending:
                    st.caption(f"{row['symbol']} pending: {pending.get('side', '').upper()} | {pending.get('unfilled_size', pending.get('size', 0))} lots | limit {pending.get('limit_price', 'N/A')}")

    with st.container(border=True):
        st.subheader("Recent bot activity")
        st.write(_latest_activity())

    time.sleep(2)
    st.rerun()


if __name__ == "__main__":
    run()
