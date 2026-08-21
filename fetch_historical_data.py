import json
import os
from datetime import datetime, timedelta
from kiteconnect import KiteConnect
import pandas as pd

DEFAULT_TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kite_token.json")

def load_kite_client(path=DEFAULT_TOKEN_PATH):
    with open(path, "r") as f:
        token_data = json.load(f)

    if token_data["generated_date"] != datetime.now().strftime("%Y-%m-%d"):
        raise RuntimeError(
            "Saved access_token is from a previous day and has expired. "
            "Re-run the login flow to generate a new one."
        )

    kite = KiteConnect(api_key=token_data["api_key"])
    kite.set_access_token(token_data["access_token"])
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
