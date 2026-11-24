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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from itertools import combinations
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
SUPPORTED_SYMBOLS = ("ETHUSD", "SOLUSD", "BNBUSD", "SNDKBUSD", "SOXLBUSD", "MVLLBUSD")
SUPPORTED_SEGMENTS = {",".join(c) for size in range(1, len(SUPPORTED_SYMBOLS) + 1) for c in combinations(SUPPORTED_SYMBOLS, size)}

app = Flask(__name__)
log = logging.getLogger("delta-supertrend-dashboard")
state_lock = threading.Lock()
bot_process: subprocess.Popen[str] | None = None
bot_pid: int | None = None
strategy_requested_running = False
stopping_requested = False
strategy_environment: dict[str, str] | None = None
restart_delay_seconds = 5
restart_at = 0.0
last_bot_launch_at = 0.0
active_timeframe: str | None = None
active_segment: str | None = None
selected_timeframe = os.getenv("TIMEFRAME", "1m").strip().lower()
selected_segment = os.getenv("TRADE_SYMBOLS", "ETHUSD,SOLUSD,BNBUSD,SNDKBUSD,SOXLBUSD,MVLLBUSD").strip().upper()
selected_sizes = {
    "ETH_SIZE": int(os.getenv("ETH_SIZE", "1")),
    "SOL_SIZE": int(os.getenv("SOL_SIZE", "1")),
    "BNB_SIZE": int(os.getenv("BNB_SIZE", "1")),
    "SNDKB_SIZE": int(os.getenv("SNDKB_SIZE", "1")),
    "SOXLB_SIZE": int(os.getenv("SOXLB_SIZE", "1")),
    "MVLL_SIZE": int(os.getenv("MVLL_SIZE", "1")),
    "LOTS_PER_SEGMENT": int(os.getenv("LOTS_PER_SEGMENT", os.getenv("ETH_SIZE", "1"))),
}
last_error: str | None = None
ip_cache: dict[str, Any] = {"address": None, "checked_at": 0.0, "error": None}
ip_lock = threading.Lock()
supertrend_cache_lock = threading.Lock()
supertrend_cache: dict[tuple[str, str], tuple[int, float, int]] = {}
product_id_cache: dict[str, int] = {}
product_id_cache_lock = threading.Lock()
status_snapshot_lock = threading.Lock()
status_snapshot: dict[str, Any] | None = None
trade_activity_lock = threading.Lock()
trade_activity_cache: dict[str, Any] = {"checked_at": 0.0, "rows": [], "error": None}
TRADE_ACTIVITY_CACHE_SECONDS = 10
TRADE_TIMEZONE = timezone(timedelta(hours=5, minutes=30))
inr_balance_lock = threading.Lock()
inr_balance_cache: dict[str, Any] = {"checked_at": 0.0, "value": None, "error": None}
INR_BALANCE_CACHE_SECONDS = 10


