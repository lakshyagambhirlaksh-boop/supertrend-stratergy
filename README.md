# Delta SuperTrend futures bot

This bot trades Delta India perpetual futures using **SuperTrend (length 15, factor 3)** on completed mark-price candles. Choose `1m`, `3m`, `5m`, `15m`, `30m`, or `1h`.

- Green SuperTrend: target a long position.
- Red SuperTrend: target a short position.
- A color change reverses the position after the candle closes.

The dashboard offers ETHUSD, SOLUSD, BNBUSD, SNDKBUSD, and CROSSUSD. **Select all** is on by default. BTCUSD is not a trading selection, but its position remains visible in the positions panel so an existing BTC position can be monitored.

Set one shared `LOTS_PER_SEGMENT` value. That many contract lots are used for every selected instrument. Each product's contract size and exposure can differ.

Orders start as post-only limit orders and are repriced after 3 and 6 seconds. At 10 seconds, an unfilled remainder may fall back to a market order and incur taker fees. A reversal submits the reduce-only close and new-side entry orders in the same reconciliation cycle. Stopping the bot cancels its pending orders and leaves positions open.

## Setup

Copy `.env.example` to `.env`, then enter your Delta API credentials. Keep `.env` private and do not commit it. `ENABLE_LIVE_TRADING` defaults to `false`; set it to `true` only when you intend to place live orders.

Install and run on Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python .\dashboard.py
```

Open `http://127.0.0.1:8765`. The dashboard is bound to localhost by default. For VPS access, use an SSH tunnel rather than exposing the unauthenticated control page publicly.

The dashboard saves timeframe, segment selection, and shared lot size into `.env`. It retries temporary bot errors, but trading pauses while the exchange or API is unavailable. The bot does not set an exchange stop-loss or take-profit.

## Streamlit Cloud

Use `streamlit_app.py` as the main file. Add credentials in **App settings → Secrets** using TOML:

```toml
DELTA_API_KEY = "your-key"
DELTA_API_SECRET = "your-secret"
DELTA_API_BASE = "https://api.india.delta.exchange"
```

The Streamlit page starts live trading only after **Start live strategy** is clicked. Whitelist the app's outbound IP in Delta when required.
