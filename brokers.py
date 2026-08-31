"""Broker abstraction: same execution interface over Zerodha Kite and Mastertrust Noren.

The strategy only needs three things from a broker:
    buy(ticker, qty)          -> order id or None (order not placed)
    sell(ticker, qty)         -> order id or None
    net_position(ticker)      -> signed int net intraday quantity

Switch implementations via make_broker("zerodha" | "mastertrust", ...).

Noren-specific behavior baked in:
  - MKT orders are blocked by the broker's ALGO_CHK for API flow, so all
    orders go out as marketable LMT: LTP +/- a buffer, snapped to tick size.
  - Buys run a GetOrderMargin pre-check and are skipped (return None) on
    "Insufficient Balance". Sells (square-offs) never pre-check - an exit
    must always be attempted.
  - After placing, SingleOrdStatus is polled a couple of times; if the order
    is not COMPLETE we log loudly but do NOT cancel/repost (yet).
"""

import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
import requests

from logger import event, delivery_log

IST = ZoneInfo("Asia/Kolkata")

NOREN_SECRET_ID = "/trading/brokers/mastertrust/vaibhav"
# Local dev uses a named AWS CLI profile; on EC2 there's no such profile, so
# leave NOREN_AWS_PROFILE unset there and boto3 falls back to the instance's
# attached IAM role instead.
NOREN_AWS_PROFILE = os.environ.get("NOREN_AWS_PROFILE")
NOREN_AWS_REGION = "ap-south-1"
NOREN_BASE_URL = "https://midlive.mastertrust.co.in/NorenWClientAPI/"

# marketable-LMT buffer: how far past LTP the limit is priced to behave like MKT
LMT_BUFFER = 0.002  # 0.2%

# fill confirmation polling (keep short: this runs on the tick callback thread)
FILL_POLL_ATTEMPTS = 2
FILL_POLL_SLEEP = 1.5  # seconds


def _fetch_secret(secret_id, profile=None, region=None):
    """Fetch a broker secret and enforce the daily-refresh check."""
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    client = session.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_id)

    updated_date = response["CreatedDate"].astimezone(IST).date()
    if updated_date != datetime.now(tz=IST).date():
        raise RuntimeError(
            f"Secret '{secret_id}' was last updated on {updated_date}, not today. "
            "The access_token has likely expired - refresh it before running."
        )
    return json.loads(response["SecretString"])


class NorenError(RuntimeError):
    pass


