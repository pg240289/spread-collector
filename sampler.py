#!/usr/bin/env python3
"""
Order-book sampler: NSE index/stock F&O vs Indian crypto venues.

One run = one timestamped snapshot of every instrument in scope.
Outputs:
  data/raw/<UTC stamp>.json   full 5-level (NSE) / 20-level (crypto) books + metadata + errors
  data/rows/<UTC stamp>.csv   one row per instrument with spread / depth / impact-cost metrics

Only dependency: requests.  Run with:  python sampler.py
"""
import csv
import datetime as dt
import json
import math
import os
import sys
import time
import traceback

import requests

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
RUN_START = dt.datetime.now(dt.timezone.utc)
STAMP = RUN_START.strftime("%Y%m%dT%H%M%SZ")
OUT_RAW = os.path.join("data", "raw")
OUT_ROWS = os.path.join("data", "rows")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
NSE_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
NSE_API = "https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi"
NSE_PAUSE = 0.6          # seconds between NSE calls (be polite; avoid throttling)
HTTP_TIMEOUT = 20

# Single-stock futures: six structurally liquid names and six low-liquidity F&O names
# (tail list chosen from 4-Sep-2026 NSE stock-futures turnover ranking).
STOCKS_TOP = ["RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "INFY", "AXISBANK"]
STOCKS_TAIL = ["SAIL", "NBCC", "PETRONET", "TIINDIA", "MANKIND", "GODFRYPHLP"]
STOCK_OPTIONS = ["RELIANCE", "HDFCBANK"]        # ATM CE/PE, nearest expiry

# Order sizes (INR) for NSE-style impact cost, and bps bands for cumulative depth
IC_SIZES_INR = [100_000, 1_000_000, 5_000_000, 10_000_000]
DEPTH_BANDS_BPS = [1, 2, 5, 10, 25, 50]

CRYPTO_DEPTH = 20

errors = []          # list of {"where":..., "error":...}
raw_instruments = [] # list of dicts with full books
rows = []            # flattened metrics


def log(msg):
    print(f"[{dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S')}Z] {msg}", flush=True)


def record_error(where, exc):
    msg = f"{type(exc).__name__}: {exc}" if isinstance(exc, Exception) else str(exc)
    errors.append({"where": where, "error": msg})
    log(f"ERROR {where}: {msg}")


def fnum(x, default=None):
    try:
        if x is None:
            return default
        v = float(str(x).replace(",", "").strip())
        return v if math.isfinite(v) else default
    except Exception:
        return default


# --------------------------------------------------------------------------------------
# Generic HTTP with retries
# --------------------------------------------------------------------------------------
def get_json(session, url, params=None, headers=None, retries=3, backoff=2.0, timeout=HTTP_TIMEOUT):
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 200 and r.text.strip():
                return r.json()
            last = RuntimeError(f"HTTP {r.status_code} body[:120]={r.text[:120]!r}")
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(backoff * attempt)
    raise last if last else RuntimeError("unknown fetch failure")


# --------------------------------------------------------------------------------------
# Book metrics (shared by NSE and crypto)
# --------------------------------------------------------------------------------------
def walk_book(levels, target_inr, qty_mult, px_to_inr):
    """Walk one side of the book to fill `target_inr` of notional.
    levels: [[price, size_units], ...] best first. Returns (avg_price, filled_inr, exhausted)."""
    remaining = target_inr
    cost = 0.0
    qty = 0.0
    for price, size in levels:
        p, s = fnum(price), fnum(size)
        if not p or not s or p <= 0 or s <= 0:
            continue
        level_inr = p * s * qty_mult * px_to_inr
        take_inr = min(remaining, level_inr)
        take_qty = take_inr / (p * px_to_inr)          # in base units (after qty_mult)
        cost += take_qty * p
        qty += take_qty
        remaining -= take_inr
        if remaining <= 1e-9:
            return (cost / qty if qty else None, target_inr - remaining, False)
    return (cost / qty if qty else None, target_inr - remaining, True)


def book_metrics(levels_bid, levels_ask, qty_mult=1.0, px_to_inr=1.0):
    m = {}
    lb = [(fnum(p), fnum(q)) for p, q in levels_bid if fnum(p) and fnum(q)]
    la = [(fnum(p), fnum(q)) for p, q in levels_ask if fnum(p) and fnum(q)]
    lb.sort(key=lambda x: -x[0])
    la.sort(key=lambda x: x[0])
    m["visible_levels_bid"] = len(lb)
    m["visible_levels_ask"] = len(la)
    if not lb or not la:
        return m
    bid, bq = lb[0]
    ask, aq = la[0]
    mid = (bid + ask) / 2.0
    m.update({
        "bid": bid, "ask": ask, "bid_qty_units": bq, "ask_qty_units": aq,
        "mid": mid, "spread": ask - bid,
        "spread_bps": (ask - bid) / mid * 1e4 if mid else None,
        "depth_touch_bid_inr": bid * bq * qty_mult * px_to_inr,
        "depth_touch_ask_inr": ask * aq * qty_mult * px_to_inr,
    })
    for k in DEPTH_BANDS_BPS:
        lo = mid * (1 - k / 1e4)
        hi = mid * (1 + k / 1e4)
        m[f"depth_bid_{k}bp_inr"] = sum(p * q for p, q in lb if p >= lo) * qty_mult * px_to_inr
        m[f"depth_ask_{k}bp_inr"] = sum(p * q for p, q in la if p <= hi) * qty_mult * px_to_inr
    for size in IC_SIZES_INR:
        tag = {100_000: "1L", 1_000_000: "10L", 5_000_000: "50L", 10_000_000: "1cr"}[size]
        avg_b, _, ex_b = walk_book(la, size, qty_mult, px_to_inr)
        avg_s, _, ex_s = walk_book(lb, size, qty_mult, px_to_inr)
        m[f"ic_buy_{tag}_bps"] = None if (ex_b or avg_b is None) else (avg_b - mid) / mid * 1e4
        m[f"ic_sell_{tag}_bps"] = None if (ex_s or avg_s is None) else (mid - avg_s) / mid * 1e4
    return m


def add_instrument(rec, levels_bid, levels_ask, qty_mult=1.0, px_to_inr=1.0):
    rec = dict(rec)
    rec["levels_bid"] = [[fnum(p), fnum(q)] for p, q in levels_bid]
    rec["levels_ask"] = [[fnum(p), fnum(q)] for p, q in levels_ask]
    rec["qty_mult"] = qty_mult
    rec["px_to_inr"] = px_to_inr
    raw_instruments.append(rec)
    row = {k: v for k, v in rec.items() if k not in ("levels_bid", "levels_ask")}
    row.update(book_metrics(levels_bid, levels_ask, qty_mult, px_to_inr))
    rows.append(row)
    sp = row.get("spread_bps")
    log(f"  {rec['venue']:<22} {rec['label']:<28} bid={row.get('bid')} ask={row.get('ask')} "
        f"spread_bps={sp:.3f}" if sp is not None else f"  {rec['venue']:<22} {rec['label']:<28} EMPTY BOOK")


# --------------------------------------------------------------------------------------
# NSE
# --------------------------------------------------------------------------------------
def nse_session():
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    try:
        s.get("https://www.nseindia.com/", timeout=HTTP_TIMEOUT)   # cookie warm-up; failure is non-fatal
    except Exception as e:  # noqa: BLE001
        log(f"NSE warm-up failed (continuing): {e}")
    return s


def nse_symbol_data(s, symbol):
    time.sleep(NSE_PAUSE)
    j = get_json(s, NSE_API, params={"functionName": "getSymbolDerivativesData", "symbol": symbol})
    data = j.get("data") or []
    if not data:
        raise RuntimeError(f"no derivatives data for {symbol}: {str(j)[:200]}")
    return data


def parse_exp(sdate):
    return dt.datetime.strptime(sdate, "%d-%b-%Y").date()


def pick_index_instruments(data, symbol, otm_offset):
    """Return list of (identifier, label, kind, extra) for an index underlying."""
    out = []
    ul = fnum(data[0].get("underlyingValue"))
    futs = sorted([d for d in data if d["instrumentType"] == "FUTIDX"], key=lambda d: parse_exp(d["expiryDate"]))
    if futs:
        out.append((futs[0]["identifier"], f"{symbol} FUT near", "index_future", {"expiry": futs[0]["expiryDate"]}))
    if len(futs) > 1:
        out.append((futs[1]["identifier"], f"{symbol} FUT next", "index_future", {"expiry": futs[1]["expiryDate"]}))
    opts = [d for d in data if d["instrumentType"] == "OPTIDX"]
    expiries = sorted({parse_exp(d["expiryDate"]) for d in opts})
    if not opts or not expiries or ul is None:
        return out, ul
    monthly = parse_exp(futs[0]["expiryDate"]) if futs else None

    def atm_for(exp):
        strikes = sorted({fnum(d["strikePrice"]) for d in opts if parse_exp(d["expiryDate"]) == exp})
        return min(strikes, key=lambda k: abs(k - ul)) if strikes else None

    def find(exp, strike, typ):
        for d in opts:
            if parse_exp(d["expiryDate"]) == exp and d["optionType"] == typ and abs(fnum(d["strikePrice"]) - strike) < 1e-6:
                return d["identifier"]
        return None

    near = expiries[0]
    atm = atm_for(near)
    if atm is not None:
        dte = (near - RUN_START.astimezone(IST).date()).days
        tag = f"exp {near.strftime('%d-%b')} ({dte}DTE)"
        for typ in ("CE", "PE"):
            i = find(near, atm, typ)
            if i:
                out.append((i, f"{symbol} {int(atm)} {typ} {tag}", "index_option_atm", {"expiry": near.isoformat(), "strike": atm, "moneyness": "ATM", "dte": dte}))
        if otm_offset:
            for typ, k in (("CE", atm + otm_offset), ("PE", atm - otm_offset)):
                i = find(near, k, typ)
                if i:
                    out.append((i, f"{symbol} {int(k)} {typ} {tag}", "index_option_otm", {"expiry": near.isoformat(), "strike": k, "moneyness": f"OTM{otm_offset}", "dte": dte}))
    if monthly and monthly != near and monthly in expiries:
        atm_m = atm_for(monthly)
        if atm_m is not None:
            dte = (monthly - RUN_START.astimezone(IST).date()).days
            tag = f"exp {monthly.strftime('%d-%b')} ({dte}DTE)"
            for typ in ("CE", "PE"):
                i = find(monthly, atm_m, typ)
                if i:
                    out.append((i, f"{symbol} {int(atm_m)} {typ} {tag}", "index_option_atm_monthly", {"expiry": monthly.isoformat(), "strike": atm_m, "moneyness": "ATM", "dte": dte}))
    return out, ul


def pick_stock_option_instruments(data, symbol):
    out = []
    ul = fnum(data[0].get("underlyingValue"))
    opts = [d for d in data if d["instrumentType"] == "OPTSTK"]
    if not opts or ul is None:
        return out
    near = min(parse_exp(d["expiryDate"]) for d in opts)
    strikes = sorted({fnum(d["strikePrice"]) for d in opts if parse_exp(d["expiryDate"]) == near})
    atm = min(strikes, key=lambda k: abs(k - ul))
    dte = (near - RUN_START.astimezone(IST).date()).days
    for d in opts:
        if parse_exp(d["expiryDate"]) == near and abs(fnum(d["strikePrice"]) - atm) < 1e-6:
            out.append((d["identifier"], f"{symbol} {atm:g} {d['optionType']} exp {near.strftime('%d-%b')} ({dte}DTE)", "stock_option_atm",
                        {"expiry": near.isoformat(), "strike": atm, "moneyness": "ATM", "dte": dte}))
    return out


def nse_trade_info(s, symbol, identifier):
    time.sleep(NSE_PAUSE)
    j = get_json(s, NSE_API, params={"functionName": "getTradeInfoDerivative", "symbol": symbol,
                                      "identifier": identifier, "type": "W"})
    d = (j.get("derivateResponse") or [None])[0]
    if not d:
        raise RuntimeError(f"empty derivateResponse: {str(j)[:200]}")
    ob = d.get("orderBook") or {}
    bids = [[ob.get(f"buyPrice{i}"), ob.get(f"buyQuantity{i}")] for i in range(1, 6)]
    asks = [[ob.get(f"sellPrice{i}"), ob.get(f"sellQuantity{i}")] for i in range(1, 6)]
    bids = [b for b in bids if fnum(b[0]) and fnum(b[1])]
    asks = [a for a in asks if fnum(a[0]) and fnum(a[1])]
    ti = d.get("tradeInfo") or {}
    oi = d.get("otherinfo") or {}
    md = d.get("metaData") or {}
    meta = {
        "source_ts": d.get("lastUpdateTime"),
        "lot": fnum(ti.get("marketlot")), "tick": fnum(oi.get("ticksize")),
        "underlying": fnum(ti.get("underlyingvalue")), "last": fnum(md.get("last")),
        "volume": fnum(ti.get("totalTradedVolume")), "oi": fnum(ti.get("openinterest")),
        "notional_traded_inr": fnum(ti.get("totalTradedNotionalValue")),
        "total_buy_units": fnum(ob.get("totalBuyQuantity")), "total_sell_units": fnum(ob.get("totalSellQuantity")),
    }
    return bids, asks, meta


def sample_nse():
    s = nse_session()
    plan = []   # (symbol, identifier, label, kind, extra)
    # Index underlyings (Nifty OTM offset 100 pts, Bank Nifty 200 pts)
    for symbol, otm in (("NIFTY", 100), ("BANKNIFTY", 200)):
        try:
            data = nse_symbol_data(s, symbol)
            inst, ul = pick_index_instruments(data, symbol, otm)
            for ident, label, kind, extra in inst:
                plan.append((symbol, ident, label, kind, extra))
            log(f"NSE {symbol}: underlying={ul}, {len(inst)} instruments selected")
        except Exception as e:  # noqa: BLE001
            record_error(f"NSE symbol data {symbol}", e)
    # Stock futures: build identifiers from the index near-month expiry (same last-Tuesday date)
    near_month = None
    for sym, ident, label, kind, extra in plan:
        if label == "NIFTY FUT near":
            near_month = ident.replace("FUTIDXNIFTY", "").replace("XX0.00", "")   # e.g. 29-09-2026
    for group, names in (("top", STOCKS_TOP), ("tail", STOCKS_TAIL)):
        for sym in names:
            if near_month:
                plan.append((sym, f"FUTSTK{sym}{near_month}XX0.00", f"{sym} FUT near ({group})", f"stock_future_{group}", {"expiry": near_month}))
    for sym in STOCK_OPTIONS:
        try:
            data = nse_symbol_data(s, sym)
            for ident, label, kind, extra in pick_stock_option_instruments(data, sym):
                plan.append((sym, ident, label, kind, extra))
        except Exception as e:  # noqa: BLE001
            record_error(f"NSE symbol data {sym}", e)
    # Fetch books
    for sym, ident, label, kind, extra in plan:
        try:
            bids, asks, meta = nse_trade_info(s, sym, ident)
            rec = {"venue": "NSE F&O", "symbol": sym, "identifier": ident, "label": label, "kind": kind,
                   "ccy": "INR", "liquidity_source": "native", **extra, **meta}
            add_instrument(rec, bids, asks, qty_mult=1.0, px_to_inr=1.0)
        except Exception as e:  # noqa: BLE001
            record_error(f"NSE trade info {ident}", e)


# --------------------------------------------------------------------------------------
# Crypto venues (all public, no key)
# --------------------------------------------------------------------------------------
def map_book(d):
    """CoinDCX-style {price: qty} dict -> [[price, qty], ...]"""
    return [[k, v] for k, v in (d or {}).items()]


def sample_crypto(fx):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    usdt_inr = fx.get("usdt_inr_mid") or fx.get("usd_inr_official") or 95.0

    # Delta Exchange India — USD-quoted perpetuals (contract_value BTC 0.001, ETH 0.01)
    for sym, cv in (("BTCUSD", 0.001), ("ETHUSD", 0.01)):
        try:
            j = get_json(s, f"https://api.india.delta.exchange/v2/l2orderbook/{sym}", params={"depth": CRYPTO_DEPTH})
            r = j["result"]
            bids = [[x["price"], x["size"]] for x in r["buy"]]
            asks = [[x["price"], x["size"]] for x in r["sell"]]
            rec = {"venue": "Delta Exchange India", "symbol": sym, "label": f"Delta India {sym} perp", "kind": "crypto_perp",
                   "ccy": "USD", "liquidity_source": "native (RMM programme)", "contract_value": cv,
                   "source_ts": r.get("last_updated_at")}
            add_instrument(rec, bids, asks, qty_mult=cv, px_to_inr=usdt_inr)
        except Exception as e:  # noqa: BLE001
            record_error(f"Delta {sym}", e)

    # CoinDCX spot — native INR books (I-) and Binance-routed USDT book (B-)
    for pair, ccy, src in (("I-BTC_INR", "INR", "native"), ("I-ETH_INR", "INR", "native"), ("I-USDT_INR", "INR", "native"),
                           ("B-BTC_USDT", "USDT", "routed (Binance)"), ("B-ETH_USDT", "USDT", "routed (Binance)")):
        try:
            j = get_json(s, "https://public.coindcx.com/market_data/orderbook", params={"pair": pair, "depth": CRYPTO_DEPTH})
            bids, asks = map_book(j.get("bids")), map_book(j.get("asks"))
            rec = {"venue": "CoinDCX spot", "symbol": pair, "label": f"CoinDCX {pair}", "kind": "crypto_spot",
                   "ccy": ccy, "liquidity_source": src, "source_ts": j.get("timestamp")}
            add_instrument(rec, bids, asks, qty_mult=1.0, px_to_inr=(1.0 if ccy == "INR" else usdt_inr))
        except Exception as e:  # noqa: BLE001
            record_error(f"CoinDCX {pair}", e)

    # CoinDCX Futures — INR-margined, USDT-quoted perpetuals (1 contract = 1 BTC)
    for pair in ("B-BTC_USDT-futures", "B-ETH_USDT-futures"):
        try:
            j = get_json(s, f"https://public.coindcx.com/market_data/v3/orderbook/{pair}/{CRYPTO_DEPTH}", timeout=12, retries=2)
            bids, asks = map_book(j.get("bids")), map_book(j.get("asks"))
            if not bids or not asks:
                raise RuntimeError(f"empty book: {str(j)[:120]}")
            rec = {"venue": "CoinDCX Futures", "symbol": pair, "label": f"CoinDCX Futures {pair.split('-')[1]}", "kind": "crypto_perp",
                   "ccy": "USDT", "liquidity_source": "routed (Binance)", "source_ts": j.get("ts")}
            add_instrument(rec, bids, asks, qty_mult=1.0, px_to_inr=usdt_inr)
        except Exception as e:  # noqa: BLE001
            record_error(f"CoinDCX Futures {pair}", e)

    # ZebPay — native INR spot and mirrored INR-quoted futures
    for sym in ("BTC-INR", "ETH-INR", "USDT-INR"):
        try:
            j = get_json(s, "https://sapi.zebpay.com/api/v2/market/orderbook", params={"symbol": sym, "limit": CRYPTO_DEPTH})
            d = j.get("data") or {}
            rec = {"venue": "ZebPay spot", "symbol": sym, "label": f"ZebPay {sym}", "kind": "crypto_spot",
                   "ccy": "INR", "liquidity_source": "native", "source_ts": d.get("timestamp")}
            add_instrument(rec, d.get("bids") or [], d.get("asks") or [], 1.0, 1.0)
        except Exception as e:  # noqa: BLE001
            record_error(f"ZebPay spot {sym}", e)
    for sym in ("BTCINR", "ETHINR"):
        try:
            j = get_json(s, "https://futuresbe.zebpay.com/api/v1/market/orderBook", params={"symbol": sym})
            d = j.get("data") or {}
            rec = {"venue": "ZebPay Futures", "symbol": sym, "label": f"ZebPay Futures {sym} perp", "kind": "crypto_perp",
                   "ccy": "INR", "liquidity_source": "mirrored (global USDT book, internal FX)", "source_ts": d.get("timestamp")}
            add_instrument(rec, d.get("bids") or [], d.get("asks") or [], 1.0, 1.0)
        except Exception as e:  # noqa: BLE001
            record_error(f"ZebPay Futures {sym}", e)

    # Pi42 — INR-margined perpetuals (mirrored global book). Often blocks non-browser clients; recorded if it answers.
    for sym in ("BTCINR", "ETHINR"):
        try:
            j = get_json(s, f"https://api.pi42.com/v1/market/depth/{sym}", timeout=12, retries=2)
            j = j.get("data") or j
            bids = j.get("bids") or j.get("b") or []
            asks = j.get("asks") or j.get("a") or []
            if not bids or not asks:
                raise RuntimeError(f"empty book: {str(j)[:120]}")
            rec = {"venue": "Pi42", "symbol": sym, "label": f"Pi42 {sym} perp", "kind": "crypto_perp",
                   "ccy": "INR", "liquidity_source": "mirrored (global USDT book, internal FX)", "source_ts": j.get("T") or j.get("timestamp")}
            add_instrument(rec, bids, asks, 1.0, 1.0)
        except Exception as e:  # noqa: BLE001
            record_error(f"Pi42 {sym}", e)

    # WazirX and Giottus — native INR spot books (small venues, included for completeness)
    try:
        j = get_json(s, "https://api.wazirx.com/sapi/v1/depth", params={"symbol": "btcinr", "limit": CRYPTO_DEPTH})
        rec = {"venue": "WazirX spot", "symbol": "btcinr", "label": "WazirX BTC/INR", "kind": "crypto_spot",
               "ccy": "INR", "liquidity_source": "native", "source_ts": j.get("timestamp")}
        add_instrument(rec, j.get("bids") or [], j.get("asks") or [], 1.0, 1.0)
    except Exception as e:  # noqa: BLE001
        record_error("WazirX btcinr", e)
    try:
        j = get_json(s, "https://api.giottus.com/api/v1/public/market/orderbook", params={"symbol": "BTC/INR", "limit": CRYPTO_DEPTH})
        rec = {"venue": "Giottus spot", "symbol": "BTC/INR", "label": "Giottus BTC/INR", "kind": "crypto_spot",
               "ccy": "INR", "liquidity_source": "native", "source_ts": None}
        add_instrument(rec, j.get("bids") or [], j.get("asks") or [], 1.0, 1.0)
    except Exception as e:  # noqa: BLE001
        record_error("Giottus BTC/INR", e)


def get_fx():
    """USDT/INR mid from CoinDCX (the rate an INR trader actually converts at) and the official USD/INR reference."""
    fx = {}
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    try:
        j = get_json(s, "https://public.coindcx.com/market_data/orderbook", params={"pair": "I-USDT_INR", "depth": 5})
        bids = sorted((fnum(k) for k in (j.get("bids") or {})), reverse=True)
        asks = sorted(fnum(k) for k in (j.get("asks") or {}))
        if bids and asks:
            fx["usdt_inr_bid"], fx["usdt_inr_ask"] = bids[0], asks[0]
            fx["usdt_inr_mid"] = (bids[0] + asks[0]) / 2
    except Exception as e:  # noqa: BLE001
        record_error("FX usdt_inr", e)
    try:
        j = get_json(s, "https://open.er-api.com/v6/latest/USD")
        fx["usd_inr_official"] = fnum((j.get("rates") or {}).get("INR"))
        fx["usd_inr_official_asof"] = j.get("time_last_update_utc")
    except Exception as e:  # noqa: BLE001
        record_error("FX usd_inr_official", e)
    if fx.get("usdt_inr_mid") and fx.get("usd_inr_official"):
        fx["usdt_premium_pct"] = (fx["usdt_inr_mid"] / fx["usd_inr_official"] - 1) * 100
    return fx


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
def market_phase(ist_now):
    t = ist_now.time()
    if ist_now.weekday() >= 5:
        return "weekend"
    if dt.time(9, 0) <= t < dt.time(9, 8):
        return "pre-open (order collection)"
    if dt.time(9, 8) <= t < dt.time(9, 15):
        return "pre-open (matching/buffer)"
    if dt.time(9, 15) <= t < dt.time(15, 30):
        return "open"
    return "closed"


def main():
    os.makedirs(OUT_RAW, exist_ok=True)
    os.makedirs(OUT_ROWS, exist_ok=True)
    ist_now = RUN_START.astimezone(IST)
    log(f"Run {STAMP} | IST {ist_now.strftime('%d-%b-%Y %H:%M:%S')} | NSE phase: {market_phase(ist_now)}")

    fx = get_fx()
    log(f"FX: {fx}")

    try:
        sample_nse()
    except Exception as e:  # noqa: BLE001
        record_error("sample_nse (fatal)", f"{e}\n{traceback.format_exc()}")
    try:
        sample_crypto(fx)
    except Exception as e:  # noqa: BLE001
        record_error("sample_crypto (fatal)", f"{e}\n{traceback.format_exc()}")

    run_end = dt.datetime.now(dt.timezone.utc)
    meta = {
        "run_id": STAMP, "ts_utc": RUN_START.isoformat(), "ts_ist": ist_now.strftime("%d-%b-%Y %H:%M:%S"),
        "run_end_utc": run_end.isoformat(), "duration_s": round((run_end - RUN_START).total_seconds(), 1),
        "nse_phase": market_phase(ist_now), "fx": fx, "n_instruments": len(raw_instruments), "n_errors": len(errors),
        "collector": os.environ.get("COLLECTOR_NAME", "github-actions"),
        "config": {"ic_sizes_inr": IC_SIZES_INR, "depth_bands_bps": DEPTH_BANDS_BPS, "crypto_depth": CRYPTO_DEPTH,
                   "stocks_top": STOCKS_TOP, "stocks_tail": STOCKS_TAIL, "stock_options": STOCK_OPTIONS},
    }
    with open(os.path.join(OUT_RAW, f"{STAMP}.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "instruments": raw_instruments, "errors": errors}, f, ensure_ascii=False)

    for r in rows:
        r["run_id"], r["ts_utc"], r["ts_ist"], r["nse_phase"] = STAMP, meta["ts_utc"], meta["ts_ist"], meta["nse_phase"]
        r["usdt_inr_mid"], r["usd_inr_official"] = fx.get("usdt_inr_mid"), fx.get("usd_inr_official")
    cols = ["run_id", "ts_utc", "ts_ist", "nse_phase", "venue", "symbol", "label", "kind", "ccy", "liquidity_source",
            "identifier", "expiry", "strike", "moneyness", "dte", "lot", "tick", "contract_value", "qty_mult", "px_to_inr",
            "usdt_inr_mid", "usd_inr_official", "underlying", "last", "bid", "ask", "mid", "spread", "spread_bps",
            "bid_qty_units", "ask_qty_units", "depth_touch_bid_inr", "depth_touch_ask_inr"]
    cols += [f"depth_{side}_{k}bp_inr" for k in DEPTH_BANDS_BPS for side in ("bid", "ask")]
    cols += [f"ic_{side}_{tag}_bps" for tag in ("1L", "10L", "50L", "1cr") for side in ("buy", "sell")]
    cols += ["visible_levels_bid", "visible_levels_ask", "total_buy_units", "total_sell_units", "volume", "oi",
             "notional_traded_inr", "source_ts"]
    with open(os.path.join(OUT_ROWS, f"{STAMP}.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})

    log(f"Done: {len(raw_instruments)} instruments, {len(errors)} errors, {meta['duration_s']}s")
    if errors:
        for e in errors:
            log(f"  - {e['where']}: {e['error'][:160]}")
    # Exit 0 even with partial errors so the workflow still commits what it has.
    return 0


if __name__ == "__main__":
    sys.exit(main())
