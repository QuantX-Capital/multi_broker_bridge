"""Standalone, read-only account check - independent of main.py.

Prints, for the active broker (config.json "brokers"):
  * account balance / margin
  * delivery (CNC) holdings for the tracked tickers
  * current intraday (MIS / prd=I) positions for the tracked tickers

Places no orders and imports nothing from main.py - only the shared
brokers.py module, same as convert_to_delivery.py.

    python account_status.py                    # active broker from config.json
    python account_status.py --broker mastertrust
    python account_status.py --broker zerodha

Credentials:
  * mastertrust -> AWS Secrets Manager, same as the live bot (brokers.py)
  * zerodha     -> local kite_token.json via fetch_historical_data.py
                   (NOT the AWS secret main.py uses - refresh kite_token.json
                   yourself first, e.g. via mastertrust.ipynb, if it's stale)
"""

import argparse
import json
import sys
from pathlib import Path

from brokers import NorenBroker, KiteBroker

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

# Tracked tickers. Keep this in sync with main.py's TICKERS keys.
TICKERS = [
    "JIOFIN", "POWERGRID", "ITC", "TATASTEEL", "BEL",
    "HDFCLIFE", "RELIANCE", "TMPV", "HINDALCO", "BAJFINANCE",
]


def pick_broker(override=None):
    """Return (name, broker_instance). name is 'mastertrust' or 'zerodha'."""
    if override:
        name = override.lower()
    else:
        cfg = json.loads(CONFIG_PATH.read_text())
        enabled = [b for b, flag in cfg.get("brokers", {}).items() if flag == 1]
        if len(enabled) != 1:
            sys.exit(f"config.json 'brokers' must have exactly one set to 1, got: {enabled}")
        name = enabled[0].lower()

    if name in ("mastertrust", "noren"):
        return "mastertrust", NorenBroker(TICKERS)
    if name in ("zerodha", "kite"):
        try:
            from fetch_historical_data import load_kite_client
            return "zerodha", KiteBroker(load_kite_client())
        except Exception as e:
            sys.exit(
                f"could not build a Kite client for the zerodha path ({e}).\n"
                "This script's Zerodha path needs a valid kite_token.json; the "
                "live bot uses AWS Secrets Manager instead. If your live broker "
                "is mastertrust, run with: --broker mastertrust"
            )
    sys.exit(f"unknown broker '{name}'")


# ---------- balance ----------

def show_balance(name, broker):
    print("\n=== ACCOUNT BALANCE ===")
    if name == "zerodha":
        try:
            margins = broker.kite.margins("equity")
        except Exception as e:
            print(f"  could not fetch margins: {e}")
            return
        available = margins.get("available", {})
        utilised = margins.get("utilised", {})
        print(f"  net                 : {margins.get('net')}")
        print(f"  cash                : {available.get('cash')}")
        print(f"  live balance        : {available.get('live_balance')}")
        print(f"  opening balance     : {available.get('opening_balance')}")
        print(f"  collateral          : {available.get('collateral')}")
        print(f"  total utilised debit: {utilised.get('debits')}")
    else:
        # Noren has no single "balance" call - Limits is read per product.
        # Report both delivery (C) and intraday (I) margin buckets.
        for label, prd in (("delivery (C)", "C"), ("intraday (I)", "I")):
            try:
                lim = broker._call("Limits", {"prd": prd, "seg": "EQT", "exch": "NSE"},
                                   timeout=10)
            except Exception as e:
                print(f"  {label}: could not fetch ({e})")
                continue
            cash = next((lim[k] for k in ("cash", "cashmarginavailable") if k in lim), None)
            used = lim.get("marginused")
            print(f"  {label:<14}: cash={cash}  margin used={used}")


# ---------- delivery holdings ----------

def show_delivery(name, broker):
    print("\n=== DELIVERY (CNC) HOLDINGS ===")
    try:
        holdings = broker.delivery_holdings(TICKERS)
    except Exception as e:
        print(f"  could not fetch: {e}")
        return
    if not holdings:
        print("  none")
        return
    for ticker in TICKERS:
        d = holdings.get(ticker)
        if not d:
            continue
        qty = d.get("qty", 0)
        side = "LONG " if qty >= 0 else "SHORT"
        print(f"  {ticker:<12} {side} {abs(qty):<6} avg={d.get('avg_price')}")


# ---------- intraday positions ----------

def show_intraday(name, broker):
    print("\n=== INTRADAY (MIS) POSITIONS ===")
    any_open = False
    for ticker in TICKERS:
        try:
            qty = broker.net_position(ticker)
        except Exception as e:
            print(f"  {ticker:<12} could not fetch ({e})")
            continue
        if qty == 0:
            continue
        any_open = True
        side = "LONG " if qty > 0 else "SHORT"
        print(f"  {ticker:<12} {side} {abs(qty)}")
    if not any_open:
        print("  none - flat on all tracked tickers")


def main():
    ap = argparse.ArgumentParser(description="Read-only account/position check (no orders placed).")
    ap.add_argument("--broker", choices=["mastertrust", "noren", "zerodha", "kite"],
                    help="override the broker from config.json")
    args = ap.parse_args()

    name, broker = pick_broker(args.broker)
    print(f"Broker: {name}")
    print(f"Tickers: {', '.join(TICKERS)}")

    show_balance(name, broker)
    show_delivery(name, broker)
    show_intraday(name, broker)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