class KiteBroker:
    """Thin wrapper over the existing Kite execution path (MKT orders)."""

    def __init__(self, kite):
        self.kite = kite

    def _place(self, ticker, qty, transaction_type):
        return self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR,
            exchange=self.kite.EXCHANGE_NSE,
            tradingsymbol=ticker.upper(),
            transaction_type=transaction_type,
            quantity=qty,
            product=self.kite.PRODUCT_MIS,
            order_type=self.kite.ORDER_TYPE_MARKET,
            validity=self.kite.VALIDITY_DAY,
            market_protection=-1,
        )

    def buy(self, ticker, qty):
        return self._place(ticker, qty, self.kite.TRANSACTION_TYPE_BUY)

    def sell(self, ticker, qty):
        return self._place(ticker, qty, self.kite.TRANSACTION_TYPE_SELL)

    def net_position(self, ticker):
        # Intraday (MIS) leg only, so a position converted to CNC reads as 0
        # here and the 15:10 square-off correctly leaves it alone.
        for p in self.kite.positions()["day"]:
            if (p["tradingsymbol"] == ticker.upper()
                    and p["product"] == self.kite.PRODUCT_MIS):
                return int(p["quantity"])
        return 0

    def _convert_kite_pos(self, p, ticker=None, source="auto"):
        """Convert one live MIS day position dict to CNC. On success writes a
        delivery-log record (ticker, side, qty, avg entry price, LTP).
        Returns (ok, avg_price)."""
        sym = p["tradingsymbol"]
        netqty = int(p.get("quantity", 0) or 0)
        if netqty == 0:
            return False, None
        side = "B" if netqty > 0 else "S"
        avg = round(float(p.get("average_price", 0) or 0), 2) or None
        ltp = round(float(p.get("last_price", 0) or 0), 2) or None
        try:
            self.kite.convert_position(
                exchange=p.get("exchange", self.kite.EXCHANGE_NSE),
                tradingsymbol=sym,
                transaction_type=(self.kite.TRANSACTION_TYPE_BUY if netqty > 0
                                  else self.kite.TRANSACTION_TYPE_SELL),
                position_type="day",
                quantity=abs(netqty),
                old_product=self.kite.PRODUCT_MIS,
                new_product=self.kite.PRODUCT_CNC,
            )
        except Exception as e:
            event(f"[kite] !! {ticker or sym} convert_position failed: {e}")
            return False, avg
        delivery_log({
            "time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            "ticker": ticker or sym,
            "tsym": sym,
            "side": side,
            "qty": abs(netqty),
            "avg_price": avg,
            "ltp": ltp,
            "source": source,
        })
        return True, avg

    def convert_to_delivery(self, ticker, source="auto"):
        """Convert this ticker's open intraday (MIS) day position to delivery
        (CNC). Returns True if the conversion call was accepted."""
        sym = ticker.upper()
        for p in self.kite.positions()["day"]:
            if p["tradingsymbol"] == sym and p["product"] == self.kite.PRODUCT_MIS:
                return self._convert_kite_pos(p, ticker=ticker, source=source)[0]
        return False

    def convert_all_intraday(self, source="script"):
        """Convert EVERY open intraday (MIS) day position to delivery (CNC).
        Returns a list of {tsym, trantype, qty, avg_price, ok} dicts."""
        results = []
        for p in self.kite.positions()["day"]:
            netqty = int(p.get("quantity", 0) or 0)
            if p.get("product") != self.kite.PRODUCT_MIS or netqty == 0:
                continue
            ok, avg = self._convert_kite_pos(p, source=source)
            results.append({
                "tsym": p["tradingsymbol"],
                "trantype": "B" if netqty > 0 else "S",
                "qty": abs(netqty),
                "avg_price": avg,
                "ok": ok,
            })
        return results

    def delivery_holdings(self, tickers):
        """Current delivery (CNC) stock for `tickers` - CNC day positions
        plus the demat holdings book. {ticker: {'qty': int,
        'avg_price': float|None}}."""
        want = {t.upper() for t in tickers}
        out = {}
        for p in self.kite.positions()["day"]:
            if p.get("product") != self.kite.PRODUCT_CNC:
                continue
            sym = p["tradingsymbol"]
            q = int(p.get("quantity", 0) or 0)
            if sym in want and q:
                out[sym] = {"qty": q,
                            "avg_price": round(float(p.get("average_price", 0) or 0), 2) or None}
        for h in self.kite.holdings():
            sym = h.get("tradingsymbol", "")
            q = int(h.get("quantity", 0) or 0)
            if sym in want and q and sym not in out:
                out[sym] = {"qty": q,
                            "avg_price": round(float(h.get("average_price", 0) or 0), 2) or None}
        return out

    def trades(self):
        """Today's fills, normalized to the shared shape:
            {tsym, transaction_type, fill_timestamp, exchange_time,
             qty, placed_price, fill_price, order_no}
        Kite orders here are MARKET, so there is no meaningful placed price -
        placed_price is None and only the average fill price is known."""
        out = []
        for t in self.kite.trades():
            out.append({
                "tsym": t["tradingsymbol"],
                "transaction_type": t["transaction_type"],
                "fill_timestamp": t["fill_timestamp"],
                "exchange_time": t.get("exchange_timestamp") or t["fill_timestamp"],
                "qty": int(float(t.get("quantity", 0) or 0)),
                "placed_price": None,
                "fill_price": float(t.get("average_price", 0) or 0),
                "order_no": str(t.get("order_id", "")),
            })
        return out

    def position_book(self):
        """All of today's positions, normalized to the shared shape:
            {tsym, buy_qty, buy_price, sell_qty, sell_price, net_qty, net_avg, cf_qty}
        Only rows with any qty (buy, sell, net or carried forward) are
        returned. cf_qty is Kite's overnight (carried forward) quantity."""
        out = []
        for p in self.kite.positions()["day"]:
            buy_qty = int(p.get("buy_quantity", 0) or 0)
            sell_qty = int(p.get("sell_quantity", 0) or 0)
            net_qty = int(p.get("quantity", 0) or 0)
            cf_qty = int(p.get("overnight_quantity", 0) or 0)
            if not (buy_qty or sell_qty or net_qty or cf_qty):
                continue
            out.append({
                "tsym": p.get("tradingsymbol", ""),
                "buy_qty": buy_qty,
                "buy_price": float(p.get("buy_price", 0) or 0),
                "sell_qty": sell_qty,
                "sell_price": float(p.get("sell_price", 0) or 0),
                "net_qty": net_qty,
                "net_avg": float(p.get("average_price", 0) or 0),
                "cf_qty": cf_qty,
            })
        return out


