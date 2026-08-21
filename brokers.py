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

IST = ZoneInfo("Asia/Kolkata")

NOREN_SECRET_ID = "/trading/brokers/mastertrust/vaibhav"
NOREN_AWS_PROFILE = "broker-secrets"
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
        day_positions = self.kite.positions()["day"]
        for p in day_positions:
            if p["tradingsymbol"] == ticker.upper():
                return int(p["quantity"])
        return 0


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

    def _call(self, endpoint, body=None, tolerate_no_data=False):
        payload = {"uid": self.uid, "actid": self.actid, **(body or {})}
        data = f"jData={json.dumps(payload)}"
        headers = {"Authorization": f"Bearer {self.access_token}"}

        response = requests.post(self.base_url + endpoint, data=data, headers=headers)
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
        result = self._call("SearchScrip", {"stext": tsym, "exch": "NSE"})
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
        quote = self._call("GetQuotes", {"exch": "NSE", "token": scrip["token"]})
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
            })
        except NorenError as e:
            # a broken pre-check should not block trading; log and proceed
            print(f"[noren] {ticker} margin pre-check errored ({e}), proceeding")
            return True

        remarks = result.get("remarks", "")
        if "Insufficient Balance" in remarks:
            print(f"[noren] {ticker} BUY SKIPPED - insufficient margin "
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
        print(f"[noren] {ticker} {side} x{qty} @ {price} placed, "
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
                                    {"norenordno": norenordno, "exch": "NSE"})
                entry = result[0] if isinstance(result, list) else result
                status = entry.get("status")
            except NorenError as e:
                print(f"[noren] {ticker} status check failed for "
                      f"{norenordno}: {e}")
                continue
            if status == "COMPLETE":
                print(f"[noren] {ticker} order {norenordno} COMPLETE")
                return
            if status == "REJECTED":
                print(f"[noren] !! {ticker} order {norenordno} REJECTED: "
                      f"{entry.get('rejreason', '')}")
                return

        print(f"[noren] !! {ticker} order {norenordno} still '{status}' after "
              f"{FILL_POLL_ATTEMPTS} checks - VERIFY MANUALLY, position state "
              f"may drift until next bar's net_position() resync")

    def buy(self, ticker, qty):
        return self._place(ticker, qty, "B")

    def sell(self, ticker, qty):
        return self._place(ticker, qty, "S")

    # ---------- positions ----------

    def net_position(self, ticker):
        scrip = self.scrips[ticker]
        book = self._call("PositionBook", tolerate_no_data=True)
        for p in book:
            if p.get("tsym") == scrip["tsym"] and p.get("prd") == "I":
                return int(p.get("netqty", 0))
        return 0


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
