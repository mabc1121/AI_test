# Kit 4: passive execution test and Bitfinex L2 recorder

Two scripts. The first answers, on your 92 Bybit days, whether the one-minute imbalance edge survives being filled
passively at zero fees. The second starts collecting Bitfinex L2 data in the same schema, so the whole toolchain
(kit v2 `bars2`, the passive simulator, the cost sensitivity) runs on Bitfinex data once a few weeks exist.

## 1. Passive simulator on the Bybit files (10 minutes quick, about an hour for the full grid)

```
python passive_sim.py --glob "data/bybit_1s/*.parquet" --out out_passive --quick     # primary cell + random baseline
python passive_sim.py --glob "data/bybit_1s/*.parquet" --out out_passive             # + 112 secondary cells
```

Pre-registered primary cell and pass rule are in the script header (depth imbalance within 0.5 bps, threshold 0.5,
30-second entry patience, resting take-profit 0.5 bps with 60-second patience then market exit with 0.5 bps slippage,
0.01 BTC, zero fees; pass = net per round trip > 0, daily t ≥ 3, ≥ 10 of 13 weeks positive, and ≥ 0.2 bps better
than the random-side baseline). Send back `out_passive/passive_cells.csv` and `out_passive/passive_trades_primary.csv`.

What the two sample days already showed (so you know what to expect): every cell negative; passive exits earn the
take-profit, the 30% of trades that need a market exit lose 6 to 10 bps each, the mid sits 0.7 bps against you at
the moment you are filled, and the random-side baseline is indistinguishable from the signal-conditioned version.

## 2. Bitfinex recorder (start it now; it needs to run for at least two weeks)

```
pip install websockets pandas numpy pyarrow
python bitfinex_recorder.py --selftest                              # must print SELFTEST PASS
python bitfinex_recorder.py --symbol tBTCF0:USTF0 --out data_bitfinex
```

- `tBTCF0:USTF0` is the Bitfinex BTC perpetual (USDt margined). For spot use `tBTCUSD`.
- Within a minute you should see `connected, subscribed ...`, and every five minutes `flushed YYYY-MM-DD: N rows`.
  Check after ten minutes with:
  ```python
  import pandas as pd; d = pd.read_parquet("data_bitfinex/bitfinex_1s/<today>.parquet"); print(d.tail(3).T); print(d.ok.mean())
  ```
  `ok` should be 1.0 and `msgs` mostly > 0. Bitfinex's perpetual book is quieter than Bybit's, so seconds with
  `msgs = 0` will be more common than in your Bybit files; that is expected.
- Run it under a supervisor that restarts on failure (systemd, pm2, or Windows Task Scheduler) on a machine with an
  NTP-synced clock and a stable connection. Disk: roughly 10 MB per day. Reconnects are automatic; the `ok` column
  marks seconds recorded while connected.
- Output schema is identical to your Bybit 1-second files plus the `ok` column, with level files in
  `data_bitfinex/bitfinex_lvl/YYYY-MM-DD.npy`. Point kit v2's `V2_RAW_GLOB` and `V2_LVL_PATTERN` at these folders and
  everything runs unchanged. Two differences to keep in mind when comparing: timestamps are local receipt time, and
  the Bitfinex book is order-level, aggregated per price here.

## 3. What happens next

With two weeks of Bitfinex data the questions become answerable on the venue itself: its spread and depth, the
realised per-fill P&L of passive quoting with and without the imbalance signal, and whether spread capture on a wider
book covers the adverse selection that the Bybit simulation measures at about 0.7 bps per fill.

---

## 4. The code

### `passive_sim.py`

Passive-execution simulator on the 1-second files; protocol in the header.

