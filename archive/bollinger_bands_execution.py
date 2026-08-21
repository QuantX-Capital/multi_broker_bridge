import json
import os
import webbrowser
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import boto3
from dotenv import load_dotenv
from kiteconnect import KiteConnect, KiteTicker
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import talib
import requests

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
ZERODHA_SECRET_ID = "/trading/brokers/zerodha/luv"

CHARTS_DIR = Path("charts")
# Browser tab reload cadence. Independent of the bar interval - the HTML file
# itself is only rewritten when a bar actually closes, this just controls how
# soon the open tab picks up a rewritten file.
REFRESH_SECONDS = 60

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
    secret.setdefault("api_key", os.getenv("zerodha_api_key"))
    if not secret.get("api_key") or not secret.get("access_token"):
        raise RuntimeError(
            f"Zerodha secret '{ZERODHA_SECRET_ID}' is missing api_key/access_token "
            "(and no zerodha_api_key env var fallback was found)."
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
data = {
    ticker: fetch_historical_data(kite, instrument_token=token, interval=interval)
    for ticker, token in TICKERS.items()
}
for ticker, df in data.items():
    print(f"--- {ticker} ---")
    print(df.head())



def make_candle_fig(df, title, log=None):
    """Candlestick + Bollinger bands, with ENTRY/ADD/EXIT markers if a bar log is given.
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
            ("ENTRY", "green", "triangle-up", "low", 0.995),
            ("ADD", "limegreen", "triangle-up", "low", 0.99),
            ("EXIT", "red", "triangle-down", "high", 1.005),
        ]:
            times = log_df.index[log_df["action"] == action].intersection(df_bb.index)
            if len(times):
                fig.add_trace(go.Scatter(
                    x=times, y=df_bb.loc[times, price_col] * offset,
                    mode="markers", marker=dict(symbol=symbol, size=12, color=color),
                    name=action,
                ))

    fig.update_layout(title=title, xaxis_rangeslider_visible=False, template="plotly_white")
    return fig


def write_chart_html(fig, ticker):
    """Overwrite this ticker's chart file. The already-open browser tab picks
    up the new content on its next auto-refresh (see REFRESH_SECONDS)."""
    CHARTS_DIR.mkdir(exist_ok=True)
    path = CHARTS_DIR / f"{ticker}.html"
    html = fig.to_html(include_plotlyjs="cdn", full_html=True)
    html = html.replace("<head>", f'<head>\n<meta http-equiv="refresh" content="{REFRESH_SECONDS}">', 1)
    path.write_text(html, encoding="utf-8")
    return path




def place_order(ticker, quantity, transaction_type):
    order_id = kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=kite.EXCHANGE_NSE,
        tradingsymbol=ticker.upper(),
        transaction_type=transaction_type,
        quantity=quantity,
        product=kite.PRODUCT_MIS,
        order_type=kite.ORDER_TYPE_MARKET,
        validity=kite.VALIDITY_DAY,
        market_protection=-1
    )

    return order_id


def place_buy_order(ticker, quantity):
    return place_order(
        ticker=ticker,
        quantity=quantity,
        transaction_type=kite.TRANSACTION_TYPE_BUY
    )


def square_off(ticker, quantity):
    return place_order(
        ticker=ticker,
        quantity=quantity,
        transaction_type=kite.TRANSACTION_TYPE_SELL
    )


def positions(ticker):
    daily_positions = kite.positions()["day"]
    if not daily_positions:
        return 0

    positions_df = pd.DataFrame(daily_positions)[["tradingsymbol", "instrument_token", "product", "quantity", "last_price"]]
    positions_df = positions_df[positions_df["tradingsymbol"] == ticker.upper()]

    if positions_df.empty:
        return 0

    return positions_df["quantity"].to_list()[0]


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

        active_positions = positions(self.ticker)
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
                square_off(self.ticker, active_positions)
                # TODO: confirm fill before resetting
                self.enteries_taken = 0
                action = "EXIT"

            elif (self.enteries_taken < self.max_entries
                  and status["add_position"] == 1):
                print(f"[{self.ticker}] ADD rung {self.enteries_taken + 1}")
                place_buy_order(self.ticker, self.quantity)
                # TODO: confirm fill before incrementing
                self.enteries_taken += 1
                action = "ADD"

        else:
            # Use the raw band touch, not the "fresh_entry" signal state, since
            # `positions` is computed over the full multi-day lookback and can
            # already be stuck at 1 from a touch before this bot session even
            # started (no upper-band close since). active_positions == 0 is
            # already the real guard against double-entering, so requiring
            # fresh_entry on top of it can permanently lock out a flat ticker.
            if status["entry_conditions"] == 1:
                print(f"[{self.ticker}] FRESH ENTRY")
                place_buy_order(self.ticker, self.quantity)
                # TODO: confirm fill before setting
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

def update_chart(ticker, df, log=None):
    """(Re)write this ticker's chart file with the latest bars/signals."""
    fig = make_candle_fig(df, ticker, log=log)
    return write_chart_html(fig, ticker)

# seed one chart per ticker up front and open each in a browser tab; every
# tab then auto-refreshes (REFRESH_SECONDS) and picks up rewrites below
for token in instrument_tokens:
    ticker = token_to_ticker[token]
    path = update_chart(ticker, ba.state[token]["historical_df"])
    webbrowser.open(path.resolve().as_uri())

def on_ticks(ws, ticks):
    for tick in ticks:
        result = ba.create_bar(tick, kite)
        if result is not None:
            token, bar_df = result
            ticker = token_to_ticker[token]
            strategy = strategies[token].on_event(bar_df)
            update_chart(ticker, strategy.df, strategy.log)

def on_connect(ws, response):
    ws.subscribe(instrument_tokens)

def on_close(ws, code, reason):
    ws.stop()

kws.on_ticks = on_ticks
kws.on_connect = on_connect
kws.on_close = on_close

# kws.connect() blocks until the ws is stopped (manual interrupt, ws.stop(), or a
# fatal error). Whenever that happens, treat it as "the bot stopped" and flush
# whatever bars are sitting in memory to CSV for every ticker.
try:
    kws.connect()
except KeyboardInterrupt:
    print("Bot stopped manually (KeyboardInterrupt)")
finally:
    for strategy in strategies.values():
        strategy.save_log()
        if strategy.df is not None and strategy.log:
            update_chart(strategy.ticker, strategy.df, strategy.log)