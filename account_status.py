"""Standalone, read-only account check - independent of main.py.

Prints, for the active broker (config.json "brokers"):
  * account balance / margin
  * delivery (CNC) holdings for the tracked tickers
  * current intraday (MIS / prd=I) positions for the tracked tickers

Read-only by default and imports nothing from main.py - only the shared
brokers.py module, same as convert_to_delivery.py. --exit-all --yes is the
one path that places orders (see below).

    python account_status.py                    # active broker from config.json
    python account_status.py --broker mastertrust
    python account_status.py --broker zerodha
    python account_status.py --orders            # today's order book, status per order
    python account_status.py --all-delivery      # full-account delivery book, tagged
    python account_status.py --exit-all          # DRY RUN: show exit plan only
    python account_status.py --exit-all --yes    # place the exit orders, then
                                                  # re-check what's left open

Credentials:
  * mastertrust -> AWS Secrets Manager, same as the live bot (brokers.py)
  * zerodha     -> local kite_token.json via fetch_historical_data.py
                   (NOT the AWS secret main.py uses - refresh kite_token.json
                   yourself first, e.g. via mastertrust.ipynb, if it's stale)
"""

import argparse
import json
import sys
import time
from pathlib import Path

from brokers import NorenBroker, KiteBroker, _num_or

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


# ---------- order status (read-only, no orders placed) ----------

def show_orders(name, broker):
    """Today's order book for the tracked tickers, split into delivery
    (CNC/prd=C) and intraday (MIS/prd=I) orders - status, filled qty, avg
    price, reject reason if any. Read-only: places or modifies nothing.
    Use this to see what an order actually did (filled / rejected / still
    open) rather than inferring it from the resulting position."""
    print("\n=== ORDER STATUS (today) ===")
    tracked = set(TICKERS)

    if name == "zerodha":
        k = broker.kite
        try:
            orders = k.orders()
        except Exception as e:
            print(f"  could not read order book: {e}")
            return
        rows = [o for o in orders if o.get("tradingsymbol", "").upper() in tracked]
        delivery = [o for o in rows if o.get("product") == k.PRODUCT_CNC]
        intraday = [o for o in rows if o.get("product") == k.PRODUCT_MIS]

        def _p(o):
            print(f"  {o.get('tradingsymbol'):<12} {o.get('transaction_type'):<4} "
                  f"qty={o.get('quantity')} filled={o.get('filled_quantity')} "
                  f"avg={o.get('average_price')} status={o.get('status')} "
                  f"{o.get('status_message') or ''} id={o.get('order_id')}")
    else:
        try:
            book = broker._call("OrderBook", tolerate_no_data=True, timeout=20)
        except Exception as e:
            print(f"  could not read order book: {e}")
            return
        rows = []
        for o in book:
            tsym = o.get("tsym", "")
            base = tsym[:-3] if tsym.endswith("-EQ") else tsym
            if base.upper() in tracked:
                rows.append(o)
        delivery = [o for o in rows if o.get("prd") == "C"]
        intraday = [o for o in rows if o.get("prd") == "I"]

        def _p(o):
            filled = o.get("fillshares") or o.get("flqty") or 0
            print(f"  {o.get('tsym'):<14} {o.get('trantype'):<4} "
                  f"qty={o.get('qty')} filled={filled} avg={o.get('avgprc')} "
                  f"status={o.get('status')} {o.get('rejreason', '')} id={o.get('norenordno')}")

    print("\n  -- DELIVERY (holding account) ORDERS --")
    if delivery:
        for o in delivery:
            _p(o)
    else:
        print("  none today")

    print("\n  -- INTRADAY ORDERS --")
    if intraday:
        for o in intraday:
            _p(o)
    else:
        print("  none today")


# ---------- full-account delivery book (not just the tracked tickers) ----------

def show_all_delivery(name, broker):
    """Every CNC/delivery holding on the whole account, tagged as tracked or
    untracked - so you can verify exit_all() only ever touched TICKERS and
    nothing else on the account moved. brokers.py's delivery_holdings() takes
    a ticker filter and can't answer this, so this reaches into the broker
    directly (mirrors delivery_holdings()'s own logic, just unfiltered)."""
    print("\n=== ALL DELIVERY (CNC) HOLDINGS ON ACCOUNT ===")
    out = {}
    tracked = set(TICKERS)

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
        return out

    for sym in sorted(out):
        base = sym[:-3] if sym.endswith("-EQ") else sym
        d = out[sym]
        qty = d["qty"]
        side = "LONG " if qty >= 0 else "SHORT"
        tag = "" if base.upper() in tracked else "  <- UNTRACKED (never touched by exit_all)"
        print(f"  {sym:<14} {side} {abs(qty):<6} avg={d.get('avg_price')}{tag}")
    return out


# ---------- exit everything ----------

