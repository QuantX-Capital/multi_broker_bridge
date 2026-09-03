"""Standalone account check - independent of main.py.

Read-only, for the whole account (not limited to any particular ticker list):
  * every open intraday (MIS / prd=I) position
  * every demat / delivery (CNC / prd=C) holding

Plus one action, scoped to exactly EXIT_TICKERS below and nothing else:
  * exit_delivery - sell an existing CNC/delivery holding (plain prd=C sell,
    per Mastertrust: this needs no OTP/TPIN, and a "short"/negative day-net
    reading right after is the normal look of a filled exit, not a problem -
    the demat debit itself just runs at end of day). No buy-side "cover"
    logic - this only ever sells an existing long holding.

    python account_status.py                       # read-only status
    python account_status.py --broker mastertrust
    python account_status.py --broker zerodha
    python account_status.py --exit-delivery        # DRY RUN: show exit plan
    python account_status.py --exit-delivery --yes  # place the sell orders

Credentials:
  * mastertrust -> AWS Secrets Manager, same as the live bot (brokers.py)
  * zerodha     -> local kite_token.json via fetch_historical_data.py
                   (NOT the AWS secret main.py uses - refresh kite_token.json
                   yourself first if it's stale)
"""

import argparse
import json
import sys
from pathlib import Path

from brokers import NorenBroker, KiteBroker, _num_or

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

# exit_delivery() touches ONLY these tickers - nothing else on the account.
# Currently: every tracked delivery holding EXCEPT ETERNAL and HFCL.
EXIT_TICKERS = ["BAJFINANCE", "BEL", "HDFCLIFE", "HINDALCO", "JIOFIN",
                "POWERGRID", "RELIANCE", "TATASTEEL", "TMPV"]


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
        # scrips are only resolved for EXIT_TICKERS - the account-wide read
        # functions below don't need scrip resolution, they read raw
        # PositionBook/Holdings rows directly.
        return "mastertrust", NorenBroker(EXIT_TICKERS)
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


def show_all_intraday(name, broker):
    """Every open intraday (MIS/prd=I) position on the whole account."""
    print("\n=== ALL INTRADAY (MIS) POSITIONS ===")
    rows = []
    if name == "zerodha":
        k = broker.kite
        try:
            for p in k.positions()["day"]:
                if p.get("product") != k.PRODUCT_MIS:
                    continue
                q = int(p.get("quantity", 0) or 0)
                if q:
                    rows.append((p["tradingsymbol"], q,
                                round(float(p.get("average_price", 0) or 0), 2) or None))
        except Exception as e:
            print(f"  could not read positions: {e}")
            return
    else:
        try:
            for p in broker._call("PositionBook", tolerate_no_data=True, timeout=20):
                if p.get("prd") != "I":
                    continue
                q = int(p.get("netqty", 0) or 0)
                if q:
                    rows.append((p.get("tsym", ""), q,
                                broker._row_price(p, "B" if q > 0 else "S")))
        except Exception as e:
            print(f"  could not read PositionBook: {e}")
            return

    if not rows:
        print("  none - flat on the whole account")
        return
    for sym, qty, avg in sorted(rows):
        side = "LONG " if qty >= 0 else "SHORT"
        print(f"  {sym:<14} {side} {abs(qty):<6} avg={avg}")


