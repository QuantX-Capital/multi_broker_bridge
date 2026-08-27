import html
import json
import os
import webbrowser
from pathlib import Path
from datetime import datetime, timedelta, time
from time import sleep
from zoneinfo import ZoneInfo
import boto3
from kiteconnect import KiteConnect, KiteTicker
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import talib
import requests

from brokers import make_broker
from logger import enable_file_logging, tail_log

enable_file_logging()

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
with open(CONFIG_PATH, "r") as f:
    CONFIG = json.load(f)

# execution broker: exactly one of config.json's "brokers" must be set to 1
# - "zerodha" (Kite, MKT orders) or "mastertrust" (Noren, marketable LMT)
_enabled_brokers = [name for name, flag in CONFIG.get("brokers", {}).items() if flag == 1]
if len(_enabled_brokers) != 1:
    raise RuntimeError(
        f"config.json 'brokers' must have exactly one broker set to 1, "
        f"got: {CONFIG.get('brokers')}"
    )
EXEC_BROKER = _enabled_brokers[0]

IST = ZoneInfo("Asia/Kolkata")
ZERODHA_SECRET_ID = "/trading/brokers/zerodha/luv"

CHARTS_DIR = Path(__file__).resolve().parent / "charts"
# Browser tab reload cadence. Independent of the bar interval - the HTML file
# itself is only rewritten when a bar actually closes, this just controls how
# soon the open tab picks up a rewritten file.
REFRESH_SECONDS = 60

# Exit everything ourselves ahead of the broker's own MIS auto square-off
# (which can land anywhere in a ~15:15-15:25 window on a blunt market order
# and cost real slippage/charges) so exits happen on our terms, earlier and
# more predictably.
SQUAREOFF_TIME = time(15, 10)

interval = "5minute"
days = 3

# ticker -> instrument_token. Add more entries here to trade multiple tickers at once.
TICKERS = {
    "RELIANCE": 738561,
    "TMPV":884737,
    "HINDALCO": 348929,
    "SBIN": 779521
}

# per-ticker tranche size (quantity per entry/add). Falls back to 1 if a ticker
# isn't listed here.
QUANTITY = {
    "RELIANCE": 1,
    "TMPV": 1,
    "HINDALCO": 1,
    "SBIN": 1
}



def get_zerodha_secret():
    """Fetch Zerodha credentials from AWS Secrets Manager.

    api_key falls back to the zerodha_api_key env var if the secret only
    holds access_token. CreatedDate stands in for the old generated_date
    check - it advances whenever the secret value is refreshed."""
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=ZERODHA_SECRET_ID)

    updated_date = response["CreatedDate"].astimezone(IST).date()
    if updated_date != datetime.now(tz=IST).date():
        raise RuntimeError(
            f"Zerodha secret '{ZERODHA_SECRET_ID}' was last updated on {updated_date}, "
            "not today. The access_token has likely expired - refresh it and update "
            "the secret before running the strategy."
        )

    secret = json.loads(response["SecretString"])
    secret.setdefault("api_key", CONFIG.get("zerodha_api_key"))
    if not secret.get("api_key") or not secret.get("access_token"):
        raise RuntimeError(
            f"Zerodha secret '{ZERODHA_SECRET_ID}' is missing api_key/access_token "
            "(and no zerodha_api_key fallback was found in config.json)."
        )
    return secret


def load_kite_client():
    secret = get_zerodha_secret()
    kite = KiteConnect(api_key=secret["api_key"])
    kite.set_access_token(secret["access_token"])
    return kite

def fetch_historical_data(kite, instrument_token, interval="5minute", days=4):
    from_date = (datetime.now() - timedelta(days)).strftime("%Y-%m-%d")
    to_date = datetime.now().strftime("%Y-%m-%d")

    candles = kite.historical_data(
        instrument_token=instrument_token,
        from_date=from_date,
        to_date=to_date,
        interval=interval,
        continuous=False,
        oi=False,
    )

    df = pd.DataFrame(candles)

    # Normalize to match Fyers output: named IST zone, index named "timestamp"
    df["date"] = pd.to_datetime(df["date"]).dt.tz_convert("Asia/Kolkata")
    df = df.rename(columns={"date": "timestamp"})
    df = df.set_index("timestamp")

    return df



