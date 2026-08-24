from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
from fetch_historical_data import fetch_historical_data

IST = ZoneInfo("Asia/Kolkata")


class BarAggregator:

    def __init__(self, instrument_tokens):
        self.state = {
            token: {
                "previous_minute": None,
                "highest_close": None,
                "lowest_close": None,
                "open": None,
                "close": None,
                "open_volume": None,
                "close_volume": None,
                "bar_start_time": None,
                "historical_df": None,
            }
            for token in instrument_tokens
        }

    def prime(self, kite):
        """Eagerly fetch historical bars for every tracked instrument so the
        chart has data to show before the first live tick/boundary arrives."""
        for token, s in self.state.items():
            if s["historical_df"] is None:
                s["historical_df"] = fetch_historical_data(kite, token, interval="5minute").iloc[:-1]

    def create_bar(self, tick, kite):
        if "last_price" not in tick:
            return None

        token = tick["instrument_token"]
        if token not in self.state:
            return None

        s = self.state[token]
        current_ltp = float(tick["last_price"])
        dt = datetime.now(tz=IST)
        current_minute = dt.strftime("%M")
        avoid_time = dt.strftime("%H-%M")
        s["close_volume"] = tick["volume_traded"]

        # First tick ever for this instrument
        if s["previous_minute"] is None:
            print("Bot Started")
            s["previous_minute"] = current_minute
            s["bar_start_time"] = dt.replace(second=0, microsecond=0)
            s["open"] = s["highest_close"] = s["lowest_close"] = s["close"] = current_ltp
            s["open_volume"] = tick["volume_traded"]
            return None

        if current_minute != s["previous_minute"] and int(current_minute) % 5 == 0 and avoid_time != "09-15":
            # Fetch historical data at the first bar boundary so it includes all bars up to this point
            if s["historical_df"] is None:
                s["historical_df"] = fetch_historical_data(kite, token, interval="5minute").iloc[:-1]
                s["previous_minute"] = current_minute
                s["bar_start_time"] = dt.replace(second=0, microsecond=0)
                s["open"] = s["highest_close"] = s["lowest_close"] = s["close"] = current_ltp
                s["open_volume"] = tick["volume_traded"]
                return token, s["historical_df"]

            volume = s["close_volume"] - s["open_volume"]

            new_bar = pd.DataFrame(
                [[s["bar_start_time"], s["open"], s["highest_close"], s["lowest_close"], s["close"], volume]],
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            ).set_index("timestamp")

            s["historical_df"] = pd.concat([s["historical_df"], new_bar])

            # Start NEW bar
            s["previous_minute"] = current_minute
            s["bar_start_time"] = dt.replace(second=0, microsecond=0)
            s["open"] = s["highest_close"] = s["lowest_close"] = s["close"] = current_ltp
            s["open_volume"] = tick["volume_traded"]
            return token, s["historical_df"]
        else:
            # Update current bar
            s["highest_close"] = max(s["highest_close"], current_ltp)
            s["lowest_close"] = min(s["lowest_close"], current_ltp)
            s["close"] = current_ltp
            return None