def show_all_delivery(name, broker):
    """Every demat / delivery (CNC/prd=C) holding on the whole account -
    same-day converts plus settled holdings."""
    print("\n=== ALL DELIVERY (DEMAT/CNC) HOLDINGS ===")
    out = {}

    if name == "zerodha":
        k = broker.kite
        try:
            for p in k.positions()["day"]:
                if p.get("product") != k.PRODUCT_CNC:
                    continue
                q = int(p.get("quantity", 0) or 0)
                if q:
                    out[p["tradingsymbol"]] = {
                        "qty": q,
                        "avg_price": round(float(p.get("average_price", 0) or 0), 2) or None,
                    }
        except Exception as e:
            print(f"  could not read CNC day positions: {e}")
        try:
            for h in k.holdings():
                sym = h.get("tradingsymbol", "")
                q = int(h.get("quantity", 0) or 0)
                if q and sym not in out:
                    out[sym] = {
                        "qty": q,
                        "avg_price": round(float(h.get("average_price", 0) or 0), 2) or None,
                    }
        except Exception as e:
            print(f"  could not read holdings: {e}")
    else:
        try:
            for p in broker._call("PositionBook", tolerate_no_data=True, timeout=20):
                if p.get("prd") != "C":
                    continue
                q = int(p.get("netqty", 0) or 0)
                if q:
                    out[p.get("tsym", "")] = {
                        "qty": q,
                        "avg_price": broker._row_price(p, "B" if q > 0 else "S"),
                    }
        except Exception as e:
            print(f"  could not read PositionBook: {e}")
        try:
            book = broker._call("Holdings", {"prd": "C"}, tolerate_no_data=True, timeout=12)
            for h in (book if isinstance(book, list) else []):
                exch = h.get("exch_tsym") or []
                sym = exch[0].get("tsym", "") if exch else ""
                if not sym or sym in out:
                    continue
                hold = max((_num_or(h.get(k2), 0) for k2 in ("holdqty", "dpqty", "npoadqty")), default=0)
                qty = int(hold) - int(_num_or(h.get("usedqty"), 0))
                if qty > 0:
                    out[sym] = {"qty": qty, "avg_price": _num_or(h.get("upldprc"), None)}
        except Exception as e:
            print(f"  could not read Holdings: {e}")

    if not out:
        print("  none")
        return
    for sym in sorted(out):
        d = out[sym]
        qty = d["qty"]
        side = "LONG " if qty >= 0 else "SHORT"
        print(f"  {sym:<14} {side} {abs(qty):<6} avg={d.get('avg_price')}")


# ---------- exit delivery (scoped to EXIT_TICKERS only) ----------

def _sellable_qty(broker, tsym):
    """Settled, sellable-today quantity for one Noren scrip, per
    Mastertrust's own Holdings formula:
        holdqty + unplgdqty + benqty + dpqty - usedqty
    Deliberately excludes btstqty: those shares were bought yesterday and
    haven't actually settled into demat yet (T+1). Selling them is a BTST
    trade - if the counterparty to that earlier buy defaults at pay-in, the
    shares go to auction and you eat the penalty. This function only ever
    returns what's genuinely settled and safe to sell. Returns None if the
    check itself fails (network/timeout) - caller should treat that as
    "can't verify, don't sell", not as zero."""
    try:
        rows = broker._call("Holdings", {"prd": "C"}, tolerate_no_data=True, timeout=12)
    except Exception:
        return None
    for r in rows:
        if r.get("stat") not in (None, "Ok"):   # skip error/malformed rows
            continue
        nse = next((e for e in (r.get("exch_tsym") or []) if e.get("exch") == "NSE"), None)
        if not nse or nse.get("tsym") != tsym:
            continue
        g = lambda k: int(_num_or(r.get(k), 0))
        qty = g("holdqty") + g("unplgdqty") + g("benqty") + g("dpqty") - g("usedqty")
        return max(qty, 0)
    return 0


def _sell_delivery(name, broker, ticker, qty):
    """Place one plain CNC/delivery sell order. qty must be > 0 - this only
    ever sells an existing long holding, no buy-side 'cover' branch (that
    logic was the earlier mistake). Returns the order id, or None."""
    if name == "zerodha":
        k = broker.kite
        return k.place_order(
            variety=k.VARIETY_REGULAR,
            exchange=k.EXCHANGE_NSE,
            tradingsymbol=ticker.upper(),
            transaction_type=k.TRANSACTION_TYPE_SELL,
            quantity=qty,
            product=k.PRODUCT_CNC,
            order_type=k.ORDER_TYPE_MARKET,
            validity=k.VALIDITY_DAY,
            market_protection=-1,
        )
    else:
        price = broker._marketable_price(ticker, "S")
        result = broker._call("PlaceOrder", {
            "exch": "NSE",
            "tsym": broker.scrips[ticker]["tsym"],
            "qty": str(qty),
            "prc": str(price),
            "prd": "C",
            "trantype": "S",
            "prctyp": "LMT",
            "ret": "DAY",
        })
        return result.get("norenordno")


