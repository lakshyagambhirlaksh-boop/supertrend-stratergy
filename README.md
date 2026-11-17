# Delta SuperTrend BTC/ETH futures bot

This bot follows the chart settings shown: **mark-price candles**, **SuperTrend length 15**, **factor 3**. Set `TIMEFRAME` in `.env` to one of `1m`, `3m`, `5m`, `15m`, `30m`, or `1h` (default: `1m`). It runs one timeframe for the selected BTC/ETH segment at a time.

- Green SuperTrend at a completed candle: close a short if present, then go long.
- Red SuperTrend at a completed candle: close a long if present, then go short.
- While the trend color is unchanged, it leaves the position alone.
- Orders begin as post-only GTC limit orders at the passive best bid/ask. If still unfilled after a fixed 10 seconds, the bot cancels the maker order and sends the remaining amount as a market order. The fallback can incur taker fees. The bot tracks pending orders and changes them when the desired side changes.

It uses Delta India public mark-price OHLC data for signals and Delta India futures post-only limit orders for execution. Unfilled maker orders are repriced to the current passive best bid/ask at 3 and 6 seconds. At 10 seconds, the bot cancels the pending maker order(s) and uses a market fallback for any remaining target quantity. Position size is set as integer contract lots separately for BTCUSD and ETHUSD. The dashboard offers 20x, 50x, and 100x leverage, defaulting to 20x. Set the same leverage in Delta first; before trading, the dashboard and bot check Delta's reported leverage for each selected contract. The bot will not place orders if the value does not match.

On a SuperTrend reversal, the bot submits the reduce-only close limit and the new-direction entry limit in the same reconciliation cycle. Both are tracked while pending; if either reaches the 10-second timeout, the bot cancels the reversal pair before reconciling the net position at market.

## Setup on Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Open `.env` and enter your Delta API key and secret. Do not share this file or paste the credentials into chat. Create a Delta key with only the permissions required for your chosen mode; read access is enough for paper mode, and trading permission is needed for live orders.

## Run in paper mode

Keep `ENABLE_LIVE_TRADING=false` in `.env`, then run:

```powershell
python .\supertrend_delta_bot.py
```

Paper mode fetches data and prints the orders it would place; it does not send orders. By default it logs a 1-lot position for each instrument. Set `BTC_SIZE` and `ETH_SIZE` to the desired contract lots before running.

## Live orders

Only after reviewing the code and checking paper behavior, set `ENABLE_LIVE_TRADING=true` in `.env`. Live mode reads the current position and uses a reduce-only post-only limit order to close it before opening the opposite side. If the order is still unfilled after the timeout, it falls back to a market order for the remaining quantity. It does not set leverage, a stop-loss, take-profit, or account risk limits. Set leverage in Delta and confirm it matches the dashboard selection before starting; leverage magnifies both gains and losses and can increase liquidation risk.

## Dashboard

Install requirements, then run `python .\dashboard.py` and open `http://127.0.0.1:8765`. Choose a timeframe, BTC/ETH/both segment, contract lot sizes, and a leverage selection (20x default). Set that same leverage in Delta first; the dashboard verifies it before launch. Settings are saved into `.env` and can be changed while the strategy is stopped. Start may place post-only limit orders immediately to align positions with the current closed candle. **Stop strategy** terminates the bot and leaves any open positions on Delta unchanged. The dashboard binds to localhost by default; on a VPS, use an SSH tunnel rather than exposing this unauthenticated control page to the public internet.

The bot reconciles its position to the current completed candle when it starts, then checks for each new completed candle in the selected timeframe. Keep the process running; it does not restore order history or protect against an unexpected manual trade between checks.

## Streamlit Cloud deployment

Set the Streamlit Cloud app's **Main file path** to `streamlit_app.py` (the previous `supertrend_delta_bot.py` entry is also routed to the UI to avoid the signal-handler crash). Add the credentials under **App settings → Secrets**, using TOML:

```toml
DELTA_API_KEY = "your-key"
DELTA_API_SECRET = "your-secret"
DELTA_API_BASE = "https://api.india.delta.exchange"
```

The Streamlit page starts live trading only when **Start live strategy** is clicked. Delta API keys must allow the Streamlit app's outbound IP; the dashboard shows the IP and reports Delta's observed IP when the exchange includes it in an API error. Never commit credentials to GitHub.