```python
#!/usr/bin/env python3
"""
passive_sim.py — passive (maker) execution of the 1-minute book-imbalance signal, simulated on 1-second L2 rows.

Question: does the ~0.5 bps/minute imbalance edge survive being FILLED passively, at zero fees?

Strategy (long side; short is symmetric)
  * at the end of second t, if flat and the signal s_t >= THETA (bid-heavy book), post a BUY limit of size Q at the
    best bid B = bid1_t, joining the queue behind bsz1_t; cancel if not filled within T_ENTRY seconds.
  * fill model for a resting BUY at B (evaluated each later second u; our own order is not in the recorded book):
      - a trade printed below our price (px_min_u <= B - tick)                       -> filled at B
      - best bid still at B and cumulative aggressive SELL volume >= queue ahead + Q  -> filled at B
      - best bid moved below B (level gone) AND an aggressive sell occurred this second -> filled at B
        (we would have been the best bid; a vanished level with no sells is treated as cancellations, we keep waiting)
      - best bid above B: we are behind the touch, nothing is consumed, we wait
  * exit (EXIT_MODE):
      "target": on a fill at second u, rest a SELL at entry * (1 + TP_BPS/1e4) with the mirrored fill model, giving the
                signal its horizon; if not filled within T_EXIT seconds, exit at market at bid1_v minus SLIP_BPS.
      "touch":  rest a SELL at the best ask at fill time (pure spread capture), same expiry and market fallback.
  * one position at a time; positions are flattened at market at the end of each day.
  * baseline: the same mechanics with a random side every time we are flat (no signal) -> pure passive-execution P&L.

PRE-REGISTERED PRIMARY CELL (fixed before the 92-day run; the two sample days were used only to debug mechanics):
  signal = depth imbalance within 0.5 bps (dimb05), THETA = 0.5, T_ENTRY = 30 s, EXIT_MODE = "target",
  TP_BPS = 0.5, T_EXIT = 60 s, Q = 0.01 BTC, SLIP_BPS = 0.5 on market legs, zero fees.
  Pass requires: mean net per round trip > 0, t-stat of daily P&L >= 3, positive in >= 10 of 13 weeks, and net per
  round trip at least 0.2 bps above the random-side baseline with the same parameters. Everything else is secondary.

usage: python passive_sim.py --glob "data/bybit_1s/*.parquet" [--out out_passive] [--max-days N]
"""
import argparse
import glob
import math
import os
import time

import numpy as np
import pandas as pd

TICK = 0.1
PRIMARY = dict(signal="dimb05", theta=0.5, t_entry=30, exit="target", tp=0.5, t_exit=60, q=0.01, slip=0.5)
GRID = [dict(signal=s, theta=th, t_entry=te, exit=ex, tp=tp, t_exit=tx, q=0.01, slip=0.5)
        for s in ("dimb05", "bimb") for th in (0.3, 0.5, 0.7) for te in (10, 30, 60)
        for ex, tp in (("target", 0.5), ("target", 1.0), ("touch", 0.0)) for tx in (60, 120)]
SECONDARY_Q = [dict(PRIMARY, q=0.1), dict(PRIMARY, slip=1.0), dict(PRIMARY, slip=0.0), dict(PRIMARY, exit="touch", tp=0.0)]


def load_day(path):
    d = pd.read_parquet(path)
    if "sec" in d.columns:
        d = d.set_index("sec")
    d = d.sort_index()
    bid, ask = d["bid1"].to_numpy(float), d["ask1"].to_numpy(float)
    bsz, asz = d["bsz1"].to_numpy(float), d["asz1"].to_numpy(float)
    out = {
        "sec": d.index.to_numpy().astype(np.int64), "bid": bid, "ask": ask, "bsz": bsz, "asz": asz,
        "buy": np.nan_to_num(d["buy_vol"].to_numpy(float)), "sell": np.nan_to_num(d["sell_vol"].to_numpy(float)),
        "pxmin": d["px_min"].to_numpy(float), "pxmax": d["px_max"].to_numpy(float),
        "mid": (bid + ask) / 2,
        "bimb": (bsz - asz) / np.maximum(bsz + asz, 1e-9),
        "dimb05": (d["bd0.5"].to_numpy(float) - d["ad0.5"].to_numpy(float)) / np.maximum(d["bd0.5"].to_numpy(float) + d["ad0.5"].to_numpy(float), 1e-9),
    }
    return out


def simulate_day(D, p, rng=None):
    """returns list of round trips for one day: (entry_sec, side, entry_px, exit_px, exit_sec, exit_kind, mid_at_entry, mid_60s)"""
    n = len(D["sec"])
    sig = D[p["signal"]] if rng is None else None
    bid, ask, bsz, asz, buy, sell, pxmin, pxmax, mid = (D[k] for k in ("bid", "ask", "bsz", "asz", "buy", "sell", "pxmin", "pxmax", "mid"))
    th, te, tx, q, slip = p["theta"], p["t_entry"], p["t_exit"], p["q"], p["slip"]
    exit_mode, tp = p.get("exit", "target"), p.get("tp", 0.5)

    def buy_filled(u, px, st):
        """resting BUY at px; st = [consumed]. Returns True if filled during second u."""
        if np.isfinite(pxmin[u]) and pxmin[u] <= px - TICK:
            return True
        if bid[u] == px:
            st[0] += sell[u]
            return st[0] >= queue_of(st) + q
        if bid[u] < px:
            return sell[u] > 0            # we would be the best bid; any aggressive sell hits us
        return False                       # bid above us: behind the touch

    def sell_filled(u, px, st):
        if np.isfinite(pxmax[u]) and pxmax[u] >= px + TICK:
            return True
        if ask[u] == px:
            st[0] += buy[u]
            return st[0] >= queue_of(st) + q
        if ask[u] > px:
            return buy[u] > 0
        return False

    def queue_of(st):
        return st[1]

    trips = []
    state = "flat"              # flat | entry | exit
    t = 0
    while t < n - 1:
        if state == "flat":
            if rng is not None:
                side = 1 if rng.random() < 0.5 else -1
            else:
                s = sig[t]
                side = 1 if s >= th else (-1 if s <= -th else 0)
            if side == 0 or not np.isfinite(bid[t]):
                t += 1
                continue
            px = bid[t] if side > 0 else ask[t]
            st = [0.0, bsz[t] if side > 0 else asz[t]]        # [consumed, queue ahead]
            expiry = t + te
            state = "entry"
            t += 1
            continue
        if state == "entry":
            u = t
            filled = buy_filled(u, px, st) if side > 0 else sell_filled(u, px, st)
            if filled:
                entry_px, entry_sec, m0 = px, u, mid[u]
                if exit_mode == "touch":
                    xpx = ask[u] if side > 0 else bid[u]
                else:
                    xpx = round((entry_px * (1 + side * tp / 1e4)) / TICK) * TICK
                    if side > 0:
                        xpx = max(xpx, ask[u])                 # never rest a sell below the current ask
                    else:
                        xpx = min(xpx, bid[u])
                xst = [0.0, (asz[u] if side > 0 else bsz[u]) if (xpx == (ask[u] if side > 0 else bid[u])) else 0.0]
                xexpiry = u + tx
                state = "exit"
            elif u >= expiry:
                state = "flat"
            t += 1
            continue
        if state == "exit":
            v = t
            done, kind, out_px = False, None, None
            if (sell_filled(v, xpx, xst) if side > 0 else buy_filled(v, xpx, xst)):
                done, kind, out_px = True, "passive", xpx
            if not done and (v >= xexpiry or v >= n - 2):
                done, kind = True, "market"
                out_px = bid[v] * (1 - slip / 1e4) if side > 0 else ask[v] * (1 + slip / 1e4)
            if done:
                m60 = mid[min(entry_sec + 60, n - 1)]
                trips.append((entry_sec, side, entry_px, out_px, v, kind, m0, m60))
                state = "flat"
            t += 1
            continue
    return trips


def run(files, params, label, rng_seed=None):
    rows = []
    for f in files:
        D = load_day(f)
        rng = np.random.default_rng(rng_seed) if rng_seed is not None else None
        for (es, side, ep, xp, xs, kind, m0, m60) in simulate_day(D, params, rng):
            pnl = side * (xp - ep) / ep * 1e4
            rows.append({"day": os.path.basename(f)[:10], "entry_sec": int(D["sec"][es]), "side": side, "entry_px": ep,
                         "exit_px": xp, "hold_s": int(D["sec"][xs] - D["sec"][es]), "exit": kind, "pnl_bps": pnl,
                         "adverse_bps": side * (m0 - ep) / ep * 1e4,           # mid vs our fill price at fill time
                         "mid_move_60s_bps": side * (m60 - m0) / m0 * 1e4})    # signal realisation after the fill
    T = pd.DataFrame(rows)
    T["label"] = label
    return T


def summarize(T, label, n_days):
    if T.empty:
        return {"label": label, "round_trips": 0}
    daily = T.groupby("day")["pnl_bps"].sum()
    wk = T.assign(week=pd.to_datetime(T["day"]).dt.to_period("W")).groupby("week")["pnl_bps"].sum()
    t = daily.mean() / (daily.std(ddof=1) / math.sqrt(len(daily))) if len(daily) > 2 and daily.std(ddof=1) > 0 else np.nan
    return {"label": label, "round_trips": len(T), "per_day": len(T) / n_days, "mean_pnl_bps": T["pnl_bps"].mean(),
            "median_pnl_bps": T["pnl_bps"].median(), "passive_exit_share": (T["exit"] == "passive").mean(),
            "pnl_passive_exits": T.loc[T["exit"] == "passive", "pnl_bps"].mean(),
            "pnl_market_exits": T.loc[T["exit"] == "market", "pnl_bps"].mean(), "hold_s_median": T["hold_s"].median(),
            "adverse_at_fill_bps": T["adverse_bps"].mean(), "mid_move_60s_bps": T["mid_move_60s_bps"].mean(),
            "daily_pnl_mean_bps": daily.mean(), "t_daily": t, "weeks_pos": f"{int((wk > 0).sum())}/{len(wk)}",
            "days_pos": f"{int((daily > 0).sum())}/{len(daily)}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="data/bybit_1s/*.parquet")
    ap.add_argument("--out", default="out_passive")
    ap.add_argument("--max-days", type=int, default=None)
    ap.add_argument("--quick", action="store_true", help="primary cell + baseline only")
    a = ap.parse_args()
    files = sorted(glob.glob(a.glob))
    if a.max_days:
        files = files[: a.max_days]
    if not files:
        raise SystemExit(f"no files match {a.glob}")
    os.makedirs(a.out, exist_ok=True)
    n_days = len(files)
    t0 = time.time()
    results = []
    T = run(files, PRIMARY, "PRIMARY " + str(PRIMARY))
    T.to_csv(os.path.join(a.out, "passive_trades_primary.csv"), index=False)
    results.append(summarize(T, "PRIMARY " + str(PRIMARY), n_days))
    B = run(files, PRIMARY, "BASELINE random side " + str(PRIMARY), rng_seed=1)
    results.append(summarize(B, "BASELINE random side " + str(PRIMARY), n_days))
    print(f"primary + baseline done ({time.time()-t0:.0f}s)")
    if not a.quick:
        for p in SECONDARY_Q + GRID:
            if p == PRIMARY:
                continue
            results.append(summarize(run(files, p, str(p)), str(p), n_days))
        print(f"grid done ({time.time()-t0:.0f}s)")
    R = pd.DataFrame(results)
    R.to_csv(os.path.join(a.out, "passive_cells.csv"), index=False)
    pd.set_option("display.width", 250)
    cols = ["label", "round_trips", "per_day", "mean_pnl_bps", "median_pnl_bps", "passive_exit_share", "pnl_passive_exits",
            "pnl_market_exits", "hold_s_median", "adverse_at_fill_bps", "mid_move_60s_bps", "t_daily", "weeks_pos", "days_pos"]
    print(R[cols].round(3).to_string(index=False))
    P, Bs = results[0], results[1]
    ok = (P.get("round_trips", 0) > 0 and P["mean_pnl_bps"] > 0 and P["t_daily"] >= 3 and
          int(P["weeks_pos"].split("/")[0]) >= min(10, int(P["weeks_pos"].split("/")[1])) and
          P["mean_pnl_bps"] - Bs.get("mean_pnl_bps", 0) >= 0.2)
    print("\nPRIMARY VERDICT:", "PASS" if ok else "FAIL",
          f"| net per round trip {P.get('mean_pnl_bps', float('nan')):.3f} bps vs baseline {Bs.get('mean_pnl_bps', float('nan')):.3f}, "
          f"t {P.get('t_daily', float('nan')):.2f}, weeks {P.get('weeks_pos')}")


if __name__ == "__main__":
    main()
```

