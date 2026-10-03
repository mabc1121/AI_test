#!/usr/bin/env python3
"""
bitfinex_l3.py — order-level (R0, "level 3") features from the raw Bitfinex websocket logs, one row per second.

Everything here needs individual order ids, which the Bybit level-2 data cannot give:
  * order COUNTS per band (how many orders make up the depth), largest single resting order ("wall") and its distance
  * order AGE: depth within a band split into fresh (< 5 s) and old (>= 60 s) orders, median age
  * CANCELS split by the age of the cancelled order (fleeting vs. patient liquidity) and by size (walls pulled)
  * EXECUTIONS of resting orders (a removal/reduction at the touch that coincides with a trade print at that price),
    number of distinct orders and price levels executed per second (sweep breadth)
  * REFILLS: a new order posted at a price/side that was just executed in the same second (iceberg / hidden refresh proxy)

Side suffix: _b bids, _a asks. Bands in bps of the mid. Flows are per second; states are the book after the last
update in the second. Output: <out>/bitfinex_l3/YYYY-MM-DD.parquet indexed by Unix second.

  python bitfinex_l3.py --root raw --out data_bitfinex [--days 2026-09-25]
"""
import argparse
import glob
import gzip
import json
import math
import os
import re
import time
from collections import defaultdict

import numpy as np
import pandas as pd
from sortedcontainers import SortedDict

try:
    import orjson as fastjson

    def loads(s):
        return fastjson.loads(s)
except ImportError:  # pragma: no cover
    def loads(s):
        return json.loads(s)

BIG = 1.0            # BTC: a "wall" order
FRESH_MS = 5_000
OLD_MS = 60_000
FLOW_BPS = 2.0       # band for add/cancel/execution flows (same as the kit's add/rem)
WALL_BPS = 10.0      # band for wall placements / pulls
STATE = ["n1", "n3", "n10", "old3", "fresh3", "old10", "max3", "max10", "dmax10", "snap3", "medage3"]
FLOW = ["nadd", "ncxl", "cxl_fresh", "cxl_old", "exec", "nexec", "nexlvl", "refill", "bigadd", "bigcxl"]
COLS = ["mid", "bid1", "ask1"] + [f"{c}_{s}" for c in STATE + FLOW for s in ("b", "a")]


