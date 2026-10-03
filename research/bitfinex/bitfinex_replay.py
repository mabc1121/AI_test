#!/usr/bin/env python3
"""
bitfinex_replay.py — rebuild the Bitfinex R0 order book from raw websocket logs and write 1-second rows in the Bybit
schema (+ 'ok'), plus the top-10 level files.  Input layout: <root>/YYYY/MM/DD/HH-<session>.jsonl.gz where each line is
{"r": receipt_ms, "m": "<raw message json>"} or a control record {"r":..,"k":"start|connect|book",...}.
Control record k="book" is a checkpoint with the full order list; the replayed book is compared with every checkpoint.

  python bitfinex_replay.py --root raw --out data_bitfinex [--days 2026-09-25 2026-09-26]

Conventions (match SCHEMA.md of the Bybit files): row stamped s covers [s, s+1) by EXCHANGE timestamp; book-state
columns = state after the last update in the second; flows summed over the second; seconds without any message carry
the book forward with zero flows; 'ok' = 0 when no message (not even a heartbeat) arrived for > 20 s.
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

COLS = ["bid1", "ask1", "bsz1", "asz1", "mid_hi", "mid_lo", "ofi", "add_b", "rem_b", "add_a", "rem_a", "msgs",
        "bd0.5", "bd1", "bd2", "bd3", "ad0.5", "ad1", "ad2", "ad3", "bdtot", "adtot", "breach", "areach",
        "buy_vol", "sell_vol", "n_trades", "big_buy", "big_sell", "px_min", "px_max", "px_last", "ok"]
BIG = 1.0


class Book:
    def __init__(self):
        self.orders = {}
        self.bids, self.asks = SortedDict(), SortedDict()
        self.btot = self.atot = 0.0
        self.reset_second()

    def reset_second(self):
        self.ofi = 0.0
        self.add_b = self.rem_b = self.add_a = self.rem_a = 0.0
        self.msgs = 0
        self.mid_hi, self.mid_lo = -math.inf, math.inf

    def clear(self):
        self.orders.clear()
        self.bids.clear()
        self.asks.clear()
        self.btot = self.atot = 0.0

    def best(self):
        if not self.bids or not self.asks:
            return None
        pb, qb = self.bids.peekitem(-1)
        pa, qa = self.asks.peekitem(0)
        return pb, qb, pa, qa

    def _level_add(self, price, delta, is_bid):
        """add delta (signed size change, in absolute units) to the price level on the given side"""
        side = self.bids if is_bid else self.asks
        new = side.get(price, 0.0) + delta
        if is_bid:
            self.btot += delta
        else:
            self.atot += delta
        if new <= 1e-12:
            if price in side:
                del side[price]
        else:
            side[price] = new

    def _flow(self, price, delta, is_bid, mid_pre):
        if mid_pre is None or abs(price - mid_pre) / mid_pre * 1e4 > 2.0:
            return
        if is_bid:
            if delta > 0:
                self.add_b += delta
            else:
                self.rem_b += -delta
        else:
            if delta > 0:
                self.add_a += delta
            else:
                self.rem_a += -delta

    def load_snapshot(self, rows):
        self.clear()
        for oid, price, amount in rows:
            oid, price, amount = int(oid), float(price), float(amount)
            self.orders[oid] = (price, amount)
            self._level_add(price, abs(amount), amount > 0)
        self.reset_second()

    def update(self, oid, price, amount):
        pre = self.best()
        mid_pre = (pre[0] + pre[2]) / 2 if pre else None
        old = self.orders.get(oid)
        if price == 0:
            if old is not None:
                self._level_add(old[0], -abs(old[1]), old[1] > 0)
                self._flow(old[0], -abs(old[1]), old[1] > 0, mid_pre)
                del self.orders[oid]
        else:
            if old is not None:
                self._level_add(old[0], -abs(old[1]), old[1] > 0)
                if old[0] != price:
                    self._flow(old[0], -abs(old[1]), old[1] > 0, mid_pre)
                    self._flow(price, abs(amount), amount > 0, mid_pre)
                else:
                    self._flow(price, abs(amount) - abs(old[1]), amount > 0, mid_pre)
            else:
                self._flow(price, abs(amount), amount > 0, mid_pre)
            self.orders[oid] = (price, amount)
            self._level_add(price, abs(amount), amount > 0)
        self.msgs += 1
        post = self.best()
        if pre and post:
            pb, qb, pa, qa = pre
            b, sb, a, sa = post
            self.ofi += (sb if b >= pb else 0) - (qb if b <= pb else 0) - (sa if a <= pa else 0) + (qa if a >= pa else 0)
        if post:
            m = (post[0] + post[2]) / 2
            if m > self.mid_hi:
                self.mid_hi = m
            if m < self.mid_lo:
                self.mid_lo = m

    def row(self, trades, ok):
        r = [np.nan] * len(COLS)
        lv = np.full(40, np.nan, dtype=np.float32)
        b = self.best()
        if b:
            pb, qb, pa, qa = b
            mid = (pb + pa) / 2
            r[0:6] = [pb, pa, qb, qa, self.mid_hi if self.mid_hi > -math.inf else mid, self.mid_lo if self.mid_lo < math.inf else mid]
            for j, k in enumerate((0.5, 1, 2, 3)):
                r[12 + j] = sum(self.bids[p] for p in self.bids.irange(minimum=mid * (1 - k / 1e4)))
                r[16 + j] = sum(self.asks[p] for p in self.asks.irange(maximum=mid * (1 + k / 1e4)))
            r[20], r[21] = self.btot, self.atot
            r[22] = (mid - self.bids.peekitem(0)[0]) / mid * 1e4
            r[23] = (self.asks.peekitem(-1)[0] - mid) / mid * 1e4
            nb, na = len(self.bids), len(self.asks)
            for i in range(min(10, nb)):
                p, a = self.bids.peekitem(nb - 1 - i)
                lv[i], lv[10 + i] = (p - mid) / mid * 1e4, a
            for i in range(min(10, na)):
                p, a = self.asks.peekitem(i)
                lv[20 + i], lv[30 + i] = (p - mid) / mid * 1e4, a
        r[6:12] = [self.ofi, self.add_b, self.rem_b, self.add_a, self.rem_a, self.msgs]
        if trades:
            buys = [a for a, _ in trades if a > 0]
            sells = [-a for a, _ in trades if a < 0]
            prices = [p for _, p in trades]
            r[24:32] = [sum(buys), sum(sells), len(trades), sum(a for a in buys if a >= BIG), sum(a for a in sells if a >= BIG),
                        min(prices), max(prices), trades[-1][1]]
        else:
            r[24:29] = [0.0, 0.0, 0.0, 0.0, 0.0]
        r[32] = 1.0 if ok else 0.0
        self.reset_second()
        return r, lv


def session_starts(files):
    """first receipt time per (session file)"""
    out = {}
    for f in files:
        with gzip.open(f, "rt") as fh:
            for line in fh:
                out[f] = loads(line)["r"]
                break
    return out


def replay(root, out, days=None):
    files = sorted(glob.glob(os.path.join(root, "*", "*", "*", "*.jsonl.gz")))
    if days:
        files = [f for f in files if re.search(r"(\d{4})/(\d{2})/(\d{2})/", f.replace("\\", "/")) and
                 "-".join(re.search(r"(\d{4})/(\d{2})/(\d{2})/", f.replace("\\", "/")).groups()) in days]
    starts = session_starts(files)
    files.sort(key=lambda f: starts[f])
    sess_of = {f: int(re.search(r"-(\d+)\.jsonl\.gz$", f).group(1)) for f in files}
    sess_first = {}
    for f in files:
        s = sess_of[f]
        sess_first[s] = min(sess_first.get(s, starts[f]), starts[f])
    sess_order = sorted(sess_first, key=lambda s: sess_first[s])
    cutoff = {s: (sess_first[sess_order[i + 1]] if i + 1 < len(sess_order) else math.inf) for i, s in enumerate(sess_order)}
    os.makedirs(os.path.join(out, "bitfinex_1s"), exist_ok=True)
    os.makedirs(os.path.join(out, "bitfinex_lvl"), exist_ok=True)

    book = Book()
    chan = {}
    trades = []
    cur_sec = None
    last_msg_ms = None
    rows, lvls = {}, {}
    stats = defaultdict(int)
    cp_checked = cp_bad = 0
    t0 = time.time()

    def finalize(sec, ok):
        nonlocal trades
        r, lv = book.row(trades, ok)
        trades = []
        rows[sec] = r
        lvls[sec] = lv

    def flush_day(day):
        if not rows:
            return
        idx = np.array(sorted(rows))
        df = pd.DataFrame([rows[s] for s in idx], index=pd.Index(idx, name="sec"), columns=COLS)
        day_start = int(pd.Timestamp(day, tz="UTC").timestamp())
        df = df[(df.index >= day_start) & (df.index < day_start + 86400)]
        df.to_parquet(os.path.join(out, "bitfinex_1s", f"{day}.parquet"))
        arr = np.full((86400, 40), np.nan, dtype=np.float32)
        for s, lv in lvls.items():
            i = s - day_start
            if 0 <= i < 86400:
                arr[i] = lv
        np.save(os.path.join(out, "bitfinex_lvl", f"{day}.npy"), arr)
        print(f"  wrote {day}: {len(df):,} rows, ok {df['ok'].mean()*100:.2f}%, msgs/s {df['msgs'].mean():.1f}, "
              f"spread med {((df['ask1']-df['bid1'])/((df['ask1']+df['bid1'])/2)*1e4).median():.3f} bps "
              f"({time.time()-t0:.0f}s)", flush=True)

    cur_day = None
    for f in files:
        s = sess_of[f]
        cut = cutoff[s]
        with gzip.open(f, "rt") as fh:
            for line in fh:
                rec = loads(line)
                r = rec["r"]
                if r >= cut:
                    stats["dropped_overlap"] += 1
                    continue
                if "k" in rec:
                    k = rec["k"]
                    if k == "connect":
                        chan.clear()
                        stats["connects"] += 1
                    elif k == "book" and "cp" in rec:
                        cp = {int(o[0]): (float(o[1]), float(o[2])) for o in rec["cp"]["orders"]}
                        if not book.orders:                                   # started mid-session: bootstrap from the checkpoint
                            book.load_snapshot([(o, p, a) for o, (p, a) in cp.items()])
                            for cid, name in rec["cp"].get("channels", {}).items():
                                chan[int(cid)] = name
                            stats["bootstrapped_from_checkpoint"] += 1
                            continue
                        cp_checked += 1
                        if cp != book.orders:
                            cp_bad += 1
                            if cp_bad <= 3:
                                miss = len(set(cp) - set(book.orders))
                                extra = len(set(book.orders) - set(cp))
                                diff = sum(1 for o in cp if o in book.orders and cp[o] != book.orders[o])
                                print(f"  checkpoint mismatch at r={r}: missing {miss}, extra {extra}, differ {diff}, "
                                      f"cp size {len(cp)}, mine {len(book.orders)}", flush=True)
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
                    finalize(cur_sec, True)                                   # a message arrived in this second
                    for g in range(cur_sec + 1, sec):                         # silent seconds: carry the book forward
                        finalize(g, last_msg_ms is not None and (g * 1000 - last_msg_ms) < 20000)
                    cur_sec = sec
                    day = time.strftime("%Y-%m-%d", time.gmtime(sec))
                    if cur_day is None:
                        cur_day = day
                    elif day != cur_day:
                        flush_day(cur_day)
                        rows, lvls = {}, {}
                        cur_day = day
                last_msg_ms = ms
                tag = m[1]
                if tag == "hb":
                    stats["hb"] += 1
                    continue
                if ch == "book":
                    if isinstance(tag, list) and tag and isinstance(tag[0], list):
                        book.load_snapshot(tag)
                        stats["snapshots"] += 1
                    elif isinstance(tag, list):
                        book.update(int(tag[0]), float(tag[1]), float(tag[2]))
                        stats["updates"] += 1
                    elif tag == "cs":
                        stats["checksums"] += 1
                elif ch == "trades":
                    if tag == "tu":
                        trades.append((float(m[2][2]), float(m[2][3])))
                        stats["trades"] += 1
        print(f"{os.path.relpath(f, root)} done ({time.time()-t0:.0f}s)", flush=True)
    if cur_sec is not None:
        finalize(cur_sec, True)
        flush_day(cur_day)
    stats.update(checkpoints_checked=cp_checked, checkpoints_mismatched=cp_bad)
    print("stats:", dict(stats))
    with open(os.path.join(out, "replay_stats.json"), "w") as fh:
        json.dump(dict(stats), fh, indent=1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="raw")
    ap.add_argument("--out", default="data_bitfinex")
    ap.add_argument("--days", nargs="*", default=None)
    a = ap.parse_args()
    replay(a.root, a.out, a.days)