kite = load_kite_client()
broker = make_broker(EXEC_BROKER, kite=kite, tickers=list(TICKERS.keys()))
data = {
    ticker: fetch_historical_data(kite, instrument_token=token, interval=interval)
    for ticker, token in TICKERS.items()
}
for ticker, df in data.items():
    print(f"--- {ticker} ---")
    print(df.head())



def fetch_trade_markers(broker):
    """Today's real fills from the active broker (whichever one EXEC_BROKER
    selected), grouped by ticker and snapped to the bar they fall in (a fill
    lands mid-bar, e.g. 11:00:55, but the chart only has one candle per 5min,
    e.g. 11:00 - so round down to line up with it). BUY -> ENTRY marker,
    SELL -> EXIT marker; no ADD distinction, every buy is drawn the same way.
    Source of truth is the broker, not the bot's own memory, so this picks up
    trades placed manually as well as by the bot, and survives bot restarts."""
    markers = {ticker: [] for ticker in TICKERS}
    for trade in broker.trades():
        ticker = trade["tsym"]
        if ticker not in markers:
            continue
        bar_time = trade["fill_timestamp"].replace(second=0, microsecond=0)
        bar_time -= timedelta(minutes=bar_time.minute % 5)
        bar_time = bar_time.replace(tzinfo=IST)
        action = "ENTRY" if trade["transaction_type"] == "BUY" else "EXIT"
        markers[ticker].append({"timestamp": bar_time, "action": action})
    return markers


def make_candle_fig(df, title, log=None):
    """Candlestick + Bollinger bands, with ENTRY/EXIT markers if a bar log is given.
    Bands are recomputed here so this works on both raw historical data (no bb_
    columns yet) and a strategy's already-annotated df."""
    df_bb = df.copy()
    if "bb_upper" not in df_bb.columns:
        df_bb["bb_upper"], df_bb["bb_middle"], df_bb["bb_lower"] = talib.BBANDS(
            df_bb["close"].to_numpy(dtype=float),
            timeperiod=20,
            nbdevup=2.0,
            nbdevdn=2.0,
            matype=0,
        )

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df_bb.index, open=df_bb["open"], high=df_bb["high"],
        low=df_bb["low"], close=df_bb["close"], name="price",
    ))
    fig.add_trace(go.Scatter(x=df_bb.index, y=df_bb["bb_upper"], line=dict(color="royalblue", width=1), name="BB upper"))
    fig.add_trace(go.Scatter(x=df_bb.index, y=df_bb["bb_middle"], line=dict(color="gray", width=1, dash="dot"), name="BB mid"))
    fig.add_trace(go.Scatter(x=df_bb.index, y=df_bb["bb_lower"], line=dict(color="royalblue", width=1), name="BB lower"))

    if log:
        log_df = pd.DataFrame(log).set_index("timestamp")
        for action, color, symbol, price_col, offset in [
            ("ENTRY", "yellow", "triangle-up", "low", 0.998),
            ("EXIT", "red", "triangle-down", "high", 1.002),
        ]:
            times = log_df.index[log_df["action"] == action].intersection(df_bb.index)
            if len(times):
                fig.add_trace(go.Scatter(
                    x=times, y=df_bb.loc[times, price_col] * offset,
                    mode="markers",
                    marker=dict(symbol=symbol, size=22, color=color, line=dict(color="white", width=1)),
                    name=action,
                ))

    today = datetime.now(tz=IST).date()
    session_start = datetime.combine(today, time(9, 15), tzinfo=IST)
    session_end = datetime.combine(today, time(15, 30), tzinfo=IST)
    # pad both edges by half a bar so the first/last candle isn't clipped -
    # a candle is drawn centered on its timestamp, so one sitting exactly on
    # the range boundary only shows its right (or left) half
    pad = timedelta(minutes=2, seconds=30)

    fig.update_layout(title=title, xaxis_rangeslider_visible=False, template="plotly_dark", autosize=True)
    fig.update_xaxes(
        range=[session_start - pad, session_end + pad],
        rangebreaks=[
            dict(bounds=[16.00, 8.00], pattern="hour"),  # hide 15:30 -> next day's 09:15
            dict(bounds=["sat", "mon"]),                # hide weekends
        ],
    )
    return fig


