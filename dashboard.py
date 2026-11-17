"""Local web dashboard for starting/stopping the Delta SuperTrend bot."""

from __future__ import annotations

import os
import re
import signal
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, request
import requests

load_dotenv()

from supertrend_delta_bot import (  # noqa: E402
    API_KEY,
    API_SECRET,
    API_BASE,
    LIVE_TRADING,
    SUPPORTED_TIMEFRAMES,
    add_supertrend,
    get_mark_candles,
    get_position,
    get_product_id,
    signed_request,
)

ROOT = Path(__file__).resolve().parent
BOT_PATH = ROOT / "supertrend_delta_bot.py"
LOG_PATH = ROOT / "strategy.log"
HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.getenv("DASHBOARD_PORT", "8765"))

app = Flask(__name__)
log = logging.getLogger("delta-supertrend-dashboard")
state_lock = threading.Lock()
bot_process: subprocess.Popen[str] | None = None
strategy_requested_running = False
strategy_environment: dict[str, str] | None = None
restart_delay_seconds = 5
restart_at = 0.0
last_bot_launch_at = 0.0
active_timeframe: str | None = None
active_segment: str | None = None
selected_timeframe = os.getenv("TIMEFRAME", "1m").strip().lower()
selected_segment = os.getenv("TRADE_SYMBOLS", "BTCUSD,ETHUSD").strip().upper()
selected_sizes = {
    "BTC_SIZE": int(os.getenv("BTC_SIZE", "1")),
    "ETH_SIZE": int(os.getenv("ETH_SIZE", "1")),
}
last_error: str | None = None
ip_cache: dict[str, Any] = {"address": None, "checked_at": 0.0, "error": None}
ip_lock = threading.Lock()
supertrend_cache_lock = threading.Lock()
supertrend_cache: dict[tuple[str, str], tuple[int, float, int]] = {}


def get_supertrend_reference(symbol: str, timeframe: str) -> tuple[float, int]:
    """Return the latest completed SuperTrend level, cached for the current bar."""
    seconds = SUPPORTED_TIMEFRAMES[timeframe]
    bar = int(time.time()) // seconds
    key = (symbol, timeframe)
    with supertrend_cache_lock:
        cached = supertrend_cache.get(key)
        if cached and cached[0] == bar:
            return cached[1], cached[2]
    frame = add_supertrend(get_mark_candles(symbol, timeframe))
    latest = frame.iloc[-1]
    value = float(latest["supertrend"])
    direction = int(latest["direction"])
    with supertrend_cache_lock:
        supertrend_cache[key] = (bar, value, direction)
    return value, direction


