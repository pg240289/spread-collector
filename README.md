# NSE F&O vs Indian crypto venues — order-book sampler

Samples the live order book of NSE index/stock futures and options and of Indian crypto
venues every 10 minutes during NSE market hours, and commits the snapshots to this repo.

## What is sampled

| Venue | Instruments | Book depth | Liquidity source |
|---|---|---|---|
| NSE F&O (nseindia.com) | Nifty & Bank Nifty futures (near + next month); Nifty ATM CE/PE and ±100 OTM for the nearest weekly expiry; Nifty and Bank Nifty ATM CE/PE for the monthly expiry; 12 single-stock futures (6 liquid, 6 thin); Reliance and HDFC Bank ATM options | 5 levels (all NSE shows publicly) | native |
| Delta Exchange India | BTCUSD, ETHUSD perpetuals | 20 levels | native (registered market-maker programme) |
| CoinDCX spot | BTC/INR, ETH/INR, USDT/INR (native INR books); BTC/USDT, ETH/USDT (Binance-routed) | 20 levels | native / routed |
| CoinDCX Futures | BTC, ETH USDT-quoted perps with INR margin | 20 levels | routed (Binance) |
| ZebPay | BTC/INR, ETH/INR, USDT/INR spot; BTCINR, ETHINR futures | 20 levels | native / mirrored |
| Pi42 | BTCINR, ETHINR perps (only if the API answers a non-browser client) | 20 levels | mirrored |
| WazirX, Giottus | BTC/INR spot | 20 levels | native |

Also recorded each run: USDT/INR mid from CoinDCX (the conversion an INR trader actually gets) and the
official USD/INR reference, so USD-quoted crypto depth can be expressed in rupees.

## Metrics per instrument (data/rows/*.csv)

* `spread`, `spread_bps` — quoted spread, absolute and in basis points of mid
* `depth_touch_bid_inr`, `depth_touch_ask_inr` — rupee value resting at the best bid / best ask
* `depth_{bid|ask}_{1,2,5,10,25,50}bp_inr` — cumulative rupee depth within k bps of mid
* `ic_{buy|sell}_{1L,10L,50L,1cr}_bps` — NSE-style impact cost: (average fill − mid) / mid for a market
  order of ₹1 lakh / ₹10 lakh / ₹50 lakh / ₹1 crore walked through the visible book; blank when the
  visible book is too shallow to fill the order (NSE shows only 5 levels)
* `visible_levels_*`, `total_buy_units`, `total_sell_units`, `volume`, `oi`, `lot`, `tick`, `underlying`

Raw books (every level, every run) are in `data/raw/*.json`.

## Schedule

`.github/workflows/sample.yml` runs Monday–Friday from 09:18 to 15:43 IST every ~10 minutes
(GitHub cron is best-effort; expect 0–10 minutes of drift). Disable the workflow from the Actions tab
when the study is over.

## Running it yourself

```
pip install requests
python sampler.py
```

Each run writes one JSON and one CSV named by the UTC timestamp. Fees, funding and taxes are
deliberately out of scope: this measures spreads and depth only.