def build_log_rows_html():
    """Render the tail of today's log file as <tr> rows for the dashboard's
    bottom log panel. Each line is '[timestamp] message'; the timestamp is
    split into its own narrow column. Content is HTML-escaped."""
    rows = []
    for line in tail_log():
        if line.startswith("[") and "] " in line:
            ts, msg = line[1:].split("] ", 1)
        else:
            ts, msg = "", line
        rows.append(
            f'<tr><td class="log-ts">{html.escape(ts)}</td>'
            f'<td class="log-msg">{html.escape(msg)}</td></tr>'
        )
    if not rows:
        rows.append('<tr><td class="log-ts"></td>'
                    '<td class="log-msg">(no log output yet)</td></tr>')
    return "".join(rows)


def build_dashboard_html(figs):
    """Combine per-ticker figures into one page: a dropdown toggles which
    ticker's chart panel is visible. Selection is kept in localStorage so it
    survives the page's own auto-refresh (meta refresh reloads the whole
    page, which would otherwise reset the dropdown to the first ticker).

    The bottom 25% of the page is a scrolling table of the most recent log
    lines (embedded at file-write time, so it's as fresh as the last chart
    rebuild)."""
    panels = []
    options = []
    for i, (ticker, fig) in enumerate(figs.items()):
        include_js = "cdn" if i == 0 else False
        chart_html = fig.to_html(
            include_plotlyjs=include_js, full_html=False,
            default_width="100%", default_height="100%",
            config={"responsive": True},
        )
        active_class = " active" if i == 0 else ""
        panels.append(f'<div id="tab-{ticker}" class="tab-panel{active_class}">{chart_html}</div>')
        options.append(f'<option value="{ticker}">{ticker}</option>')

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>Live Trading Dashboard</title>
<style>
  html, body {{ height: 100%; margin: 0; }}
  body {{ font-family: sans-serif; display: flex; flex-direction: column; background: #111418; color: #e6e6e6; }}
  .header {{ flex: 0 0 auto; padding: 12px 16px; }}
  select {{ font-size: 16px; padding: 6px; background: #1e2229; color: #e6e6e6; border: 1px solid #3a3f47; }}
  .charts {{ flex: 3 1 0; min-height: 0; display: flex; flex-direction: column; }}
  .tab-panel {{ display: none; flex: 1 1 auto; min-height: 0; }}
  .tab-panel.active {{ display: block; }}
  .tab-panel > div {{ width: 100% !important; height: 100% !important; }}
  .logs {{ flex: 1 1 0; min-height: 0; overflow-y: auto; border-top: 1px solid #3a3f47; background: #0c0f12; }}
  .logs table {{ width: 100%; border-collapse: collapse;
                 font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  .logs td {{ padding: 2px 10px; vertical-align: top; border-bottom: 1px solid #1a1e24;
              white-space: pre-wrap; word-break: break-word; }}
  .log-ts {{ color: #7a828c; white-space: nowrap; width: 1%; }}
</style>
</head>
<body>
<div class="header">
<label for="ticker-select"><strong>Ticker:</strong></label>
<select id="ticker-select" onchange="showTicker(this.value)">
{''.join(options)}
</select>
</div>
<div class="charts">
{''.join(panels)}
</div>
<div class="logs" id="logs">
<table><tbody>
{build_log_rows_html()}
</tbody></table>
</div>
<script>
function showTicker(ticker) {{
  document.querySelectorAll('.tab-panel').forEach(function(el) {{ el.classList.remove('active'); }});
  var panel = document.getElementById('tab-' + ticker);
  if (panel) {{
    panel.classList.add('active');
    // Panels other than the first are drawn by Plotly while still display:none,
    // so they come out at a small fallback size. Force a resize now that the
    // panel is actually visible and has real dimensions.
    var plot = panel.querySelector('.plotly-graph-div');
    if (plot && window.Plotly) {{
      setTimeout(function() {{ Plotly.Plots.resize(plot); }}, 0);
    }}
  }}
  try {{ localStorage.setItem('selectedTicker', ticker); }} catch (e) {{}}
}}
(function() {{
  var saved = null;
  try {{ saved = localStorage.getItem('selectedTicker'); }} catch (e) {{}}
  var select = document.getElementById('ticker-select');
  var options = Array.prototype.map.call(select.options, function(o) {{ return o.value; }});
  var initial = (saved && options.indexOf(saved) !== -1) ? saved : options[0];
  select.value = initial;
  showTicker(initial);
}})();
(function() {{
  // Keep the log panel pinned to the newest line across each auto-refresh.
  var logs = document.getElementById('logs');
  if (logs) logs.scrollTop = logs.scrollHeight;
}})();
</script>
</body>
</html>"""


def write_dashboard_html(figs):
    """Overwrite the combined dashboard file. The already-open browser tab
    picks up the new content on its next auto-refresh (see REFRESH_SECONDS)."""
    CHARTS_DIR.mkdir(exist_ok=True)
    path = CHARTS_DIR / "dashboard.html"
    path.write_text(build_dashboard_html(figs), encoding="utf-8")
    return path




import numpy as np
import pandas as pd


class LONGSTRATEGY:
    """Bollinger Band pyramiding, long only. One instance per ticker."""

    def __init__(self, ticker, quantity, max_entries=5, bb_period=20, bb_dev=2.0):
        self.ticker = ticker
        self.quantity = quantity          # qty per tranche
        self.max_entries = max_entries    # total tranches incl. initial entry
        self.bb_period = bb_period
        self.bb_dev = bb_dev

        self.enteries_taken = 0
        self.df = None
        self.log = []   # one row per closed bar, kept in memory while the bot runs
        self.enabled = True   # set False after the daily square-off to block new entries

    # ---------- indicators ----------
    def add_bollinger_bands(self):
        df = self.df.copy()
        df["bb_upper"], df["bb_middle"], df["bb_lower"] = talib.BBANDS(
            df["close"].to_numpy(dtype=float),
            timeperiod=self.bb_period,
            nbdevup=self.bb_dev,
            nbdevdn=self.bb_dev,
            matype=0,
        )
        df.dropna(inplace=True)
        self.df = df
        return self

    # ---------- signals ----------
    def create_conditions(self):
        df = self.df.copy()

        df["entry_conditions"] = np.where(df["close"] <= df["bb_lower"], 1, 0)
        df["exit_conditions"] = np.where(df["close"] >= df["bb_upper"], -1, 0)

        df["positions"] = df["entry_conditions"] + df["exit_conditions"]
        df["positions"] = np.where(df["positions"] == 0, np.nan, df["positions"])
        df["positions"] = df["positions"].ffill()

        # armed once price reclaims the middle band; disarmed by any lower-band touch
        df["criteria_2"] = np.where(
            df["entry_conditions"] == 1, 0,
            np.where(df["close"] >= df["bb_middle"], 1, np.nan),
        )
        df["criteria_2"] = df["criteria_2"].ffill()

        prev_pos = df["positions"].shift(1)
        prev_armed = df["criteria_2"].shift(1)

        df["fresh_entry"] = np.where(
            (df["entry_conditions"] == 1) & (prev_pos != 1), 1, 0
        )
        df["add_position"] = np.where(
            (df["entry_conditions"] == 1) & (prev_pos == 1) & (prev_armed == 1), 1, 0
        )

        self.df = df
        return self

    # ---------- logging ----------
    def _log_bar(self, bar_time, status, action):
        self.log.append({
            "timestamp": bar_time,
            "ticker": self.ticker,
            "open": status["open"],
            "high": status["high"],
            "low": status["low"],
            "close": status["close"],
            "volume": status["volume"],
            "bb_upper": status["bb_upper"],
            "bb_middle": status["bb_middle"],
            "bb_lower": status["bb_lower"],
            "touched_lower": bool(status["close"] <= status["bb_lower"]),
            "touched_middle": bool(status["close"] >= status["bb_middle"]),
            "touched_upper": bool(status["close"] >= status["bb_upper"]),
            "fresh_entry": bool(status["fresh_entry"] == 1),
            "add_position": bool(status["add_position"] == 1),
            "exit_signal": bool(status["exit_conditions"] == -1),
            "action": action,
            "entries_taken": self.enteries_taken,
        })

    def to_dataframe(self):
        return pd.DataFrame(self.log)

    def save_log(self, path=None):
        """Dump the in-memory bar log to CSV. Meant to be called when the bot stops."""
        if not self.log:
            print(f"[{self.ticker}] no bars logged, nothing to save")
            return None

        path = path or f"{self.ticker}_bar_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        self.to_dataframe().to_csv(path, index=False)
        print(f"[{self.ticker}] saved {len(self.log)} bars to {path}")
        return path

    # ---------- main loop ----------
    def on_event(self, df):
        self.df = df.copy()
        self.add_bollinger_bands()
        self.create_conditions()

        if len(self.df) < 2:
            print(f"[{self.ticker}] not enough bars")
            return self

        bar_time = self.df.index[-1]
        status = self.df.iloc[-1].to_dict()   # last CLOSED bar
        print(f"[{self.ticker}] bar close: {status['close']:.2f} "
              f"(lower {status['bb_lower']:.2f} | mid {status['bb_middle']:.2f} | upper {status['bb_upper']:.2f})")

        if status["close"] <= status["bb_lower"]:
            print(f"[{self.ticker}] touched LOWER band")
        if status["close"] >= status["bb_middle"]:
            print(f"[{self.ticker}] touched/above MIDDLE band")
        if status["close"] >= status["bb_upper"]:
            print(f"[{self.ticker}] touched UPPER band")

        active_positions = broker.net_position(self.ticker)
        action = "NONE"

        # resync if the position was closed outside this loop
        # (MIS auto square-off, manual exit, stop hit)
        if active_positions == 0 and self.enteries_taken > 0:
            print(f"[{self.ticker}] position closed externally, resetting counter")
            self.enteries_taken = 0

        if active_positions > 0:
            print(f"[{self.ticker}] position ONGOING: qty={active_positions}, "
                  f"entries_taken={self.enteries_taken}/{self.max_entries}, last_close={status['close']:.2f}")

            if status["exit_conditions"] == -1:
                print(f"[{self.ticker}] EXIT {active_positions}")
                order_id = broker.sell(self.ticker, active_positions)
                if order_id:
                    self.enteries_taken = 0
                    action = "EXIT"

            elif (self.enabled
                  and self.enteries_taken < self.max_entries
                  and status["add_position"] == 1):
                print(f"[{self.ticker}] ADD rung {self.enteries_taken + 1}")
                order_id = broker.buy(self.ticker, self.quantity)
                if order_id:
                    self.enteries_taken += 1
                    action = "ADD"

        else:
            # Use the raw band touch, not the "fresh_entry" signal state, since
            # `positions` is computed over the full multi-day lookback and can
            # already be stuck at 1 from a touch before this bot session even
            # started (no upper-band close since). active_positions == 0 is
            # already the real guard against double-entering, so requiring
            # fresh_entry on top of it can permanently lock out a flat ticker.
            if self.enabled and status["entry_conditions"] == 1:
                print(f"[{self.ticker}] FRESH ENTRY")
                order_id = broker.buy(self.ticker, self.quantity)
                if order_id:
                    self.enteries_taken = 1
                    action = "ENTRY"

        self._log_bar(bar_time, status, action)

        return self



data = {
    ticker: fetch_historical_data(kite, instrument_token=token, interval=interval)
    for ticker, token in TICKERS.items()
}
for ticker, df in data.items():
    print(f"--- {ticker} ---")
    print(df.tail())



from BarAggregator import BarAggregator

secret = get_zerodha_secret()
kws = KiteTicker(secret["api_key"], secret["access_token"])

instrument_tokens = list(TICKERS.values())
token_to_ticker = {token: ticker for ticker, token in TICKERS.items()}

ba = BarAggregator(instrument_tokens)
ba.prime(kite)

# one strategy instance per ticker, tracking its own entries/log independently
strategies = {
    token: LONGSTRATEGY(ticker, quantity=QUANTITY.get(ticker, 1))
    for ticker, token in TICKERS.items()
}

def refresh_dashboard():
    """Rebuild every ticker's chart from current state and rewrite the
    combined dashboard file."""
    trade_markers = fetch_trade_markers(broker)
    figs = {}
    for token in instrument_tokens:
        ticker = token_to_ticker[token]
        strategy = strategies[token]
        df = strategy.df if strategy.df is not None else ba.state[token]["historical_df"]
        figs[ticker] = make_candle_fig(df, ticker, log=trade_markers[ticker])
    return write_dashboard_html(figs)

# seed the dashboard up front. Locally this opens a browser tab that then
# auto-refreshes (REFRESH_SECONDS) and picks up rewrites below; on a headless
# server there's no browser to open, so just print the path and move on.
dashboard_path = refresh_dashboard()
print(f"Dashboard: {dashboard_path.resolve()}")
try:
    webbrowser.open(dashboard_path.resolve().as_uri())
except webbrowser.Error:
    pass

squared_off_today = False

def square_off_all():
    """Exit every open position across all tracked tickers, regardless of
    broker, and stop the strategies from taking new entries for the rest of
    the day. Reads real broker positions (not the bot's own memory), so this
    also catches positions opened manually - same as the chart markers."""
    global squared_off_today
    squared_off_today = True
    print(f"=== {SQUAREOFF_TIME} cutoff reached, squaring off all positions ===")
    for token, ticker in token_to_ticker.items():
        qty = broker.net_position(ticker)
        if qty > 0:
            print(f"[{ticker}] SQUARE-OFF: selling {qty}")
            broker.sell(ticker, qty)
        strategies[token].enteries_taken = 0
        strategies[token].enabled = False

def on_ticks(ws, ticks):
    updated = False

    if not squared_off_today and datetime.now(tz=IST).time() >= SQUAREOFF_TIME:
        square_off_all()
        updated = True

    for tick in ticks:
        result = ba.create_bar(tick, kite)
        if result is not None:
            token, bar_df = result
            strategies[token].on_event(bar_df)
            updated = True
    if updated:
        refresh_dashboard()

def on_connect(ws, response):
    ws.subscribe(instrument_tokens)

def on_close(ws, code, reason):
    print(f"Connection closed: {code} - {reason}")

kws.on_ticks = on_ticks
kws.on_connect = on_connect
kws.on_close = on_close

# kws.connect() blocks until the ws disconnects (network drop, ws.stop(), or a
# fatal error) and then simply returns - it does not raise on an ordinary
# disconnect. Loop around it so a dropped connection reconnects in-process
# instead of relying on systemd to restart the whole script, which would
# otherwise wipe in-memory state (entries_taken, bar log) for every ticker on
# every disconnect.
try:
    while True:
        kws.connect()
        print("WebSocket disconnected, reconnecting in 5s...")
        sleep(5)
except KeyboardInterrupt:
    print("Bot stopped manually (KeyboardInterrupt)")
finally:
    for strategy in strategies.values():
        strategy.save_log()
    refresh_dashboard()