def exit_delivery(name, broker, confirm):
    """Sell the current CNC/delivery holding for exactly EXIT_TICKERS - no
    other symbol on the account is read or touched. Dry run by default;
    pass confirm=True (--yes) to actually place the orders."""
    print(f"\n=== EXIT DELIVERY ({', '.join(EXIT_TICKERS)}) ===")

    try:
        holdings = broker.delivery_holdings(EXIT_TICKERS)
    except Exception as e:
        print(f"  could not read delivery holdings: {e}")
        return

    plan = []
    for ticker in EXIT_TICKERS:
        d = holdings.get(ticker)
        reported = int(d["qty"]) if d else 0
        if reported < 0:
            print(f"  {ticker:<12} shows {reported} (already short?) - skipping, needs manual look")
            continue
        if reported == 0:
            print(f"  {ticker:<12} nothing held - skipping")
            continue

        if name == "mastertrust":
            tsym = broker.scrips[ticker]["tsym"]
            sellable = _sellable_qty(broker, tsym)
            if sellable is None:
                print(f"  {ticker:<12} could not verify settled/sellable qty - skipping for safety")
                continue
            if sellable < reported:
                print(f"  {ticker:<12} reported {reported} but only {sellable} is settled/sellable "
                      f"today (rest is BTST or already used) - selling {sellable} only")
            qty = sellable
        else:
            qty = reported   # Kite's holdings() qty is already settled-only, see brokers.py

        if qty > 0:
            plan.append((ticker, qty))
        else:
            print(f"  {ticker:<12} nothing settled/sellable right now - skipping")

    if not plan:
        print("  nothing to exit")
        return

    tag = "[LIVE]" if confirm else "[DRY] "
    for ticker, qty in plan:
        print(f"  {tag} {ticker:<12} SELL {qty}")

    if not confirm:
        print("\n  DRY RUN - nothing sent. Re-run with --exit-delivery --yes to place these orders.")
        return

    print("\n  placing orders...")
    for ticker, qty in plan:
        try:
            order_id = _sell_delivery(name, broker, ticker, qty)
            print(f"  {ticker:<12} -> order {order_id or 'FAILED / no order id'}")
        except Exception as e:
            print(f"  {ticker:<12} -> ERROR: {e}")

    print("\n  Note: a 'short'/negative day-net reading for these tickers right\n"
          "  after this is the normal look of a filled CNC exit (per Mastertrust) -\n"
          "  the demat debit itself runs at end of day. That is expected, not a problem.")


def main():
    ap = argparse.ArgumentParser(
        description="Read-only: list all intraday positions and all demat/delivery holdings.")
    ap.add_argument("--broker", choices=["mastertrust", "noren", "zerodha", "kite"],
                    help="override the broker from config.json")
    ap.add_argument("--exit-delivery", action="store_true",
                    help=f"sell the current CNC/delivery holding for {', '.join(EXIT_TICKERS)} "
                         "only (dry run unless --yes is also given)")
    ap.add_argument("--yes", action="store_true",
                    help="with --exit-delivery, actually place the sell orders instead of a dry run")
    args = ap.parse_args()

    name, broker = pick_broker(args.broker)
    print(f"Broker: {name}")

    show_all_intraday(name, broker)
    show_all_delivery(name, broker)

    if args.exit_delivery:
        exit_delivery(name, broker, confirm=args.yes)

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