### `bitfinex_recorder.py`

Bitfinex R0 book + trades recorder writing the Bybit schema; run --selftest first.

```python
#!/usr/bin/env python3
"""
bitfinex_recorder.py — record Bitfinex L2 (order-level, R0) book + trades and write 1-second rows in the SAME schema
as the Bybit files (bid1 ask1 bsz1 asz1 mid_hi mid_lo ofi add_b rem_b add_a rem_a msgs bd0.5 bd1 bd2 bd3 ad0.5 ad1 ad2
ad3 bdtot adtot breach areach buy_vol sell_vol n_trades big_buy big_sell px_min px_max px_last; index 'sec'), plus the
top-10 level file (86400 x 40 float32: bid_dist_1..10, bid_size_1..10, ask_dist_1..10, ask_size_1..10).
Extra column 'ok' = 1 when the connection was alive for the whole second.

  pip install websockets pandas numpy pyarrow
  python bitfinex_recorder.py --symbol tBTCF0:USTF0 --out data_bitfinex        # BTC perpetual (USDt margined)
  python bitfinex_recorder.py --selftest                                        # offline check of the aggregation

Files: <out>/bitfinex_1s/YYYY-MM-DD.parquet and <out>/bitfinex_lvl/YYYY-MM-DD.npy, UTC days, flushed every 5 minutes.
Timestamps are local receipt time (UTC); Bitfinex R0 book updates carry no exchange timestamp.
Run it under a supervisor (systemd, pm2, Windows Task Scheduler "restart on failure") and keep the clock NTP-synced.
"""
import argparse
import asyncio
import json
import math
import os
import sys
import time
from collections import defaultdict

import numpy as np
import pandas as pd

WS_URL = "wss://api-pub.bitfinex.com/ws/2"
COLS = ["bid1", "ask1", "bsz1", "asz1", "mid_hi", "mid_lo", "ofi", "add_b", "rem_b", "add_a", "rem_a", "msgs",
        "bd0.5", "bd1", "bd2", "bd3", "ad0.5", "ad1", "ad2", "ad3", "bdtot", "adtot", "breach", "areach",
        "buy_vol", "sell_vol", "n_trades", "big_buy", "big_sell", "px_min", "px_max", "px_last", "ok"]


class Book:
    """order-level book; aggregates per price on demand"""

    def __init__(self):
        self.orders = {}          # order_id -> (price, amount)  amount>0 bid, <0 ask
        self.reset_second()
        self.prev_best = None     # (pb, qb, pa, qa)

    def reset_second(self):
        self.ofi = 0.0
        self.add = {"b": 0.0, "a": 0.0}
        self.rem = {"b": 0.0, "a": 0.0}
        self.msgs = 0
        self.mid_hi = -math.inf
        self.mid_lo = math.inf

    def levels(self):
        bids, asks = defaultdict(float), defaultdict(float)
        for p, a in self.orders.values():
            if a > 0:
                bids[p] += a
            elif a < 0:
                asks[p] += -a
        return bids, asks

    def best(self):
        bids, asks = self.levels()
        if not bids or not asks:
            return None
        pb, pa = max(bids), min(asks)
        return pb, bids[pb], pa, asks[pa]

    def apply(self, oid, price, amount, snapshot=False):
        """one R0 message; price == 0 means delete"""
        pre = self.best()
        mid_pre = (pre[0] + pre[2]) / 2 if pre else None
        old = self.orders.get(oid)
        if price == 0:
            if old is not None:
                del self.orders[oid]
                self._flow(old[0], -abs(old[1]), "b" if old[1] > 0 else "a", mid_pre)
        else:
            if old is not None and old[0] != price:
                self._flow(old[0], -abs(old[1]), "b" if old[1] > 0 else "a", mid_pre)
                old = None
            delta = abs(amount) - (abs(old[1]) if old is not None else 0.0)
            self._flow(price, delta, "b" if amount > 0 else "a", mid_pre)
            self.orders[oid] = (price, amount)
        if snapshot:
            self.reset_second()
            self.prev_best = self.best()
            return
        self.msgs += 1
        post = self.best()
        if pre and post:
            pb, qb, pa, qa = pre
            b, sb, a, sa = post
            self.ofi += (sb if b >= pb else 0) - (qb if b <= pb else 0) - (sa if a <= pa else 0) + (qa if a >= pa else 0)
        if post:
            m = (post[0] + post[2]) / 2
            self.mid_hi, self.mid_lo = max(self.mid_hi, m), min(self.mid_lo, m)

    def _flow(self, price, delta, side, mid_pre):
        if mid_pre is None or abs(price - mid_pre) / mid_pre * 1e4 > 2.0:
            return
        if delta > 0:
            self.add[side] += delta
        elif delta < 0:
            self.rem[side] += -delta

    def snapshot_row(self, trades, ok):
        bids, asks = self.levels()
        row = dict.fromkeys(COLS, np.nan)
        lv = np.full(40, np.nan, dtype=np.float32)
        if bids and asks:
            pb, pa = max(bids), min(asks)
            mid = (pb + pa) / 2
            row.update(bid1=pb, ask1=pa, bsz1=bids[pb], asz1=asks[pa],
                       mid_hi=self.mid_hi if self.mid_hi > -math.inf else mid,
                       mid_lo=self.mid_lo if self.mid_lo < math.inf else mid)
            for k, name in ((0.5, "0.5"), (1, "1"), (2, "2"), (3, "3")):
                row[f"bd{name}"] = sum(a for p, a in bids.items() if p >= mid * (1 - k / 1e4))
                row[f"ad{name}"] = sum(a for p, a in asks.items() if p <= mid * (1 + k / 1e4))
            row["bdtot"], row["adtot"] = sum(bids.values()), sum(asks.values())
            row["breach"] = (mid - min(bids)) / mid * 1e4
            row["areach"] = (max(asks) - mid) / mid * 1e4
            bl = sorted(bids.items(), key=lambda x: -x[0])[:10]
            al = sorted(asks.items(), key=lambda x: x[0])[:10]
            for i, (p, a) in enumerate(bl):
                lv[i], lv[10 + i] = (p - mid) / mid * 1e4, a
            for i, (p, a) in enumerate(al):
                lv[20 + i], lv[30 + i] = (p - mid) / mid * 1e4, a
        row.update(ofi=self.ofi, add_b=self.add["b"], rem_b=self.rem["b"], add_a=self.add["a"], rem_a=self.rem["a"],
                   msgs=self.msgs, ok=1.0 if ok else 0.0)
        buys = [t for t in trades if t[0] > 0]
        sells = [t for t in trades if t[0] < 0]
        row.update(buy_vol=sum(a for a, _ in buys), sell_vol=sum(-a for a, _ in sells), n_trades=len(trades),
                   big_buy=sum(a for a, _ in buys if a >= 1.0), big_sell=sum(-a for a, _ in sells if -a >= 1.0),
                   px_min=min((p for _, p in trades), default=np.nan), px_max=max((p for _, p in trades), default=np.nan),
                   px_last=trades[-1][1] if trades else np.nan)
        self.reset_second()
        return row, lv


class Recorder:
    def __init__(self, out, symbol):
        self.out, self.symbol = out, symbol
        os.makedirs(os.path.join(out, "bitfinex_1s"), exist_ok=True)
        os.makedirs(os.path.join(out, "bitfinex_lvl"), exist_ok=True)
        self.book = Book()
        self.trades = []
        self.rows, self.lvls = {}, {}
        self.day = None
        self.connected = False
        self.last_flush = time.time()
        self.chan = {}
        self.last_sec = None

    def on_message(self, msg):
        if isinstance(msg, dict):
            if msg.get("event") == "subscribed":
                self.chan[msg["chanId"]] = msg["channel"]
            elif msg.get("event") in ("error",):
                print("ws error:", msg, flush=True)
            return
        if not isinstance(msg, list) or len(msg) < 2:
            return
        ch = self.chan.get(msg[0])
        if msg[1] == "hb":
            return
        if ch == "book":
            body = msg[1]
            if body and isinstance(body[0], list):            # snapshot
                self.book.orders.clear()
                for oid, price, amount in body:
                    self.book.apply(int(oid), float(price), float(amount), snapshot=True)
                self.book.prev_best = self.book.best()
            else:
                oid, price, amount = body
                self.book.apply(int(oid), float(price), float(amount))
        elif ch == "trades":
            if msg[1] == "tu":                                  # trade update (final); 'te' is the early notice
                _id, mts, amount, price = msg[2]
                self.trades.append((float(amount), float(price)))
            elif isinstance(msg[1], list):                      # snapshot of recent trades: ignore (already past)
                pass

    def tick(self, sec):
        """close the second `sec` (int UTC seconds): emit one row"""
        day = time.strftime("%Y-%m-%d", time.gmtime(sec))
        if self.day is not None and day != self.day:
            self.flush(final=True)
        self.day = day
        row, lv = self.book.snapshot_row(self.trades, self.connected)
        self.trades = []
        self.rows[sec] = row
        self.lvls[sec] = lv
        if time.time() - self.last_flush > 300:
            self.flush()

    def flush(self, final=False):
        if not self.rows:
            return
        df = pd.DataFrame.from_dict(self.rows, orient="index").sort_index()
        df.index.name = "sec"
        df = df[COLS]
        day = self.day
        df.to_parquet(os.path.join(self.out, "bitfinex_1s", f"{day}.parquet"))
        day_start = int(pd.Timestamp(day, tz="UTC").timestamp())
        arr = np.full((86400, 40), np.nan, dtype=np.float32)
        for sec, lv in self.lvls.items():
            i = sec - day_start
            if 0 <= i < 86400:
                arr[i] = lv
        np.save(os.path.join(self.out, "bitfinex_lvl", f"{day}.npy"), arr)
        self.last_flush = time.time()
        print(f"{time.strftime('%H:%M:%S')} flushed {day}: {len(df)} rows ({'final' if final else 'partial'})", flush=True)
        if final:
            self.rows, self.lvls = {}, {}


async def clock(rec):
    """emit one row per wall-clock UTC second"""
    sec = int(time.time())
    while True:
        await asyncio.sleep(max(0.0, (sec + 1) - time.time()))
        rec.tick(sec)
        sec += 1


async def stream(rec, symbol, length):
    import websockets
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20, max_size=2 ** 24) as ws:
                await ws.send(json.dumps({"event": "conf", "flags": 0}))
                await ws.send(json.dumps({"event": "subscribe", "channel": "book", "symbol": symbol, "prec": "R0", "len": str(length)}))
                await ws.send(json.dumps({"event": "subscribe", "channel": "trades", "symbol": symbol}))
                rec.connected = True
                print(f"{time.strftime('%H:%M:%S')} connected, subscribed {symbol}", flush=True)
                async for raw in ws:
                    rec.on_message(json.loads(raw))
        except Exception as e:
            rec.connected = False
            print(f"{time.strftime('%H:%M:%S')} disconnected: {e!r}; reconnecting in 3s", flush=True)
            await asyncio.sleep(3)


def selftest():
    rec = Recorder("selftest_out", "tTEST")
    rec.connected = True
    rec.on_message({"event": "subscribed", "channel": "book", "chanId": 1})
    rec.on_message({"event": "subscribed", "channel": "trades", "chanId": 2})
    mid = 100000.0
    snap = [[i + 1, mid - 1 - i * 1.0, 0.5 + 0.1 * i] for i in range(20)] + [[100 + i, mid + 1 + i * 1.0, -(0.4 + 0.1 * i)] for i in range(20)]
    rec.on_message([1, snap])
    sec = int(time.time())
    rec.on_message([1, [1, mid - 1, 1.5]])          # bid size at the touch grows: OFI +1.0, add_b +1.0
    rec.on_message([1, [100, 0, -1]])                # best ask removed: OFI +0.4 (qa term), rem_a +0.4
    rec.on_message([1, [300, mid + 0.5, -0.2]])      # new best ask inside: OFI -0.2, add_a +0.2
    rec.on_message([2, "tu", [1, sec * 1000, 0.3, mid + 0.5]])
    rec.on_message([2, "tu", [2, sec * 1000, -1.2, mid - 1]])
    rec.tick(sec)
    row = rec.rows[sec]
    lv = rec.lvls[sec]
    print({k: round(v, 4) if isinstance(v, float) and np.isfinite(v) else v for k, v in row.items()})
    print("levels bid_dist_1..3:", lv[:3], " bid_size_1..3:", lv[10:13], " ask_dist_1..3:", lv[20:23])
    exp = {"bid1": mid - 1, "ask1": mid + 0.5, "bsz1": 1.5, "asz1": 0.2, "ofi": 1.0 + 0.4 - 0.2, "add_b": 1.0, "rem_a": 0.4,
           "add_a": 0.2, "msgs": 3, "buy_vol": 0.3, "sell_vol": 1.2, "n_trades": 2, "big_sell": 1.2, "px_last": mid - 1}
    bad = {k: (row[k], v) for k, v in exp.items() if not math.isclose(row[k], v, rel_tol=1e-9, abs_tol=1e-9)}
    print("SELFTEST", "PASS" if not bad else f"FAIL {bad}")
    rec.flush(final=True)
    return not bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="tBTCF0:USTF0", help="tBTCF0:USTF0 = BTC perpetual; tBTCUSD = spot")
    ap.add_argument("--out", default="data_bitfinex")
    ap.add_argument("--len", type=int, default=100, help="book length per side (25, 100, 250)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    rec = Recorder(a.out, a.symbol)

    async def run():
        await asyncio.gather(clock(rec), stream(rec, a.symbol, a.len))
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        rec.flush(final=True)


if __name__ == "__main__":
    main()
```