class NorenBroker:
    """Mastertrust Noren REST execution. Data still comes from Zerodha."""

    def __init__(self, tickers):
        secret = _fetch_secret(NOREN_SECRET_ID,
                               profile=NOREN_AWS_PROFILE,
                               region=NOREN_AWS_REGION)
        self.uid = secret["client_id"].split("_")[0]
        self.actid = self.uid
        self.access_token = secret["access_token"]
        self.base_url = NOREN_BASE_URL

        # ticker -> {"tsym", "token", "ti"}; fail at startup, not mid-session
        self.scrips = {t: self._resolve_scrip(t) for t in tickers}
        for ticker, s in self.scrips.items():
            print(f"[noren] resolved {ticker} -> tsym={s['tsym']} "
                  f"token={s['token']} tick={s['ti']}")

    # ---------- transport ----------

    def _call(self, endpoint, body=None, tolerate_no_data=False, timeout=None):
        payload = {"uid": self.uid, "actid": self.actid, **(body or {})}
        data = f"jData={json.dumps(payload)}"
        headers = {"Authorization": f"Bearer {self.access_token}"}

        # timeout defaults to None (unchanged for the live bot's existing
        # calls); callers that must not hang forever pass a value.
        response = requests.post(self.base_url + endpoint, data=data,
                                 headers=headers, timeout=timeout)
        response.raise_for_status()
        result = response.json()

        # Error responses are dicts with stat=Not_Ok; success may be dict or list.
        if isinstance(result, dict) and result.get("stat") == "Not_Ok":
            emsg = result.get("emsg", "")
            if tolerate_no_data and "no data" in emsg.lower():
                return []
            raise NorenError(f"{endpoint}: {emsg or result}")
        return result

    # ---------- startup resolution ----------

    def _resolve_scrip(self, ticker):
        tsym = f"{ticker.upper()}-EQ"
        result = self._call("SearchScrip", {"stext": tsym, "exch": "NSE"}, timeout=20)
        for scrip in result.get("values", []):
            if scrip.get("tsym") == tsym and scrip.get("exch") == "NSE":
                return {"tsym": tsym, "token": scrip["token"],
                        "ti": float(scrip.get("ti") or 0.05)}
        raise NorenError(
            f"could not resolve '{tsym}' on NSE via SearchScrip - "
            f"check the symbol in Mastertrust's master (got: "
            f"{[s.get('tsym') for s in result.get('values', [])][:5]})"
        )

    # ---------- pricing ----------

    def _ltp(self, ticker):
        scrip = self.scrips[ticker]
        quote = self._call("GetQuotes", {"exch": "NSE", "token": scrip["token"]}, timeout=15)
        return float(quote["lp"])

    def _marketable_price(self, ticker, side):
        """LTP nudged past the touch in the trade direction, snapped to tick."""
        scrip = self.scrips[ticker]
        ltp = self._ltp(ticker)
        factor = 1 + LMT_BUFFER if side == "B" else 1 - LMT_BUFFER
        raw = ltp * factor
        ti = scrip["ti"]
        price = round(round(raw / ti) * ti, 2)
        return price

    # ---------- margin pre-check (buys only) ----------

    def _margin_ok(self, ticker, qty, price):
        scrip = self.scrips[ticker]
        try:
            result = self._call("GetOrderMargin", {
                "exch": "NSE", "tsym": scrip["tsym"], "qty": str(qty),
                "prc": str(price), "prd": "I", "trantype": "B", "prctyp": "LMT",
            }, timeout=15)
        except NorenError as e:
            # a broken pre-check should not block trading; log and proceed
            event(f"[noren] {ticker} margin pre-check errored ({e}), proceeding")
            return True

        remarks = result.get("remarks", "")
        if "Insufficient Balance" in remarks:
            event(f"[noren] {ticker} BUY SKIPPED - insufficient margin "
                  f"(shortfall={result.get('marginused')}, cash={result.get('cash')})")
            return False
        return True

    # ---------- orders ----------

    def _place(self, ticker, qty, side):
        scrip = self.scrips[ticker]
        price = self._marketable_price(ticker, side)

        if side == "B" and not self._margin_ok(ticker, qty, price):
            return None

        result = self._call("PlaceOrder", {
            "exch": "NSE",
            "tsym": scrip["tsym"],
            "qty": str(qty),
            "prc": str(price),
            "prd": "I",
            "trantype": side,
            "prctyp": "LMT",
            "ret": "DAY",
        })
        norenordno = result.get("norenordno")
        event(f"[noren] {ticker} {side} x{qty} @ {price} placed, "
              f"order={norenordno}")

        self._confirm_fill(ticker, norenordno)
        return norenordno

    def _confirm_fill(self, ticker, norenordno):
        """Poll order status briefly; alert (don't repost) if not COMPLETE.

        Marketable LMT orders can rest unfilled if price gaps through the
        buffer - unlike the MKT flow on Kite, a fill is not guaranteed.
        """
        status = None
        for _ in range(FILL_POLL_ATTEMPTS):
            time.sleep(FILL_POLL_SLEEP)
            try:
                result = self._call("SingleOrdStatus",
                                    {"norenordno": norenordno, "exch": "NSE"}, timeout=15)
                entry = result[0] if isinstance(result, list) else result
                status = entry.get("status")
            except NorenError as e:
                event(f"[noren] {ticker} status check failed for "
                      f"{norenordno}: {e}")
                continue
            if status == "COMPLETE":
                event(f"[noren] {ticker} order {norenordno} COMPLETE")
                return
            if status == "REJECTED":
                event(f"[noren] !! {ticker} order {norenordno} REJECTED: "
                      f"{entry.get('rejreason', '')}")
                return

        event(f"[noren] !! {ticker} order {norenordno} still '{status}' after "
              f"{FILL_POLL_ATTEMPTS} checks - VERIFY MANUALLY, position state "
              f"may drift until next bar's net_position() resync")

    def buy(self, ticker, qty):
        return self._place(ticker, qty, "B")

    def sell(self, ticker, qty):
        return self._place(ticker, qty, "S")

    # ---------- positions ----------

    def net_position(self, ticker):
        scrip = self.scrips[ticker]
        book = self._call("PositionBook", tolerate_no_data=True, timeout=20)
        for p in book:
            if p.get("tsym") == scrip["tsym"] and p.get("prd") == "I":
                return int(p.get("netqty", 0))
        return 0

    def _intraday_row(self, tsym):
        """Raw PositionBook row for tsym with an intraday (prd='I') leg, or None."""
        for p in self._call("PositionBook", tolerate_no_data=True, timeout=20):
            if p.get("tsym") == tsym and p.get("prd") == "I":
                return p
        return None

    def delivery_cash(self):
        """Available cash for delivery (prd='C') per the Limits endpoint, or
        None if it can't be read. Informational only - logged before a
        conversion so a later margin rejection has a breadcrumb. Never blocks:
        the Limits endpoint has been seen to hang on this deployment, so this
        is capped at a short timeout and any failure just returns None."""
        try:
            lim = self._call("Limits", {"prd": "C", "seg": "EQT", "exch": "NSE"},
                             timeout=10)
        except Exception as e:
            event(f"[noren] Limits check skipped ({e})")
            return None
        for k in ("cash", "cashmarginavailable", "marginused"):
            if k in lim:
                try:
                    return float(lim[k])
                except (TypeError, ValueError):
                    pass
        return None

    @staticmethod
    def _row_price(row, trantype):
        """Best 'carried at' price for a PositionBook row: net average price,
        falling back to the day buy/sell average for the leg's side. Returns
        a rounded float or None."""
        keys = ("netavgprc", "daybuyavgprc" if trantype == "B" else "daysellavgprc")
        for k in keys:
            v = row.get(k)
            try:
                if v not in (None, "", "NA"):
                    return round(float(v), 2)
            except (TypeError, ValueError):
                pass
        return None

    def _convert_intraday_leg(self, row, label=None, source="auto"):
        """POST ProductConversion for one open intraday PositionBook row
        (prd I -> C). Qty and side come from netqty (sign: +ve long -> 'B',
        -ve short -> 'S'), since ProductConversion takes no position id and
        matches on the exch+tsym+prd+trantype tuple. On acceptance, writes a
        delivery-log record with the price the position was carried at, then
        re-reads PositionBook (it can lag) to confirm the intraday leg is
        gone. Returns True if the endpoint accepted it - on a lagging
        PositionBook it still returns True and logs VERIFY MANUALLY, because
        the alternative (caller treats it as failed) risks a later
        square-off dumping a position that is actually now delivery."""
        tsym = row.get("tsym", "")
        name = label or tsym
        netqty = int(row.get("netqty", 0) or 0)
        if netqty == 0:
            return False
        trantype = "B" if netqty > 0 else "S"
        qty = abs(netqty)
        avg_price = self._row_price(row, trantype)
        try:
            ltp = round(float(row.get("lp")), 2)
        except (TypeError, ValueError):
            ltp = None
        try:
            self._call("ProductConversion", {
                "exch": "NSE",
                "tsym": tsym,
                "qty": str(qty),
                "prd": "C",       # target product: delivery
                "prevprd": "I",   # current product: intraday
                "trantype": trantype,
                "postype": "Day",
                "ordersource": "MOB",
            }, timeout=20)
        except Exception as e:
            event(f"[noren] !! {name} ProductConversion failed: {e}")
            return False

        delivery_log({
            "time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            "ticker": name[:-3] if name.endswith("-EQ") else name,
            "tsym": tsym,
            "side": trantype,
            "qty": qty,
            "avg_price": avg_price,
            "ltp": ltp,
            "source": source,
        })

        for _ in range(3):
            time.sleep(1)
            after = self._intraday_row(tsym)
            if after is None or int(after.get("netqty", 0) or 0) == 0:
                event(f"[noren] {name} converted {trantype} x{qty} "
                      f"@ avg {avg_price} MIS -> DELIVERY")
                return True
        event(f"[noren] {name} conversion accepted (@ avg {avg_price}) but "
              f"PositionBook still shows intraday - treating as done, VERIFY MANUALLY")
        return True

    def convert_to_delivery(self, ticker, source="auto"):
        """Convert one tracked ticker's open intraday position to delivery."""
        tsym = self.scrips[ticker]["tsym"]
        row = self._intraday_row(tsym)
        if row is None:
            return False
        return self._convert_intraday_leg(row, label=ticker, source=source)

    def convert_all_intraday(self, source="script"):
        """Convert EVERY open intraday position on the account to delivery -
        not just tracked tickers. Returns a list of
        {tsym, trantype, qty, avg_price, ok} dicts. Used by the standalone
        convert_to_delivery.py script."""
        rows = [p for p in self._call("PositionBook", tolerate_no_data=True, timeout=20)
                if p.get("prd") == "I" and int(p.get("netqty", 0) or 0) != 0]
        results = []
        for p in rows:
            netqty = int(p["netqty"])
            trantype = "B" if netqty > 0 else "S"
            results.append({
                "tsym": p["tsym"],
                "trantype": trantype,
                "qty": abs(netqty),
                "avg_price": self._row_price(p, trantype),
                "ok": self._convert_intraday_leg(p, source=source),
            })
        return results

    def delivery_holdings(self, tickers):
        """Current delivery (prd='C') stock for `tickers` (plain symbols, no
        -EQ), read from the PositionBook - covers same-day conversions and
        carry-forward positions. Returns {ticker: {'qty': int,
        'avg_price': float|None}} for tickers actually held. Stock that has
        settled into the demat holdings book is not reflected here."""
        want = {t.upper() for t in tickers}
        out = {}
        for p in self._call("PositionBook", tolerate_no_data=True, timeout=20):
            if p.get("prd") != "C":
                continue
            sym = p.get("tsym", "")
            base = sym[:-3] if sym.endswith("-EQ") else sym
            if base not in want:
                continue
            q = int(p.get("netqty", 0) or 0)
            if q == 0:
                continue
            out[base] = {"qty": q,
                         "avg_price": self._row_price(p, "B" if q > 0 else "S")}
        return out

    def trades(self):
        """Today's fills, normalized to the shared shape:
            {tsym, transaction_type, fill_timestamp, exchange_time,
             qty, placed_price, fill_price, order_no}
        exch_tm is the exchange fill time as a naive IST wall-clock string,
        e.g. '25-08-2026 14:05:00'.

        Field names for qty/price come from the Noren TradeBook payload and
        vary a little by build - the _first() fallbacks cover the common
        aliases (flprc/avgprc/prc, flqty/qty/fillshares). Verify against one
        real TradeBook row if a column shows blank; a missing key degrades to
        0/None rather than raising."""
        book = self._call("TradeBook", tolerate_no_data=True, timeout=20)

        def _first(d, *keys):
            for k in keys:
                v = d.get(k)
                if v not in (None, "", "NA", "0", 0):
                    return v
            return None

        out = []
        for t in book:
            tsym = t["tsym"][:-3] if t["tsym"].endswith("-EQ") else t["tsym"]
            exch_time = datetime.strptime(t["exch_tm"], "%d-%m-%Y %H:%M:%S")
            qty = _first(t, "flqty", "qty", "fillshares")
            placed = _first(t, "prc", "rprc")
            fill = _first(t, "flprc", "avgprc", "prc")
            out.append({
                "tsym": tsym,
                "transaction_type": "BUY" if t["trantype"] == "B" else "SELL",
                "fill_timestamp": exch_time,
                "exchange_time": exch_time,
                "qty": int(float(qty)) if qty is not None else 0,
                "placed_price": float(placed) if placed is not None else None,
                "fill_price": float(fill) if fill is not None else 0.0,
                "order_no": str(t.get("norenordno", "")),
            })
        return out

    def position_book(self):
        """All positions currently in this account - not just the tracked
        tickers - normalized to the shared shape:
            {tsym, buy_qty, buy_price, sell_qty, sell_price, net_qty, net_avg, cf_qty}
        Only rows with any qty (buy, sell, net or carried forward) are
        returned. Field names follow the standard Noren PositionBook spec;
        like trades(), Mastertrust's build can vary a little - verify
        against one real PositionBook row if a column looks off and adjust
        the keys below."""
        book = self._call("PositionBook", tolerate_no_data=True, timeout=20)

        def _f(d, key, alt=None):
            v = d.get(key)
            if v in (None, "", "NA") and alt:
                v = d.get(alt)
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        out = []
        for p in book:
            buy_qty = int(_f(p, "daybuyqty"))
            sell_qty = int(_f(p, "daysellqty"))
            net_qty = int(_f(p, "netqty"))
            cf_qty = int(_f(p, "cfbuyqty")) - int(_f(p, "cfsellqty"))
            if not (buy_qty or sell_qty or net_qty or cf_qty):
                continue
            out.append({
                "tsym": p.get("tsym", ""),
                "buy_qty": buy_qty,
                "buy_price": _f(p, "daybuyavgprc", "daybuyavg"),
                "sell_qty": sell_qty,
                "sell_price": _f(p, "daysellavgprc", "daysellavg"),
                "net_qty": net_qty,
                "net_avg": _f(p, "netavgprc", "netupldprc"),
                "cf_qty": cf_qty,
            })
        return out


def make_broker(name, kite=None, tickers=None):
    name = name.lower()
    if name == "zerodha":
        if kite is None:
            raise ValueError("zerodha broker needs a kite client")
        return KiteBroker(kite)
    if name in ("mastertrust", "noren"):
        if not tickers:
            raise ValueError("noren broker needs the ticker list for scrip resolution")
        return NorenBroker(tickers)
    raise ValueError(f"unknown broker '{name}'")