PAGE = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Delta SuperTrend</title>
  <style>
    :root { color-scheme: light; --bg:#f7c8df; --panel:#fff4f9; --line:#e7a9c7; --muted:#79556a; --text:#321d2b; --green:#08784f; --red:#c62848; --blue:#315fba; }
    * { box-sizing:border-box; } body { margin:0; font:15px/1.5 system-ui,Segoe UI,sans-serif; background:var(--bg); color:var(--text); }
    main { width:min(980px,94vw); margin:42px auto; } h1 { margin:0; font-size:29px; } .sub { color:var(--muted); margin:5px 0 24px; }
    .topbar { display:flex; align-items:center; justify-content:space-between; gap:18px; }
    .api-badge { flex:0 0 auto; text-align:right; background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:8px 14px; }
    .api-badge .connection-value { font-size:15px; margin-top:0; }
    .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; } .card { background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:20px; }
    .wide { grid-column:1/-1; } .label { color:var(--muted); font-size:13px; } .state { font-size:20px; font-weight:700; margin-top:5px; } .on { color:var(--green); } .off { color:var(--muted); } .error { color:var(--red); }
    .positions-card { padding:14px 18px; } .positions-card .trade-row { padding:7px 0; }
    .position-main { display:flex; flex-direction:column; gap:2px; } .market-price { color:var(--muted); font-size:12px; } .risk-detail { color:var(--muted); font-size:12px; }
    .connection-card { grid-column:1/-1; } .connection-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px; margin-top:12px; }
    .connection-value { font-weight:750; font-size:18px; margin-top:4px; overflow-wrap:anywhere; }
    .pending-order { color:#8b5a00; font-size:12px; font-weight:700; }
    select,button,input { font:inherit; border-radius:9px; padding:10px 13px; border:1px solid var(--line); color:var(--text); background:#fffafd; }
    select { min-width:150px; } input { width:100px; } button { cursor:pointer; font-weight:650; } button:disabled,select:disabled,input:disabled { opacity:.45; cursor:not-allowed; }
    .start { background:#078b60; border-color:#078b60; color:white; } .stop { background:#c62848; border-color:#c62848; color:white; margin-left:8px; }
    .trade-row { display:flex; justify-content:space-between; padding:12px 0; border-bottom:1px solid var(--line); } .trade-row:last-child { border:0; }
    .chip { border-radius:999px; padding:3px 10px; font-size:12px; font-weight:700; } .long { color:#075d40; background:#c7f3df; } .short { color:#971b35; background:#ffd7e1; } .flat { color:#5b5260; background:#e9e0e8; }
    .note { color:var(--muted); font-size:13px; margin:12px 0 0; } .activity-line { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-top:10px; color:#40283a; font-size:14px; }
    @media(max-width:650px) { .grid { grid-template-columns:1fr; } .wide,.connection-card { grid-column:auto; } .connection-grid { grid-template-columns:1fr; } .topbar { align-items:flex-start; flex-direction:column; } .api-badge { text-align:left; } }
  </style>
</head>
<body><main>
  <div class="topbar">
    <div><h1>Delta SuperTrend</h1><p class="sub">BTCUSD and ETHUSD perpetuals · SuperTrend length 15, factor 3 · mark-price candles</p></div>
    <div class="api-badge"><div class="label">Delta API key</div><div id="apiConnection" class="connection-value">Checking…</div></div>
  </div>
  <div class="grid">
    <section class="card">
      <div class="label">Strategy status</div><div id="status" class="state off">Loading…</div>
      <p id="live" class="note"></p>
      <div style="margin-top:18px">
        <label for="timeframe" class="label">Timeframe</label><br>
        <select id="timeframe"><option>1m</option><option>3m</option><option>5m</option><option>15m</option><option>30m</option><option>1h</option></select>
        <label for="segment" class="label">Trading segment</label><br>
        <select id="segment"><option value="BTCUSD">BTC only</option><option value="ETHUSD">ETH only</option><option value="BTCUSD,ETHUSD">Both BTC and ETH</option></select>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin:12px 0">
          <label id="btcLotField" class="label">BTC lots<br><input id="btcSize" type="number" min="1" max="10000" step="1" value="1"></label>
          <label id="ethLotField" class="label">ETH lots<br><input id="ethSize" type="number" min="1" max="10000" step="1" value="1"></label>
        </div>
        <button id="start" class="start" onclick="startBot()">Start live strategy</button>
        <button id="stop" class="stop" onclick="stopBot()">Stop strategy</button>
      </div>
      <p class="note">On errors, the strategy stays running and retries; it pauses order placement while recovering. Stop cancels pending bot orders and leaves open positions as they are.</p>
    </section>
    <section class="card positions-card">
      <div class="label">Current Delta positions and mark prices</div>
      <div id="positions"><p class="note">Loading positions…</p></div>
    </section>
    <section class="card connection-card">
      <div class="connection-grid">
        <div><div class="label">Public IP address used for Delta</div><div id="publicIp" class="connection-value">Checking…</div></div>
      </div>
      <p class="note">If your Delta API key has an IP whitelist, add this public IP address there.</p>
    </section>
    <section class="card wide">
      <div class="label">Recent bot activity</div>
      <div id="logs" class="activity-line">No trade activity yet.</div>
    </section>
  </div>
</main>
<script>
const $ = id => document.getElementById(id);
let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const r = await fetch('/api/status'); const d = await r.json();
    const healthSuffix = d.bot_health === 'RETRYING' ? ' · RETRYING' : d.bot_health === 'RESTARTING' ? ' · RESTARTING' : ` · ${d.timeframe}`;
    $('status').textContent = d.running ? `RUNNING${healthSuffix}` : 'STOPPED';
    $('status').className = `state ${d.running ? 'on' : 'off'}`;
    $('live').textContent = d.running && (d.bot_health === 'RETRYING' || d.bot_health === 'RESTARTING')
      ? `Bot remains active and will retry; trading is paused during recovery: ${d.bot_message || 'retrying'}`
      : d.live_enabled ? 'Live order placement is enabled.' : 'Live order placement is disabled in .env.';
    $('live').className = `note ${d.live_enabled ? 'error' : ''}`;
    $('start').disabled = d.running || !d.live_enabled;
    $('stop').disabled = !d.running;
    $('timeframe').disabled = d.running;
    $('segment').disabled = d.running;
    $('btcSize').disabled = d.running;
    $('ethSize').disabled = d.running;
    if (!d.running) { $('btcSize').value = d.sizes.BTC_SIZE; $('ethSize').value = d.sizes.ETH_SIZE; }
    if (d.timeframe) $('timeframe').value = d.timeframe;
    if (d.segment) $('segment').value = d.segment;
    updateLotVisibility();
    if (d.positions_error) $('positions').innerHTML = `<p class="error">Could not read Delta positions: ${escapeHtml(d.positions_error)}</p>`;
    else $('positions').innerHTML = d.positions.map(p => {
      const pending = p.pending_order;
      const pendingText = pending ? `<small class="pending-order">ORDER PENDING · ${escapeHtml(pending.side.toUpperCase())} ${pending.remaining_lots} lots @ ${formatPrice(pending.limit_price)}</small>` : '';
      const pnlText = p.size ? formatPnl(p.unrealized_pnl) : 'N/A';
      const supertrendStop = p.size ? `${formatPrice(p.supertrend_sl)} USD (${p.supertrend_direction === 1 ? 'green' : 'red'})` : 'Not active (flat)';
      const exchangeStop = p.stop_loss === 'unavailable' ? 'unavailable' : p.stop_loss ? `${formatPrice(p.stop_loss)} USD` : 'Not set';
      return `<div class="trade-row"><span class="position-main"><b>${p.symbol}</b><small class="market-price">Mark price: ${p.mark_price ? formatPrice(p.mark_price) + ' USD' : 'unavailable'}</small><small class="risk-detail">Running P&amp;L: ${pnlText} | SL (SuperTrend): ${supertrendStop} | Exchange SL: ${exchangeStop}</small>${pendingText}</span><span class="chip ${p.size > 0 ? 'long' : p.size < 0 ? 'short' : 'flat'}">${p.size > 0 ? 'LONG' : p.size < 0 ? 'SHORT' : 'FLAT'} · ${Math.abs(p.size)} lots</span></div>`;
    }).join('');
    $('apiConnection').textContent = d.positions_error ? 'DISCONNECTED' : 'CONNECTED';
    $('apiConnection').className = `connection-value ${d.positions_error ? 'error' : 'on'}`;
    $('publicIp').textContent = d.public_ip || 'Unavailable';
    $('publicIp').className = `connection-value ${d.public_ip ? '' : 'error'}`;
    $('logs').textContent = d.activity || 'No trade activity yet.';
    $('logs').title = $('logs').textContent;
  } catch (e) { $('status').textContent = 'Dashboard connection error'; $('status').className='state error'; }
  finally { refreshing = false; }
}
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function formatPrice(value) { const price = Number(value); return Number.isFinite(price) ? price.toFixed(2) : 'unavailable'; }
function formatPnl(value) { const pnl = Number(value); return Number.isFinite(pnl) ? `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}` : 'unavailable'; }
function updateLotVisibility() {
  const segment = $('segment').value;
  $('btcLotField').style.display = segment === 'ETHUSD' ? 'none' : 'block';
  $('ethLotField').style.display = segment === 'BTCUSD' ? 'none' : 'block';
}
async function startBot() {
  const timeframe = $('timeframe').value;
  const segment = $('segment').value;
  const btcSize = Number($('btcSize').value), ethSize = Number($('ethSize').value);
  if (!Number.isInteger(btcSize) || btcSize < 1 || btcSize > 10000 || !Number.isInteger(ethSize) || ethSize < 1 || ethSize > 10000) { alert('Lot sizes must be whole numbers from 1 to 10000.'); return; }
  const sizeText = segment === 'BTCUSD' ? `BTC ${btcSize} lots` : segment === 'ETHUSD' ? `ETH ${ethSize} lots` : `BTC ${btcSize} lots and ETH ${ethSize} lots`;
  if (!confirm(`Start LIVE trading on ${segment === 'BTCUSD,ETHUSD' ? 'BTC and ETH' : segment.replace('USD','')} using ${timeframe}? Size: ${sizeText}. Unfilled maker orders switch to market after a fixed 10 seconds.`)) return;
  const r = await fetch('/api/start', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({timeframe,segment,btc_size:btcSize,eth_size:ethSize})});
  const d = await r.json(); if (!r.ok) alert(d.error || 'Could not start strategy.'); refresh();
}
async function stopBot() {
  if (!confirm('Stop the strategy and cancel its pending orders? Any open Delta positions will remain open.')) return;
  const r = await fetch('/api/stop', {method:'POST'}); const d = await r.json(); if (!r.ok) alert(d.error || 'Could not stop strategy.'); refresh();
}
async function rememberTimeframe() {
  await saveSettings();
}
async function saveSettings() {
  const r = await fetch('/api/settings', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({timeframe:$('timeframe').value,segment:$('segment').value,btc_size:Number($('btcSize').value),eth_size:Number($('ethSize').value)})});
  if (!r.ok) { const d=await r.json(); alert(d.error || 'Could not save settings.'); }
}
$("timeframe").addEventListener('change', rememberTimeframe);
$("segment").addEventListener('change', () => { updateLotVisibility(); saveSettings(); });
$("btcSize").addEventListener('change', saveSettings);
$("ethSize").addEventListener('change', saveSettings);
refresh(); setInterval(refresh, 2000);
</script>
</body></html>'''


def process_is_running() -> bool:
    return bot_process is not None and bot_process.poll() is None


def read_bot_status() -> tuple[str, str]:
    if not LOG_PATH.exists():
        return "STARTING", "Waiting for the bot process."
    try:
        for line in reversed(LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()):
            marker = "BOT_STATUS|"
            if marker in line:
                parts = line.split(marker, 1)[1].split("|", 1)
                if len(parts) == 2:
                    return parts[0], parts[1]
    except OSError as exc:
        return "RETRYING", f"Could not read bot status: {exc}"
    return "STARTING", "Waiting for the bot process."


def launch_bot(environment: dict[str, str]) -> subprocess.Popen[str]:
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        return subprocess.Popen(
            [sys.executable, "-u", str(BOT_PATH)],
            cwd=str(ROOT),
            env=environment,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )


def bot_watchdog() -> None:
    """Restart an unexpectedly exited bot until the dashboard Stop button is used."""
    global bot_process, restart_delay_seconds, restart_at, last_bot_launch_at, last_error
    while True:
        time.sleep(1)
        with state_lock:
            if not strategy_requested_running:
                continue
            now = time.monotonic()
            if process_is_running():
                continue
            if bot_process is not None:
                exit_code = bot_process.returncode
                runtime = now - last_bot_launch_at
                bot_process = None
                restart_delay_seconds = 5 if runtime >= 60 else min(max(restart_delay_seconds * 2, 5), 60)
                restart_at = now + restart_delay_seconds
                last_error = f"Bot process exited with code {exit_code}; restarting automatically."
                log.error("%s Next restart in %d seconds.", last_error, restart_delay_seconds)
            if strategy_environment is None or now < restart_at:
                continue
            try:
                bot_process = launch_bot(strategy_environment)
                last_bot_launch_at = now
                restart_at = 0.0
                log.warning("Restarted strategy process automatically; requested running state remains on.")
            except OSError as exc:
                last_error = f"Could not restart bot process: {exc}"
                restart_delay_seconds = min(max(restart_delay_seconds * 2, 5), 60)
                restart_at = now + restart_delay_seconds
                log.exception("%s Retrying in %d seconds.", last_error, restart_delay_seconds)


def read_recent_activity() -> str:
    if not LOG_PATH.exists():
        return ""
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            marker = "TRADE_EVENT|"
            if marker in line:
                event = line.split(marker, 1)[1].split("|", 1)
                if len(event) == 2:
                    return f"{event[0]} — {event[1]}"
        return ""
    except OSError as exc:
        return f"Could not read trade activity: {exc}"


def persist_settings(values: dict[str, str]) -> None:
    """Update only the requested .env values, preserving the credential lines."""
    env_path = ROOT / ".env"
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    for key, value in values.items():
        pattern = re.compile(rf"(?m)^{re.escape(key)}=.*$")
        if pattern.search(text):
            text = pattern.sub(f"{key}={value}", text)
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += f"{key}={value}\n"
    env_path.write_text(text, encoding="utf-8")


def get_public_ip() -> tuple[str | None, str | None]:
    """Return the server's current public egress IP, caching the lookup for a minute."""
    with ip_lock:
        if time.monotonic() - ip_cache["checked_at"] < 60:
            return ip_cache["address"], ip_cache["error"]
        try:
            response = requests.get("https://api.ipify.org", timeout=5)
            response.raise_for_status()
            address = response.text.strip()
            if not address:
                raise ValueError("IP lookup returned an empty response")
            ip_cache.update(address=address, checked_at=time.monotonic(), error=None)
        except Exception as exc:
            ip_cache.update(address=None, checked_at=time.monotonic(), error=str(exc))
        return ip_cache["address"], ip_cache["error"]


def get_mark_price(symbol: str) -> str | None:
    response = requests.get(f"{API_BASE}/v2/tickers/{symbol}", timeout=5)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"Delta ticker API error: {payload}")
    ticker = payload.get("result") or {}
    price = ticker.get("mark_price")
    return str(price) if price is not None else None


def get_margined_positions(product_ids: list[int]) -> dict[str, dict[str, Any]]:
    data = signed_request("GET", "/v2/positions/margined", {
        "product_ids": ",".join(str(product_id) for product_id in product_ids),
    })
    return {
        str(position.get("product_symbol")): position
        for position in data.get("result", [])
    }


def get_open_orders(product_id: int) -> list[dict[str, Any]]:
    data = signed_request("GET", "/v2/orders", {
        "product_ids": product_id,
        "states": "open,pending",
    })
    return data.get("result", [])


@app.get("/")
def index():
    return PAGE


@app.get("/api/status")
def status():
    global last_error
    with state_lock:
        process_alive = process_is_running()
        running = strategy_requested_running or process_alive
        bot_health, bot_message = read_bot_status()
        if running and not process_alive:
            bot_health = "RESTARTING"
            bot_message = last_error or "Bot process exited; waiting for automatic restart."
        elif running and bot_health == "STOPPED":
            bot_health = "STARTING"
            bot_message = "Starting the strategy process."
        if running and bot_health == "OK":
            last_error = None

    positions: list[dict[str, Any]] = []
    positions_error = None
    strategy_timeframe = active_timeframe if running and active_timeframe else selected_timeframe
    if API_KEY and API_SECRET:
        try:
            symbols = ("BTCUSD", "ETHUSD")
            product_ids = {symbol: get_product_id(symbol) for symbol in symbols}
            try:
                margined = get_margined_positions(list(product_ids.values()))
            except Exception:
                margined = {}
            for symbol in symbols:
                product_id = product_ids[symbol]
                position_size = get_position(product_id)
                supertrend_sl = None
                supertrend_direction = None
                if position_size:
                    try:
                        supertrend_sl, supertrend_direction = get_supertrend_reference(symbol, strategy_timeframe)
                    except Exception:
                        log.exception("Could not read SuperTrend stop reference for %s", symbol)
                try:
                    mark_price = get_mark_price(symbol)
                except Exception:
                    mark_price = None
                try:
                    orders = get_open_orders(product_id)
                    bot_orders = [o for o in orders if str(o.get("client_order_id", "")).startswith("stbot-")]
                    order = bot_orders[0] if bot_orders else None
                    pending_order = ({
                        "side": str(order.get("side", "")),
                        "remaining_lots": int(order.get("unfilled_size", order.get("size", 0))),
                        "limit_price": str(order.get("limit_price", "0")),
                    } if order else None)
                except Exception:
                    orders = None
                    pending_order = None
                stop_loss = "unavailable" if orders is None else None
                if orders is not None:
                    for active_order in orders:
                        bracket_sl = active_order.get("bracket_stop_loss_price")
                        stop_price = active_order.get("stop_price") if active_order.get("stop_order_type") == "stop_loss_order" else None
                        candidate = bracket_sl or stop_price
                        if candidate not in (None, "", "0", "0.0"):
                            stop_loss = str(candidate)
                            break
                margin_position = margined.get(symbol, {})
                positions.append({"symbol": symbol, "size": position_size,
                                  "mark_price": mark_price, "pending_order": pending_order,
                                  "unrealized_pnl": margin_position.get("unrealized_pnl"),
                                  "stop_loss": stop_loss,
                                  "supertrend_sl": supertrend_sl,
                                  "supertrend_direction": supertrend_direction})
        except Exception as exc:
            positions_error = str(exc)
    else:
        positions_error = "Add Delta API credentials to .env to show positions."

    delta_client_ip = None
    if positions_error:
        match = re.search(r"client_ip['\"]?\s*:\s*['\"]([^'\" ]+)", positions_error)
        if match:
            delta_client_ip = match.group(1)
    if delta_client_ip:
        public_ip, public_ip_error = delta_client_ip, None
    else:
        public_ip, public_ip_error = get_public_ip()

    return jsonify({
        "running": running,
        "process_alive": process_alive,
        "bot_health": bot_health,
        "bot_message": bot_message,
        "timeframe": active_timeframe if running else selected_timeframe,
        "segment": active_segment if running else selected_segment,
        "sizes": selected_sizes,
        "live_enabled": LIVE_TRADING,
        "positions": positions,
        "positions_error": positions_error,
        "public_ip": public_ip,
        "public_ip_error": public_ip_error,
        "activity": read_recent_activity(),
        "last_error": last_error,
        "supported_timeframes": list(SUPPORTED_TIMEFRAMES),
    })


@app.post("/api/timeframe")
def set_timeframe():
    global selected_timeframe
    body = request.get_json(silent=True) or {}
    timeframe = str(body.get("timeframe", "")).lower()
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return jsonify(error=f"Choose one of: {', '.join(SUPPORTED_TIMEFRAMES)}."), 400
    with state_lock:
        if strategy_requested_running or process_is_running():
            return jsonify(error="Stop the strategy before changing timeframe."), 409
        selected_timeframe = timeframe
    return jsonify(ok=True, timeframe=timeframe)


@app.post("/api/settings")
def set_settings():
    global selected_timeframe, selected_segment, selected_sizes
    body = request.get_json(silent=True) or {}
    timeframe = str(body.get("timeframe", selected_timeframe)).lower()
    segment = str(body.get("segment", selected_segment)).upper()
    try:
        btc_size = int(body.get("btc_size", selected_sizes["BTC_SIZE"]))
        eth_size = int(body.get("eth_size", selected_sizes["ETH_SIZE"]))
    except (TypeError, ValueError):
        return jsonify(error="Lot sizes must be whole numbers."), 400
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return jsonify(error=f"Choose one of: {', '.join(SUPPORTED_TIMEFRAMES)}."), 400
    if segment not in {"BTCUSD", "ETHUSD", "BTCUSD,ETHUSD"}:
        return jsonify(error="Choose BTC only, ETH only, or both."), 400
    if not (1 <= btc_size <= 10000 and 1 <= eth_size <= 10000):
        return jsonify(error="Lot sizes must be from 1 to 10000."), 400
    with state_lock:
        if strategy_requested_running or process_is_running():
            return jsonify(error="Stop the strategy before changing settings."), 409
        selected_timeframe = timeframe
        selected_segment = segment
        selected_sizes = {"BTC_SIZE": btc_size, "ETH_SIZE": eth_size}
        persist_settings({"TIMEFRAME": timeframe, "TRADE_SYMBOLS": segment, "BTC_SIZE": str(btc_size), "ETH_SIZE": str(eth_size)})
    return jsonify(ok=True, timeframe=timeframe, segment=segment, sizes=selected_sizes)


@app.post("/api/start")
def start():
    global bot_process, active_timeframe, active_segment, selected_timeframe, selected_segment, selected_sizes, last_error
    global strategy_requested_running, strategy_environment, restart_delay_seconds, restart_at, last_bot_launch_at
    if not LIVE_TRADING:
        return jsonify(error="Live trading is disabled. Set ENABLE_LIVE_TRADING=true in .env, then restart the dashboard."), 409
    body = request.get_json(silent=True) or {}
    timeframe = str(body.get("timeframe", "1m")).lower()
    segment = str(body.get("segment", selected_segment)).upper()
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return jsonify(error=f"Choose one of: {', '.join(SUPPORTED_TIMEFRAMES)}."), 400
    if segment not in {"BTCUSD", "ETHUSD", "BTCUSD,ETHUSD"}:
        return jsonify(error="Choose BTC only, ETH only, or both."), 400
    try:
        btc_size = int(body.get("btc_size", selected_sizes["BTC_SIZE"]))
        eth_size = int(body.get("eth_size", selected_sizes["ETH_SIZE"]))
    except (TypeError, ValueError):
        return jsonify(error="Lot sizes must be whole numbers."), 400
    if not (1 <= btc_size <= 10000 and 1 <= eth_size <= 10000):
        return jsonify(error="Lot sizes must be from 1 to 10000."), 400

    with state_lock:
        if strategy_requested_running or process_is_running():
            return jsonify(error="Strategy is already running."), 409
        env = os.environ.copy()
        env["TIMEFRAME"] = timeframe
        env["TRADE_SYMBOLS"] = segment
        env["BTC_SIZE"] = str(btc_size)
        env["ETH_SIZE"] = str(eth_size)
        try:
            strategy_environment = env.copy()
            bot_process = launch_bot(strategy_environment)
            strategy_requested_running = True
            restart_delay_seconds = 5
            restart_at = 0.0
            last_bot_launch_at = time.monotonic()
            active_timeframe = timeframe
            active_segment = segment
            selected_timeframe = timeframe
            selected_segment = segment
            selected_sizes = {"BTC_SIZE": btc_size, "ETH_SIZE": eth_size}
            persist_settings({"TIMEFRAME": timeframe, "TRADE_SYMBOLS": segment, "BTC_SIZE": str(btc_size), "ETH_SIZE": str(eth_size)})
            last_error = None
        except OSError as exc:
            bot_process = None
            strategy_requested_running = False
            strategy_environment = None
            return jsonify(error=f"Could not launch strategy: {exc}"), 500
    return jsonify(ok=True, timeframe=timeframe)


@app.post("/api/stop")
def stop():
    global bot_process, active_timeframe, active_segment, last_error
    global strategy_requested_running, strategy_environment, restart_at
    with state_lock:
        was_requested = strategy_requested_running
        strategy_requested_running = False
        strategy_environment = None
        restart_at = 0.0
        if not process_is_running():
            bot_process = None
            active_timeframe = None
            active_segment = None
            last_error = None
            return jsonify(ok=True, stopped=was_requested)
        assert bot_process is not None
        if os.name == "nt":
            bot_process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            bot_process.terminate()
        try:
            bot_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            bot_process.kill()
            bot_process.wait(timeout=5)
        bot_process = None
        active_timeframe = None
        active_segment = None
        last_error = None
    return jsonify(ok=True, stopped=True, positions_left_open=True)


if __name__ == "__main__":
    print(f"Dashboard listening locally at http://{HOST}:{PORT}")
    threading.Thread(target=bot_watchdog, name="strategy-watchdog", daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, threaded=True, use_reloader=False)