class L3Book:
    def __init__(self):
        self.orders = {}                     # oid -> (price, signed_amount, t0_ms, from_snapshot)
        self.bids, self.asks = SortedDict(), SortedDict()
        self.lvl = {}                        # (is_bid, price) -> set(oid)
        self.now = 0
        self.reset_second()

    def reset_second(self):
        self.f = defaultdict(float)          # flow counters keyed "<name>_<side>"
        self.pending = []                    # touch removals: (is_bid, price, vol, age_ms, size)
        self.adds = []                       # (is_bid, price, vol)

    def clear(self):
        self.orders.clear(); self.bids.clear(); self.asks.clear(); self.lvl.clear()

    def best(self):
        if not self.bids or not self.asks:
            return None
        return self.bids.peekitem(-1)[0], self.asks.peekitem(0)[0]

    def _lvl_add(self, oid, price, size, is_bid):
        side = self.bids if is_bid else self.asks
        side[price] = side.get(price, 0.0) + size
        self.lvl.setdefault((is_bid, price), set()).add(oid)

    def _lvl_del(self, oid, price, size, is_bid):
        side = self.bids if is_bid else self.asks
        new = side.get(price, 0.0) - size
        if new <= 1e-12:
            side.pop(price, None)
        else:
            side[price] = new
        s = self.lvl.get((is_bid, price))
        if s is not None:
            s.discard(oid)
            if not s:
                del self.lvl[(is_bid, price)]

    def load_snapshot(self, rows, ms):
        self.clear()
        for oid, price, amount in rows:
            oid, price, amount = int(oid), float(price), float(amount)
            self.orders[oid] = (price, amount, ms, True)
            self._lvl_add(oid, price, abs(amount), amount > 0)
        self.reset_second()

    def _in(self, price, mid, bps):
        return mid is not None and abs(price - mid) / mid * 1e4 <= bps

    def _removed(self, oid, price, vol, is_bid, age, mid, at_touch, size):
        """a resting order (part) disappeared: execution candidate at the touch, cancel elsewhere"""
        s = "b" if is_bid else "a"
        if at_touch:
            self.pending.append((is_bid, price, vol, age, size))
        elif self._in(price, mid, FLOW_BPS):
            self.f[f"ncxl_{s}"] += 1
            if age < FRESH_MS:
                self.f[f"cxl_fresh_{s}"] += vol
            elif age >= OLD_MS:
                self.f[f"cxl_old_{s}"] += vol
        if size >= BIG and vol >= BIG * 0.5 and self._in(price, mid, WALL_BPS):
            self.f[f"bigcxl_{s}"] += vol      # provisional; executions are moved out of this at finalize

    def _added(self, price, vol, is_bid, mid):
        s = "b" if is_bid else "a"
        if self._in(price, mid, FLOW_BPS):
            self.f[f"nadd_{s}"] += 1
            self.adds.append((is_bid, price, vol))
        if vol >= BIG and self._in(price, mid, WALL_BPS):
            self.f[f"bigadd_{s}"] += vol

    def update(self, oid, price, amount, ms):
        self.now = ms
        b = self.best()
        mid = (b[0] + b[1]) / 2 if b else None
        old = self.orders.get(oid)
        if price == 0:
            if old is not None:
                op, oa, t0, _ = old
                is_bid = oa > 0
                at_touch = b is not None and op == (b[0] if is_bid else b[1])
                self._lvl_del(oid, op, abs(oa), is_bid)
                del self.orders[oid]
                self._removed(oid, op, abs(oa), is_bid, ms - t0, mid, at_touch, abs(oa))
            return
        is_bid = amount > 0
        if old is None:
            self.orders[oid] = (price, amount, ms, False)
            self._lvl_add(oid, price, abs(amount), is_bid)
            self._added(price, abs(amount), is_bid, mid)
            return
        op, oa, t0, snap = old
        if op != price:                                                   # moved: cancel at old price, add at new
            at_touch = b is not None and op == (b[0] if (oa > 0) else b[1])
            self._lvl_del(oid, op, abs(oa), oa > 0)
            self._removed(oid, op, abs(oa), oa > 0, ms - t0, mid, False, abs(oa))
            self.orders[oid] = (price, amount, ms, False)
            self._lvl_add(oid, price, abs(amount), is_bid)
            self._added(price, abs(amount), is_bid, mid)
            return
        d = abs(amount) - abs(oa)
        if d < 0:                                                         # reduced in place
            at_touch = b is not None and op == (b[0] if is_bid else b[1])
            self._lvl_del(oid, op, -d, is_bid)
            self.orders[oid] = (price, amount, t0, snap)
            self._removed(oid, op, -d, is_bid, ms - t0, mid, at_touch, abs(oa))
        elif d > 0:
            self._lvl_add(oid, price, d, is_bid)
            self.orders[oid] = (price, amount, t0, snap)
            self._added(price, d, is_bid, mid)

    def _band_stats(self, is_bid, mid, bps):
        """counts, old/fresh depth, largest order (+distance), snapshot share, median age within bps of mid"""
        side = self.bids if is_bid else self.asks
        rng = side.irange(minimum=mid * (1 - bps / 1e4)) if is_bid else side.irange(maximum=mid * (1 + bps / 1e4))
        n = 0; old = fresh = snap = tot = 0.0; mx = 0.0; dmx = np.nan; ages = []
        for p in rng:
            for oid in self.lvl.get((is_bid, p), ()):
                _, a, t0, fs = self.orders[oid]
                a = abs(a); age = self.now - t0
                n += 1; tot += a
                if age >= OLD_MS:
                    old += a
                elif age < FRESH_MS:
                    fresh += a
                if fs:
                    snap += a
                if a > mx:
                    mx, dmx = a, abs(p - mid) / mid * 1e4
                ages.append(age)
        return n, old, fresh, mx, dmx, (snap / tot if tot > 0 else np.nan), (float(np.median(ages)) / 1000 if ages else np.nan)

    def row(self, trades, prev_left):
        """finalize the second: touch removals are executions only up to the traded volume at that side/price
        (time priority: oldest orders first); the remainder are cancels. Unallocated trade volume carries one second."""
        tv = defaultdict(float)
        for amt, px in trades:                                            # aggressor buy (amt>0) hits asks at px
            tv[(amt < 0, px)] += abs(amt)                                 # key: (is_bid_side_hit, price)
        for k, v in prev_left.items():
            tv[k] += v
        exec_lv = {True: set(), False: set()}
        for is_bid, price, vol, age, size in sorted(self.pending, key=lambda x: -x[3]):
            s = "b" if is_bid else "a"
            avail = tv.get((is_bid, price), 0.0)
            ex = min(vol, avail) if avail > 1e-9 else 0.0
            if ex > 0:
                tv[(is_bid, price)] = avail - ex
                self.f[f"exec_{s}"] += ex; self.f[f"nexec_{s}"] += 1; exec_lv[is_bid].add(price)
                if size >= BIG and vol >= BIG * 0.5:
                    self.f[f"bigcxl_{s}"] -= ex                           # executed part was provisionally counted as a pull
            rest = vol - ex
            if rest > 1e-9:
                self.f[f"ncxl_{s}"] += 1
                if age < FRESH_MS:
                    self.f[f"cxl_fresh_{s}"] += rest
                elif age >= OLD_MS:
                    self.f[f"cxl_old_{s}"] += rest
        for is_bid in (True, False):
            self.f[f"nexlvl_{'b' if is_bid else 'a'}"] = len(exec_lv[is_bid])
        for is_bid, price, vol in self.adds:
            if price in exec_lv[is_bid]:
                self.f[f"refill_{'b' if is_bid else 'a'}"] += 1
        this_keys = {(amt < 0, px) for amt, px in trades}
        left = {k: v for k, v in tv.items() if v > 1e-9 and k in this_keys}
        r = [np.nan] * len(COLS)
        b = self.best()
        if b:
            mid = (b[0] + b[1]) / 2
            r[0:3] = [mid, b[0], b[1]]
            k = 3
            for is_bid in (True, False):
                n1 = self._band_stats(is_bid, mid, 1)[0]
                n3, old3, fresh3, max3, _, snap3, medage3 = self._band_stats(is_bid, mid, 3)
                n10, old10, _, max10, dmax10, _, _ = self._band_stats(is_bid, mid, 10)
                vals = dict(n1=n1, n3=n3, n10=n10, old3=old3, fresh3=fresh3, old10=old10, max3=max3, max10=max10,
                            dmax10=dmax10, snap3=snap3, medage3=medage3)
                s = "b" if is_bid else "a"
                for c in STATE:
                    r[COLS.index(f"{c}_{s}")] = vals[c]
        for c in FLOW:
            for s in ("b", "a"):
                r[COLS.index(f"{c}_{s}")] = self.f.get(f"{c}_{s}", 0.0)
        self.reset_second()
        return r, left