def exit_delivery_leg(name, broker, ticker, qty):
    """Place one CNC/delivery exit order. qty > 0 sells a long delivery
    holding; qty < 0 buys to cover a delivery short. Returns the order id,
    or None if the broker didn't return one.

    brokers.py has no public delivery-sell method (buy/sell there are always
    intraday), so this reaches into each broker's underlying client directly
    - same as show_balance() already does via broker._call()."""
    if name == "zerodha":
        k = broker.kite
        txn = k.TRANSACTION_TYPE_SELL if qty > 0 else k.TRANSACTION_TYPE_BUY
        return k.place_order(
            variety=k.VARIETY_REGULAR,
            exchange=k.EXCHANGE_NSE,
            tradingsymbol=ticker.upper(),
            transaction_type=txn,
            quantity=abs(qty),
            product=k.PRODUCT_CNC,
            order_type=k.ORDER_TYPE_MARKET,
            validity=k.VALIDITY_DAY,
            market_protection=-1,
        )
    else:
        side = "S" if qty > 0 else "B"
        price = broker._marketable_price(ticker, side)
        result = broker._call("PlaceOrder", {
            "exch": "NSE",
            "tsym": broker.scrips[ticker]["tsym"],
            "qty": str(abs(qty)),
            "prc": str(price),
            "prd": "C",
            "trantype": side,
            "prctyp": "LMT",
            "ret": "DAY",
        })
        return result.get("norenordno")


def exit_all(name, broker, confirm):
    """Exit every open intraday and delivery position for the tracked
    tickers. Dry run by default (prints the plan, sends nothing) - pass
    confirm=True (--yes) to actually place the orders."""
    print("\n=== EXIT ALL (intraday + delivery) ===")

    try:
        delivered = broker.delivery_holdings(TICKERS)
    except Exception as e:
        print(f"  could not read delivery holdings: {e}")
        delivered = {}

    plan = []   # (ticker, leg, qty) - qty > 0 long, qty < 0 short
    for ticker in TICKERS:
        try:
            iq = broker.net_position(ticker)
        except Exception as e:
            print(f"  {ticker:<12} could not read intraday position ({e})")
            iq = 0
        if iq:
            plan.append((ticker, "intraday", iq))
        d = delivered.get(ticker)
        if d and d.get("qty"):
            plan.append((ticker, "delivery", int(d["qty"])))

    if not plan:
        print("  nothing to exit - flat on all tracked tickers")
        return

    tag = "[LIVE]" if confirm else "[DRY] "
    for ticker, leg, qty in plan:
        action = "SELL" if qty > 0 else "BUY (cover)"
        print(f"  {tag} {ticker:<12} {leg:<9} {action} {abs(qty)}")

    if not confirm:
        print("\n  DRY RUN - nothing sent. Re-run with --exit-all --yes to place these orders.")
        return

    print("\n  placing orders...")
    for ticker, leg, qty in plan:
        try:
            if leg == "intraday":
                order_id = broker.sell(ticker, qty) if qty > 0 else broker.buy(ticker, abs(qty))
            else:
                order_id = exit_delivery_leg(name, broker, ticker, qty)
            print(f"  {ticker:<12} {leg:<9} -> order {order_id or 'FAILED / no order id'}")
        except Exception as e:
            print(f"  {ticker:<12} {leg:<9} -> ERROR: {e}")

    # Broker position/holdings reads can lag a couple seconds behind a
    # just-placed order (same reason NorenBroker._convert_intraday_leg polls
    # before trusting PositionBook) - give it a moment before re-checking.
    print("\n  waiting for the broker to reflect fills...")
    time.sleep(3)
    print("\n=== POSITIONS AFTER EXIT ===")
    show_intraday(name, broker)
    show_all_delivery(name, broker)


def main():
    ap = argparse.ArgumentParser(description="Account/position check, with an optional exit-all (no orders placed by default).")
    ap.add_argument("--broker", choices=["mastertrust", "noren", "zerodha", "kite"],
                    help="override the broker from config.json")
    ap.add_argument("--exit-all", action="store_true",
                    help="exit every open intraday and delivery position for the tracked tickers "
                         "(dry run unless --yes is also given)")
    ap.add_argument("--yes", action="store_true",
                    help="with --exit-all, actually place the exit orders instead of a dry run")
    ap.add_argument("--all-delivery", action="store_true",
                    help="also show every CNC/delivery holding on the whole account "
                         "(not just the tracked tickers), tagged tracked/untracked - read-only")
    ap.add_argument("--orders", action="store_true",
                    help="also show today's order book for the tracked tickers, split "
                         "into delivery/holding and intraday, with status per order - read-only")
    args = ap.parse_args()

    name, broker = pick_broker(args.broker)
    print(f"Broker: {name}")
    print(f"Tickers: {', '.join(TICKERS)}")

    show_balance(name, broker)
    show_delivery(name, broker)
    show_intraday(name, broker)

    if args.all_delivery:
        show_all_delivery(name, broker)
    if args.orders:
        show_orders(name, broker)

    if args.exit_all:
        exit_all(name, broker, confirm=args.yes)

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
