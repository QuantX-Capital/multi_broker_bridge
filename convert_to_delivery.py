"""Standalone: convert every OPEN INTRADAY position to DELIVERY (MIS -> CNC).

Run this manually near the end of the session (e.g. 15:05) to carry the
day's positions overnight instead of having them squared off. It is
completely independent of the trading bot - it reads the broker's live
position book, converts every intraday leg on the account, and re-reads to
confirm.

    python convert_to_delivery.py           # DRY RUN: list open intraday
                                            #   positions, convert nothing
    python convert_to_delivery.py --yes     # actually convert them
    python convert_to_delivery.py --yes --broker mastertrust

Safe by default: without --yes it only shows what it would do.

Notes
-----
* Active broker is taken from config.json ("brokers": {...: 1}); override
  with --broker.
* Converting Intraday -> Delivery surrenders the intraday leverage and
  needs the full delivery value in cash. If the account is short, the
  broker rejects that leg and it is reported as FAILED (still safe - it
  stays intraday).
* This can run alongside the bot. The bot's 15:10 square-off reads only
  the intraday (prd="I") leg, so anything converted here reads as flat to
  it and is left alone; anything that fails to convert is still squared
  off by the bot as usual.
"""

import argparse
import json
import sys
from pathlib import Path

from brokers import NorenBroker, KiteBroker

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


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
        # empty ticker list: this script works off PositionBook, no scrip
        # resolution needed
        return "mastertrust", NorenBroker([])
    if name in ("zerodha", "kite"):
        try:
            from fetch_historical_data import load_kite_client
            return "zerodha", KiteBroker(load_kite_client())
        except Exception as e:
            sys.exit(
                f"could not build a Kite client for the zerodha path ({e}).\n"
                "This script's Zerodha path needs a valid kite_token.json; the "
                "bot itself uses AWS Secrets Manager. If your live broker is "
                "mastertrust, run with: --broker mastertrust"
            )
    sys.exit(f"unknown broker '{name}'")


def list_open_intraday(name, broker):
    """Print current open intraday positions. Returns the count."""
    if name == "mastertrust":
        rows = [p for p in broker._call("PositionBook", tolerate_no_data=True, timeout=20)
                if p.get("prd") == "I" and int(p.get("netqty", 0) or 0) != 0]
        items = [(p["tsym"], int(p["netqty"])) for p in rows]
    else:
        k = broker.kite
        items = [(p["tradingsymbol"], int(p["quantity"]))
                 for p in k.positions()["day"]
                 if p.get("product") == k.PRODUCT_MIS and int(p.get("quantity", 0) or 0) != 0]

    if not items:
        print("No open intraday positions.")
        return 0
    print(f"Open intraday positions ({len(items)}):")
    for tsym, qty in items:
        side = "LONG " if qty > 0 else "SHORT"
        print(f"  {tsym:<20} {side} {abs(qty)}")
    return len(items)


def main():
    ap = argparse.ArgumentParser(description="Convert open intraday positions to delivery (MIS -> CNC).")
    ap.add_argument("--yes", action="store_true",
                    help="actually convert (without this it is a dry run)")
    ap.add_argument("--broker", choices=["mastertrust", "noren", "zerodha", "kite"],
                    help="override the broker from config.json")
    args = ap.parse_args()

    name, broker = pick_broker(args.broker)
    print(f"Broker: {name}")

    if name == "mastertrust":
        print("Checking delivery cash (Limits)...", flush=True)
        cash = broker.delivery_cash()   # capped at a short timeout; may be n/a
        print(f"Delivery cash (Limits): {cash if cash is not None else 'n/a'}")

    count = list_open_intraday(name, broker)
    if count == 0:
        return 0

    if not args.yes:
        print("\nDRY RUN - nothing converted. Re-run with --yes to convert.")
        return 0

    print("\nConverting...")
    results = broker.convert_all_intraday()

    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    print(f"\nConverted {len(ok)}/{len(results)}:")
    for r in results:
        mark = "OK  " if r["ok"] else "FAIL"
        print(f"  [{mark}] {r['tsym']:<20} {r['trantype']} x{r['qty']}")
    if bad:
        print(f"\n{len(bad)} FAILED - still intraday (check margin / broker terminal).")
        return 1
    print("\nAll open intraday positions converted to delivery.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
