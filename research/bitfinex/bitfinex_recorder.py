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