def get_dashboard_product_ids(symbols: tuple[str, ...]) -> dict[str, int]:
    """Resolve missing product IDs concurrently and cache them for dashboard refreshes."""
    with product_id_cache_lock:
        missing = [symbol for symbol in symbols if symbol not in product_id_cache]
    if missing:
        with ThreadPoolExecutor(max_workers=min(6, len(missing))) as pool:
            resolved = dict(zip(missing, pool.map(get_product_id, missing)))
        with product_id_cache_lock:
            product_id_cache.update(resolved)
    with product_id_cache_lock:
        return {symbol: product_id_cache[symbol] for symbol in symbols}


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
    .note { color:var(--muted); font-size:13px; margin:12px 0 0; }
    .activity-table-wrap { overflow-x:auto; margin-top:10px; }
    .activity-table { width:100%; min-width:720px; border-collapse:collapse; font-size:13px; }
    .activity-table th,.activity-table td { text-align:left; padding:9px 10px; border-bottom:1px solid var(--line); white-space:nowrap; }
    .activity-table th { color:var(--muted); font-weight:650; }
    .activity-entry { color:#087a53; font-weight:700; } .activity-exit { color:#b12d4b; font-weight:700; }
    @media(max-width:650px) { .grid { grid-template-columns:1fr; } .wide,.connection-card { grid-column:auto; } .connection-grid { grid-template-columns:1fr; } .topbar { align-items:flex-start; flex-direction:column; } .api-badge { text-align:left; } }
  </style>
</head>
<body><main>
  <div class="topbar">
    <div><h1>Delta SuperTrend</h1><p class="sub">ETH, SOL, BNB, SNDKB and SOXL perpetuals · SuperTrend 15, factor 3 · mark-price candles</p></div>
    <div class="api-badge"><div class="label">Delta API key</div><div id="apiConnection" class="connection-value">Checking…</div></div>
  </div>
  <div class="grid">
    <section class="card">
      <div class="label">Strategy status</div><div id="status" class="state off">Loading…</div>
      <p id="live" class="note"></p>
      <div style="margin-top:18px">
        <label for="timeframe" class="label">Timeframe</label><br>
        <select id="timeframe"><option>1m</option><option>3m</option><option>5m</option><option>15m</option><option>30m</option><option>1h</option></select>
        <div class="label" style="margin-top:10px">Trading segments</div>
        <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:6px">
          <label><input id="selectAllSegments" type="checkbox" style="width:16px"> Select all</label>
          <label><input class="segment-check" type="checkbox" value="SOXLBUSD" style="width:16px"> SOXLBUSD</label>
          <label><input class="segment-check" type="checkbox" value="MVLLBUSD" style="width:16px"> MVLLBUSD</label>
          <label><input class="segment-check" type="checkbox" value="ETHUSD" style="width:16px"> ETH</label>
          <label><input class="segment-check" type="checkbox" value="SOLUSD" style="width:16px"> SOL</label>
          <label><input class="segment-check" type="checkbox" value="BNBUSD" style="width:16px"> BNB</label>
          <label><input class="segment-check" type="checkbox" value="SNDKBUSD" style="width:16px"> SNDKBUSD</label>
        </div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin:12px 0">
          <label id="lotsField" class="label">Lots for each selected segment<br><input id="lotsPerSegment" type="number" min="1" max="10000" step="1" value="1"></label>
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
        <div><div class="label">Delta account balance (INR)</div><div id="accountBalance" class="connection-value">Loadingâ€¦</div></div>
      </div>
      <p class="note">If your Delta API key has an IP whitelist, add this public IP address there.</p>
    </section>
    <section class="card wide">
      <div class="label">Recent trade activity</div>
      <p class="note">Latest Delta fills for the supported segments.</p>
      <div id="tradeActivity" class="activity-table-wrap">Loading trade historyâ€¦</div>
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
    const healthSuffix = d.bot_health === 'RETRYING' ? ' · RETRYING' : d.bot_health === 'RESTARTING' ? ' · RESTARTING' : d.bot_health === 'STOPPING' ? ' · STOPPING' : ` · ${d.timeframe}`;
    $('status').textContent = d.running ? `RUNNING${healthSuffix}` : 'STOPPED';
    $('status').className = `state ${d.running ? 'on' : 'off'}`;
    $('live').textContent = d.running && d.bot_health === 'STOPPING'
      ? 'Stopping the strategy and cancelling pending bot orders. Open positions remain open.'
      : d.running && (d.bot_health === 'RETRYING' || d.bot_health === 'RESTARTING')
      ? `Bot remains active and will retry; trading is paused during recovery: ${d.bot_message || 'retrying'}`
      : d.live_enabled ? 'Live order placement is enabled.' : 'Live order placement is disabled in .env.';
    $('live').className = `note ${d.live_enabled ? 'error' : ''}`;
    $('start').disabled = d.running || !d.live_enabled;
    $('stop').disabled = !d.running || d.bot_health === 'STOPPING';
    $('timeframe').disabled = d.running;
    document.querySelectorAll('.segment-check').forEach(el => el.disabled = d.running);
    $('selectAllSegments').disabled = d.running;
    $('lotsPerSegment').disabled = d.running;
    if (!d.running) $('lotsPerSegment').value = d.sizes.LOTS_PER_SEGMENT;
    if (d.timeframe) $('timeframe').value = d.timeframe;
    if (d.segment) {
      const active = d.segment.split(',');
      document.querySelectorAll('.segment-check').forEach(el => el.checked = active.includes(el.value));
      $('selectAllSegments').checked = active.length === 6;
      $('selectAllSegments').indeterminate = active.length > 0 && active.length < 6;
    }
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
    const inrBalance = Number(d.inr_balance);
    const hasInrBalance = d.inr_balance !== null && d.inr_balance !== undefined && Number.isFinite(inrBalance);
    $('accountBalance').textContent = hasInrBalance
      ? new Intl.NumberFormat('en-IN', {style:'currency', currency:'INR', minimumFractionDigits:2, maximumFractionDigits:2}).format(inrBalance)
      : 'Unavailable';
    $('accountBalance').className = `connection-value ${hasInrBalance ? '' : 'error'}`;
    $('accountBalance').title = d.balance_error || 'Total INR wallet balance from Delta.';
    const tradeRows = d.trade_activity || [];
    if (d.trade_activity_error) {
      $('tradeActivity').innerHTML = `<p class="error">Could not read Delta fills: ${escapeHtml(d.trade_activity_error)}</p>`;
    } else if (!tradeRows.length) {
      $('tradeActivity').innerHTML = '<p class="note">No Delta fills found for these segments yet.</p>';
    } else {
      $('tradeActivity').innerHTML = `<table class="activity-table"><thead><tr><th>Time (IST)</th><th>Segment</th><th>Entry</th><th>Side</th><th>Entry price</th><th>Exit price</th><th>Realized P&amp;L</th></tr></thead><tbody>${tradeRows.map(t => {
        const isExit = String(t.event).includes('EXIT') || String(t.event).includes('CLOSE');
        const side = String(t.event).includes('LONG') ? 'LONG' : String(t.event).includes('SHORT') ? 'SHORT' : '—';
        const pnl = t.realized_pnl === null || t.realized_pnl === undefined ? '—' : formatActivityPnl(t.realized_pnl, t.currency);
        return `<tr><td>${escapeHtml(t.time)}</td><td>${escapeHtml(t.symbol)}</td><td class="${isExit ? 'activity-exit' : 'activity-entry'}">${isExit ? 'CLOSE' : 'OPEN'}</td><td>${side}</td><td>${t.entry_price == null ? '—' : formatPrice(t.entry_price)}</td><td>${t.exit_price == null ? '—' : formatPrice(t.exit_price)}</td><td>${escapeHtml(pnl)}</td></tr>`;
      }).join('')}</tbody></table>`;
    }
  } catch (e) { $('status').textContent = 'Dashboard connection error'; $('status').className='state error'; }
  finally { refreshing = false; }
}
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function formatPrice(value) { const price = Number(value); return Number.isFinite(price) ? price.toFixed(2) : 'unavailable'; }
function formatPnl(value) { const pnl = Number(value); return Number.isFinite(pnl) ? `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}` : 'unavailable'; }
function formatActivityPnl(value, currency) { const pnl = Number(value); return Number.isFinite(pnl) ? `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)} ${currency || 'USD'}` : '—'; }
function selectedSymbols() {
  return ['ETHUSD','SOLUSD','BNBUSD','SNDKBUSD','SOXLBUSD','MVLLBUSD'].filter(symbol =>
    document.querySelector(`.segment-check[value="${symbol}"]`).checked);
}
function updateLotVisibility() {}
async function startBot() {
  const timeframe = $('timeframe').value;
  const symbols = selectedSymbols();
  if (!symbols.length) { alert('Select at least one trading segment.'); return; }
  const segment = symbols.join(',');
  const lots = Number($('lotsPerSegment').value);
  if (!Number.isInteger(lots) || lots < 1 || lots > 10000) { alert('Lots must be a whole number from 1 to 10000.'); return; }
  const sizes = {lots_per_segment:lots};
  const sizeText = `${lots} lots per selected segment`;
  if (!confirm(`Start LIVE trading on ${symbols.map(s => s.replace('USD','')).join(' + ')} using ${timeframe}? Size: ${sizeText}. Maker limits are repriced every 2 seconds and switch to market after 12 seconds if still unfilled.`)) return;
  $('status').textContent = 'RUNNING · STARTING';
  $('status').className = 'state on';
  $('live').textContent = 'Starting the strategy and connecting to Delta…';
  $('start').disabled = true;
  $('stop').disabled = false;
  try {
    const r = await fetch('/api/start', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({timeframe,segment,...sizes})});
    const d = await r.json();
    if (!r.ok) { alert(d.error || 'Could not start strategy.'); refresh(); return; }
    // Refresh runs in the background; Delta's position endpoints can respond
    // more slowly than the local process launch, so don't delay this feedback.
    refresh();
  } catch (e) {
    alert('Could not reach the local dashboard. Check that it is running, then refresh this page.');
    refresh();
  }
}
async function stopBot() {
  if (!confirm('Stop the strategy and cancel its pending orders? Any open Delta positions will remain open.')) return;
  $('status').textContent = 'STOPPING';
  $('status').className = 'state on';
  $('live').textContent = 'Stopping the strategy and cancelling pending bot orders. Open positions remain open.';
  $('stop').disabled = true;
  try {
    const r = await fetch('/api/stop', {method:'POST'}); const d = await r.json();
    if (!r.ok) alert(d.error || 'Could not stop strategy.');
  } catch (e) { alert('Could not reach the local dashboard. Check that it is running, then refresh this page.'); }
  refresh();
}
async function rememberTimeframe() {
  await saveSettings();
}
async function saveSettings() {
  const symbols = selectedSymbols();
  if (!symbols.length) return;
  const lots = Number($('lotsPerSegment').value);
  const r = await fetch('/api/settings', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({timeframe:$('timeframe').value,segment:symbols.join(','),lots_per_segment:lots})});
  if (!r.ok) { const d=await r.json(); alert(d.error || 'Could not save settings.'); }
}
$("timeframe").addEventListener('change', rememberTimeframe);
function onSegmentCheckboxChange() {
  const count = selectedSymbols().length;
  $('selectAllSegments').checked = count === 6;
  $('selectAllSegments').indeterminate = count > 0 && count < 6;
  updateLotVisibility(); saveSettings();
}
document.querySelectorAll('.segment-check').forEach(el => el.addEventListener('change', onSegmentCheckboxChange));
$('selectAllSegments').addEventListener('change', () => {
  document.querySelectorAll('.segment-check').forEach(el => el.checked = $('selectAllSegments').checked);
  updateLotVisibility(); saveSettings();
});
$("lotsPerSegment").addEventListener('change', saveSettings);
refresh(); setInterval(refresh, 2000);
</script>
</body></html>'''


def process_is_running() -> bool:
    if bot_process is not None:
        return bot_process.poll() is None
    if bot_pid is None:
        return False
    if os.name == "nt":
        return False
    try:
        cmdline = Path(f"/proc/{bot_pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        return str(BOT_PATH) in cmdline
    except (OSError, ValueError):
        return False


def find_existing_bot_pid() -> int | None:
    """Find a detached strategy process so a dashboard restart can reattach safely."""
    if os.name == "nt":
        return None
    proc_root = Path("/proc")
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            if str(BOT_PATH) in cmdline:
                return int(entry.name)
        except OSError:
            continue
    return None


def adopt_existing_bot() -> None:
    """Restore dashboard control of a bot that outlived a dashboard restart."""
    global bot_pid, strategy_requested_running, strategy_environment
    global active_timeframe, active_segment, selected_timeframe, selected_segment
    existing_pid = find_existing_bot_pid()
    if existing_pid is None:
        return
    bot_pid = existing_pid
    strategy_requested_running = True
    strategy_environment = os.environ.copy()
    active_timeframe = selected_timeframe = strategy_environment.get("TIMEFRAME", "1m").strip().lower()
    active_segment = selected_segment = strategy_environment.get(
        "TRADE_SYMBOLS", selected_segment
    ).strip().upper()
    log.warning("Reattached to existing strategy process PID %d after dashboard restart", existing_pid)


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
    global bot_pid
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [sys.executable, "-u", str(BOT_PATH)],
            cwd=str(ROOT),
            env=environment,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
    bot_pid = process.pid
    return process


def bot_watchdog() -> None:
    """Restart an unexpectedly exited bot until the dashboard Stop button is used."""
    global bot_process, bot_pid, restart_delay_seconds, restart_at, last_bot_launch_at, last_error
    while True:
        time.sleep(1)
        with state_lock:
            if not strategy_requested_running:
                continue
            now = time.monotonic()
            if process_is_running():
                continue
            if bot_process is not None or bot_pid is not None:
                exit_code = bot_process.returncode if bot_process is not None else "unknown"
                runtime = now - last_bot_launch_at
                bot_process = None
                bot_pid = None
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


def parse_fill_time(value: Any) -> tuple[float, str]:
    """Return a sortable timestamp and readable India time for a Delta fill."""
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            raw = float(value)
            seconds = raw / 1_000_000 if raw > 10_000_000_000_000 else raw / 1_000 if raw > 10_000_000_000 else raw
            moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
        else:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(TRADE_TIMEZONE)
        return moment.timestamp(), moment.strftime("%d-%b %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return 0.0, "—"


def summarize_fills(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn Delta fills into entry/exit rows using the position snapshot on each fill."""
    ordered = sorted(fills, key=lambda fill: parse_fill_time(fill.get("created_at"))[0])
    states: dict[str, dict[str, Any]] = {}
    activity: list[dict[str, Any]] = []
    for fill in ordered:
        symbol = str(fill.get("product_symbol") or (fill.get("product") or {}).get("symbol") or "")
        if not symbol:
            continue
        side = str(fill.get("side", "")).lower()
        try:
            fill_size = abs(int(float(fill.get("size", 0))))
            fill_price = float(fill.get("price"))
        except (TypeError, ValueError):
            continue
        if side not in {"buy", "sell"} or fill_size <= 0:
            continue

        signed_fill = fill_size if side == "buy" else -fill_size
        metadata = fill.get("meta_data") or {}
        new_position = metadata.get("new_position") or {}
        prior_state = states.get(symbol, {"size": 0, "entry_price": None, "realized_pnl": None})
        try:
            new_size = int(new_position.get("size")) if new_position.get("size") is not None else int(prior_state["size"]) + signed_fill
        except (TypeError, ValueError):
            new_size = int(prior_state["size"]) + signed_fill
        prior_size = new_size - signed_fill
        if prior_state["size"] != 0:
            prior_size = int(prior_state["size"])

        entry_price = prior_state.get("entry_price")
        reported_entry = new_position.get("entry_price")
        try:
            reported_entry = float(reported_entry) if reported_entry not in (None, "") else None
        except (TypeError, ValueError):
            reported_entry = None
        new_sign = 1 if new_size > 0 else -1 if new_size < 0 else 0
        old_sign = 1 if prior_size > 0 else -1 if prior_size < 0 else 0
        event_epoch, event_time = parse_fill_time(fill.get("created_at"))
        realized = new_position.get("realized_pnl")
        try:
            realized = float(realized) if realized not in (None, "") else None
        except (TypeError, ValueError):
            realized = None
        previous_realized = prior_state.get("realized_pnl")
        currency = str(fill.get("settling_asset_symbol") or "USD")

        def add_row(event: str, entry: Any, exit_price: Any, pnl: Any = None) -> None:
            activity.append({
                "time": event_time,
                "sort_time": event_epoch,
                "symbol": symbol,
                "event": event,
                "entry_price": entry,
                "exit_price": exit_price,
                "realized_pnl": pnl,
                "currency": currency,
            })

        if old_sign == 0 and new_sign != 0:
            entry_price = reported_entry or fill_price
            add_row("LONG ENTRY" if new_sign > 0 else "SHORT ENTRY", entry_price, None)
        elif old_sign == new_sign and abs(new_size) > abs(prior_size):
            entry_price = reported_entry or entry_price or fill_price
            add_row("ADD LONG" if new_sign > 0 else "ADD SHORT", entry_price, None)
        elif old_sign != 0 and (new_sign == 0 or old_sign != new_sign or abs(new_size) < abs(prior_size)):
            entry_price = entry_price or reported_entry
            event = "EXIT LONG" if old_sign > 0 else "EXIT SHORT"
            if new_sign != 0 and new_sign != old_sign:
                event = "REVERSE: CLOSE LONG" if old_sign > 0 else "REVERSE: CLOSE SHORT"
            fill_realized = realized
            if realized is not None and previous_realized is not None and new_sign == old_sign:
                fill_realized = realized - float(previous_realized)
            add_row(event, entry_price, fill_price, fill_realized)
            if new_sign != 0 and new_sign != old_sign:
                entry_price = reported_entry or fill_price
                add_row("OPEN LONG" if new_sign > 0 else "OPEN SHORT", entry_price, None)
        else:
            entry_price = reported_entry or entry_price

        states[symbol] = {
            "size": new_size,
            "entry_price": entry_price if new_size != 0 else None,
            "realized_pnl": realized if new_size != 0 else None,
        }

    return sorted(activity, key=lambda row: row["sort_time"], reverse=True)[:15]


def get_recent_trade_activity(product_ids: dict[str, int]) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch recent account fills on the configured segments, cached between polls."""
    now = time.monotonic()
    with trade_activity_lock:
        if now - trade_activity_cache["checked_at"] < TRADE_ACTIVITY_CACHE_SECONDS:
            return list(trade_activity_cache["rows"]), trade_activity_cache["error"]
    try:
        payload = signed_request("GET", "/v2/fills", {
            "product_ids": ",".join(str(value) for value in product_ids.values()),
            "page_size": 50,
        })
        rows = summarize_fills(payload.get("result", []))
        error = None
    except Exception as exc:
        rows, error = [], str(exc)
    with trade_activity_lock:
        trade_activity_cache.update(checked_at=time.monotonic(), rows=rows, error=error)
    return list(rows), error


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


def get_inr_account_balance() -> tuple[str | None, str | None]:
    """Read and briefly cache the total INR wallet balance from Delta."""
    now = time.monotonic()
    with inr_balance_lock:
        if now - inr_balance_cache["checked_at"] < INR_BALANCE_CACHE_SECONDS:
            return inr_balance_cache["value"], inr_balance_cache["error"]
    try:
        payload = signed_request("GET", "/v2/wallet/balances")
        wallet = next(
            (item for item in payload.get("result", [])
             if str(item.get("asset_symbol", "")).upper() == "INR"),
            None,
        )
        if wallet is None or wallet.get("balance") in (None, ""):
            raise RuntimeError("Delta did not return an INR wallet balance.")
        value, error = str(wallet["balance"]), None
    except Exception as exc:
        value, error = None, str(exc)
    with inr_balance_lock:
        inr_balance_cache.update(checked_at=time.monotonic(), value=value, error=error)
    return value, error


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


def build_status_snapshot() -> dict[str, Any]:
    global last_error
    with state_lock:
        process_alive = process_is_running()
        running = strategy_requested_running or process_alive
        bot_health, bot_message = read_bot_status()
        if stopping_requested and process_alive:
            bot_health = "STOPPING"
            bot_message = "Cancelling pending bot orders; open positions remain open."
        elif running and not process_alive:
            bot_health = "RESTARTING"
            bot_message = last_error or "Bot process exited; waiting for automatic restart."
        elif running and bot_health == "STOPPED":
            bot_health = "STARTING"
            bot_message = "Starting the strategy process."
        if running and bot_health == "OK":
            last_error = None

    positions: list[dict[str, Any]] = []
    positions_error = None
    trade_activity: list[dict[str, Any]] = []
    trade_activity_error = None
    strategy_timeframe = active_timeframe if running and active_timeframe else selected_timeframe
    if API_KEY and API_SECRET:
        try:
            symbols = ("ETHUSD", "SOLUSD", "BNBUSD", "SNDKBUSD", "SOXLBUSD", "MVLLBUSD")
            # Product metadata is static and was being fetched again on every
            # two-second dashboard poll. Resolve once, in parallel, then reuse.
            product_ids = get_dashboard_product_ids(symbols)
            trade_activity, trade_activity_error = get_recent_trade_activity(product_ids)
            try:
                margined = get_margined_positions(list(product_ids.values()))
            except Exception:
                margined = {}

            def read_symbol(symbol: str) -> dict[str, Any]:
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
                return {"symbol": symbol, "size": position_size,
                        "mark_price": mark_price, "pending_order": pending_order,
                        "unrealized_pnl": margin_position.get("unrealized_pnl"),
                        "stop_loss": stop_loss,
                        "supertrend_sl": supertrend_sl,
                        "supertrend_direction": supertrend_direction}

            # These independent reads were sequential, so a slow Delta response
            # for one segment delayed the entire dashboard refresh/start display.
            with ThreadPoolExecutor(max_workers=len(symbols)) as pool:
                positions = list(pool.map(read_symbol, symbols))
        except Exception as exc:
            positions_error = str(exc)
            if trade_activity_error is None:
                trade_activity_error = str(exc)
    else:
        positions_error = "Add Delta API credentials to .env to show positions."
        trade_activity_error = positions_error

    if API_KEY and API_SECRET:
        inr_balance, balance_error = get_inr_account_balance()
    else:
        inr_balance, balance_error = None, "Add Delta API credentials to .env to read the account balance."

    delta_client_ip = None
    if positions_error:
        match = re.search(r"client_ip['\"]?\s*:\s*['\"]([^'\" ]+)", positions_error)
        if match:
            delta_client_ip = match.group(1)
    if delta_client_ip:
        public_ip, public_ip_error = delta_client_ip, None
    else:
        public_ip, public_ip_error = get_public_ip()

    return {
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
        "inr_balance": inr_balance,
        "balance_error": balance_error,
        "trade_activity": trade_activity,
        "trade_activity_error": trade_activity_error,
        "last_error": last_error,
        "supported_timeframes": list(SUPPORTED_TIMEFRAMES),
    }


def refresh_status_cache() -> None:
    """Fetch slow Delta account data off the request path for a responsive UI."""
    global status_snapshot
    while True:
        try:
            snapshot = build_status_snapshot()
            with status_snapshot_lock:
                status_snapshot = snapshot
        except Exception:
            log.exception("Could not refresh dashboard status snapshot")
        time.sleep(2)


@app.get("/api/status")
def status():
    with status_snapshot_lock:
        snapshot = dict(status_snapshot) if status_snapshot is not None else {
            "running": strategy_requested_running or process_is_running(),
            "process_alive": process_is_running(),
            "bot_health": "STARTING" if strategy_requested_running else "STOPPED",
            "bot_message": "Loading the latest Delta account data.",
            "timeframe": active_timeframe or selected_timeframe,
            "segment": active_segment or selected_segment,
            "sizes": selected_sizes,
            "live_enabled": LIVE_TRADING,
            "positions": [],
            "positions_error": "Loading live data from Delta…",
            "public_ip": None,
            "public_ip_error": None,
            "inr_balance": None,
            "balance_error": "Loading INR account balance from Delta…" if API_KEY and API_SECRET else "Add Delta API credentials to .env to read the account balance.",
            "trade_activity": [],
            "trade_activity_error": "Loading recent fills from Delta…" if API_KEY and API_SECRET else "Add Delta API credentials to .env to show positions.",
            "last_error": last_error,
            "supported_timeframes": list(SUPPORTED_TIMEFRAMES),
        }
    with state_lock:
        process_alive = process_is_running()
        running = strategy_requested_running or process_alive
        snapshot.update(
            running=running,
            process_alive=process_alive,
            timeframe=active_timeframe if running and active_timeframe else selected_timeframe,
            segment=active_segment if running and active_segment else selected_segment,
            sizes=selected_sizes,
            live_enabled=LIVE_TRADING,
        )
        if stopping_requested and process_alive:
            snapshot.update(
                bot_health="STOPPING",
                bot_message="Cancelling pending bot orders; open positions remain open.",
            )
        elif running and not process_alive:
            snapshot.update(
                bot_health="RESTARTING",
                bot_message=last_error or "Bot process exited; waiting for automatic restart.",
            )
        elif not running:
            snapshot.update(bot_health="STOPPED", bot_message="Stopped by request.")
    return jsonify(snapshot)


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
        lots = int(body.get("lots_per_segment", selected_sizes["LOTS_PER_SEGMENT"]))
        eth_size = sol_size = bnb_size = sndkb_size = soxlb_size = mvll_size = lots
    except (TypeError, ValueError):
        return jsonify(error="Lot sizes must be whole numbers."), 400
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return jsonify(error=f"Choose one of: {', '.join(SUPPORTED_TIMEFRAMES)}."), 400
    if segment not in SUPPORTED_SEGMENTS:
        return jsonify(error="Choose one or more supported ETH, SOL, BNB, SNDKB, SOXLB, or MVLL segments."), 400
    if not (1 <= lots <= 10000):
        return jsonify(error="Lot sizes must be from 1 to 10000."), 400
    with state_lock:
        if strategy_requested_running or process_is_running():
            return jsonify(error="Stop the strategy before changing settings."), 409
        selected_timeframe = timeframe
        selected_segment = segment
        selected_sizes = {"ETH_SIZE": eth_size, "SOL_SIZE": sol_size, "BNB_SIZE": bnb_size, "SNDKB_SIZE": sndkb_size, "SOXLB_SIZE": soxlb_size, "MVLL_SIZE": mvll_size, "LOTS_PER_SEGMENT": lots}
        persist_settings({"TIMEFRAME": timeframe, "TRADE_SYMBOLS": segment, "ETH_SIZE": str(eth_size), "SOL_SIZE": str(sol_size), "BNB_SIZE": str(bnb_size), "SNDKB_SIZE": str(sndkb_size), "SOXLB_SIZE": str(soxlb_size), "MVLL_SIZE": str(mvll_size), "LOTS_PER_SEGMENT": str(lots)})
    return jsonify(ok=True, timeframe=timeframe, segment=segment, sizes=selected_sizes)


@app.post("/api/start")
def start():
    global bot_process, bot_pid, active_timeframe, active_segment, selected_timeframe, selected_segment, selected_sizes, last_error
    global strategy_requested_running, strategy_environment, restart_delay_seconds, restart_at, last_bot_launch_at, stopping_requested
    if not LIVE_TRADING:
        return jsonify(error="Live trading is disabled. Set ENABLE_LIVE_TRADING=true in .env, then restart the dashboard."), 409
    body = request.get_json(silent=True) or {}
    timeframe = str(body.get("timeframe", "1m")).lower()
    segment = str(body.get("segment", selected_segment)).upper()
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return jsonify(error=f"Choose one of: {', '.join(SUPPORTED_TIMEFRAMES)}."), 400
    if segment not in SUPPORTED_SEGMENTS:
        return jsonify(error="Choose one or more supported ETH, SOL, BNB, SNDKB, SOXLB, or MVLL segments."), 400
    try:
        lots = int(body.get("lots_per_segment", selected_sizes["LOTS_PER_SEGMENT"]))
        eth_size = sol_size = bnb_size = sndkb_size = soxlb_size = mvll_size = lots
    except (TypeError, ValueError):
        return jsonify(error="Lot sizes must be whole numbers."), 400
    if not (1 <= lots <= 10000):
        return jsonify(error="Lot sizes must be from 1 to 10000."), 400

    with state_lock:
        if strategy_requested_running or process_is_running():
            return jsonify(error="Strategy is already running."), 409
        env = os.environ.copy()
        env["TIMEFRAME"] = timeframe
        env["TRADE_SYMBOLS"] = segment
        env["ETH_SIZE"] = str(eth_size)
        env["SOL_SIZE"] = str(sol_size)
        env["BNB_SIZE"] = str(bnb_size)
        env["SNDKB_SIZE"] = str(sndkb_size)
        env["SOXLB_SIZE"] = str(soxlb_size)
        env["MVLL_SIZE"] = str(mvll_size)
        env["LOTS_PER_SEGMENT"] = str(lots)
        try:
            strategy_environment = env.copy()
            bot_process = launch_bot(strategy_environment)
            strategy_requested_running = True
            stopping_requested = False
            restart_delay_seconds = 5
            restart_at = 0.0
            last_bot_launch_at = time.monotonic()
            active_timeframe = timeframe
            active_segment = segment
            selected_timeframe = timeframe
            selected_segment = segment
            selected_sizes = {"ETH_SIZE": eth_size, "SOL_SIZE": sol_size, "BNB_SIZE": bnb_size, "SNDKB_SIZE": sndkb_size, "SOXLB_SIZE": soxlb_size, "MVLL_SIZE": mvll_size, "LOTS_PER_SEGMENT": lots}
            persist_settings({"TIMEFRAME": timeframe, "TRADE_SYMBOLS": segment, "ETH_SIZE": str(eth_size), "SOL_SIZE": str(sol_size), "BNB_SIZE": str(bnb_size), "SNDKB_SIZE": str(sndkb_size), "SOXLB_SIZE": str(soxlb_size), "MVLL_SIZE": str(mvll_size), "LOTS_PER_SEGMENT": str(lots)})
            last_error = None
        except OSError as exc:
            bot_process = None
            bot_pid = None
            strategy_requested_running = False
            strategy_environment = None
            return jsonify(error=f"Could not launch strategy: {exc}"), 500
    return jsonify(ok=True, timeframe=timeframe)


@app.post("/api/stop")
def stop():
    global bot_process, bot_pid, active_timeframe, active_segment, last_error, stopping_requested
    global strategy_requested_running, strategy_environment, restart_at
    with state_lock:
        was_requested = strategy_requested_running
        strategy_requested_running = False
        strategy_environment = None
        restart_at = 0.0
        if not process_is_running():
            bot_process = None
            bot_pid = None
            active_timeframe = None
            active_segment = None
            last_error = None
            stopping_requested = False
            return jsonify(ok=True, stopped=was_requested, stopping=False)
        process = bot_process
        pid = bot_pid
        try:
            if process is not None:
                process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
            elif pid is not None:
                os.kill(pid, signal.SIGTERM)
            else:
                return jsonify(error="The strategy process could not be located safely."), 409
        except ProcessLookupError:
            pass
        except OSError as exc:
            return jsonify(error=f"Could not signal the strategy process: {exc}"), 500
        stopping_requested = True

    threading.Thread(
        target=finish_stop,
        args=(process, pid),
        name="strategy-stop-cleanup",
        daemon=True,
    ).start()
    return jsonify(ok=True, stopping=True, positions_left_open=True)


def finish_stop(process: subprocess.Popen[str] | None, pid: int | None) -> None:
    """Wait for graceful order cancellation without holding up the Stop request."""
    global bot_process, bot_pid, active_timeframe, active_segment, last_error, stopping_requested
    if process is not None:
        process.wait()
    else:
        while pid is not None and process_is_running():
            time.sleep(0.5)
    with state_lock:
        if pid is None or bot_pid == pid:
            bot_process = None
            bot_pid = None
            active_timeframe = None
            active_segment = None
            last_error = None
            stopping_requested = False


if __name__ == "__main__":
    print(f"Dashboard listening locally at http://{HOST}:{PORT}")
    adopt_existing_bot()
    threading.Thread(target=bot_watchdog, name="strategy-watchdog", daemon=True).start()
    threading.Thread(target=refresh_status_cache, name="delta-status-cache", daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, threaded=True, use_reloader=False)