def session_starts(files):
    out = {}
    for f in files:
        with gzip.open(f, "rt") as fh:
            for line in fh:
                out[f] = loads(line)["r"]
                break
    return out


def run(root, out, days=None):
    files = sorted(glob.glob(os.path.join(root, "*", "*", "*", "*.jsonl.gz")))
    if days:
        files = [f for f in files if "-".join(re.search(r"(\d{4})/(\d{2})/(\d{2})/", f.replace("\\", "/")).groups()) in days]
    starts = session_starts(files)
    files.sort(key=lambda f: starts[f])
    sess_of = {f: int(re.search(r"-(\d+)\.jsonl\.gz$", f).group(1)) for f in files}
    sess_first = {}
    for f in files:
        s = sess_of[f]
        sess_first[s] = min(sess_first.get(s, starts[f]), starts[f])
    order = sorted(sess_first, key=lambda s: sess_first[s])
    cutoff = {s: (sess_first[order[i + 1]] if i + 1 < len(order) else math.inf) for i, s in enumerate(order)}
    os.makedirs(os.path.join(out, "bitfinex_l3"), exist_ok=True)

    book = L3Book()
    chan = {}
    trades = []
    prev_left = {}
    cur_sec = None
    rows = {}
    cur_day = None
    t0 = time.time()
    stats = defaultdict(int)

    def finalize(sec):
        nonlocal trades, prev_left
        r, prev_left = book.row(trades, prev_left)
        trades = []
        rows[sec] = r

    def flush_day(day):
        if not rows:
            return
        idx = np.array(sorted(rows))
        df = pd.DataFrame([rows[s] for s in idx], index=pd.Index(idx, name="sec"), columns=COLS).astype("float32")
        ds = int(pd.Timestamp(day, tz="UTC").timestamp())
        df = df[(df.index >= ds) & (df.index < ds + 86400)]
        df.to_parquet(os.path.join(out, "bitfinex_l3", f"{day}.parquet"))
        print(f"  wrote {day}: {len(df):,} rows, exec/s b {df['exec_b'].mean():.4f} a {df['exec_a'].mean():.4f}, "
              f"n3 b {df['n3_b'].median():.0f} a {df['n3_a'].median():.0f}, snap3 {df['snap3_b'].mean():.3f} ({time.time()-t0:.0f}s)", flush=True)

    for f in files:
        cut = cutoff[sess_of[f]]
        with gzip.open(f, "rt") as fh:
            for line in fh:
                rec = loads(line)
                r = rec["r"]
                if r >= cut:
                    continue
                if "k" in rec:
                    k = rec["k"]
                    if k == "connect":
                        chan.clear()
                    elif k == "book" and "cp" in rec and not book.orders:
                        book.load_snapshot([(int(o[0]), float(o[1]), float(o[2])) for o in rec["cp"]["orders"]], r)
                        for cid, name in rec["cp"].get("channels", {}).items():
                            chan[int(cid)] = name
                        stats["bootstrapped"] += 1
                    continue
                m = loads(rec["m"])
                if isinstance(m, dict):
                    if m.get("event") == "subscribed":
                        chan[m["chanId"]] = m["channel"]
                    continue
                ch = chan.get(m[0])
                if ch is None:
                    continue
                ms = m[-1] if isinstance(m[-1], (int, float)) and m[-1] > 1e12 else r
                sec = int(ms // 1000)
                if cur_sec is None:
                    cur_sec = sec
                if sec > cur_sec:
                    finalize(cur_sec)
                    for g in range(cur_sec + 1, sec):
                        book.now = g * 1000
                        finalize(g)
                    cur_sec = sec
                    day = time.strftime("%Y-%m-%d", time.gmtime(sec))
                    if cur_day is None:
                        cur_day = day
                    elif day != cur_day:
                        flush_day(cur_day)
                        rows = {}
                        cur_day = day
                tag = m[1]
                if tag == "hb":
                    continue
                if ch == "book":
                    if isinstance(tag, list) and tag and isinstance(tag[0], list):
                        book.load_snapshot(tag, ms)
                        stats["snapshots"] += 1
                    elif isinstance(tag, list):
                        book.update(int(tag[0]), float(tag[1]), float(tag[2]), ms)
                        stats["updates"] += 1
                elif ch == "trades" and tag == "tu":
                    trades.append((float(m[2][2]), float(m[2][3])))
                    stats["trades"] += 1
        print(f"{os.path.relpath(f, root)} done ({time.time()-t0:.0f}s)", flush=True)
    if cur_sec is not None:
        finalize(cur_sec)
        flush_day(cur_day)
    print("stats:", dict(stats))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="raw")
    ap.add_argument("--out", default="data_bitfinex")
    ap.add_argument("--days", nargs="*", default=None)
    a = ap.parse_args()
    run(a.root, a.out, a.days)
