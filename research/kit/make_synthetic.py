#!/usr/bin/env python3
"""
make_synthetic.py — fake 1-second data for smoke tests only.

  python make_synthetic.py --days 12 --out data_synth --signal 0.02                 # v1 column-based files
  python make_synthetic.py --days 12 --out data_synth --signal 0.02 --bybit-format  # v2 native layout:
        data_synth/bybit_1s/<date>.parquet   index 'sec', the 32 original Bybit-processed column names
        data_synth/bybit_lvl/<date>.npy      float32 (86400, 40) top-10 levels

Nothing here resembles real market dynamics beyond vol clustering and a persistent latent flow.
"""
import argparse
import os

import numpy as np
import pandas as pd


def ar1(n, phi, rng, x0):
    e = rng.standard_normal(n) * np.sqrt(1 - phi ** 2)
    x = np.empty(n)
    prev = x0
    for i in range(n):
        prev = phi * prev + e[i]
        x[i] = prev
    return x


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=6)
    ap.add_argument("--out", default="data_synth")
    ap.add_argument("--start", default="2026-07-01")
    ap.add_argument("--signal", type=float, default=0.02, help="strength of the predictable flow component")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-l10", action="store_true", help="omit the top-10-level columns (v1 layout only)")
    ap.add_argument("--bybit-format", action="store_true", help="write the native Bybit-processed layout (v2)")
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    S = 86400
    tick = 0.1
    mid_last = 100_000.0
    flow_last, vol_last = 0.0, 0.0
    if a.bybit_format:
        os.makedirs(os.path.join(a.out, "bybit_1s"), exist_ok=True)
        os.makedirs(os.path.join(a.out, "bybit_lvl"), exist_ok=True)
    else:
        os.makedirs(a.out, exist_ok=True)
    for d in range(a.days):
        t0 = pd.Timestamp(a.start, tz="UTC") + pd.Timedelta(days=d)
        sec = int(t0.timestamp()) + np.arange(S)
        flow = ar1(S, 0.995, rng, flow_last)
        vst = ar1(S, 0.9995, rng, vol_last)
        flow_last, vol_last = flow[-1], vst[-1]
        hour = np.arange(S) / 3600
        intraday = 1 + 0.5 * np.cos(2 * np.pi * (hour - 15) / 24)
        sigma = 8e-5 * np.exp(0.5 * vst) * intraday
        r = sigma * (rng.standard_normal(S) + a.signal * flow)
        mid = np.exp(np.log(mid_last) + np.cumsum(r))
        mid_last = mid[-1]
        half = tick * (0.5 + rng.poisson(0.3, S))
        bid = np.floor((mid - half) / tick) * tick
        ask = np.ceil((mid + half) / tick) * tick
        tanh = np.tanh(flow)
        bid_sz = np.exp(rng.normal(0.5, 0.8, S)) * (1 + 0.3 * tanh)
        ask_sz = np.exp(rng.normal(0.5, 0.8, S)) * (1 - 0.3 * tanh)
        base = np.exp(rng.normal(3.0, 0.5, S))
        n_trades = rng.poisson(3 * intraday, S)
        vol = n_trades * np.exp(rng.normal(-2, 1, S))
        buy_share = 1 / (1 + np.exp(-(0.8 * flow + rng.normal(0, 0.5, S))))
        buy_vol = vol * buy_share
        sell_vol = vol - buy_vol
        last = np.round((mid + rng.normal(0, 0.5 * tick, S)) / tick) * tick
        high = np.maximum(last, mid) + np.abs(rng.normal(0, tick, S))
        low = np.minimum(last, mid) - np.abs(rng.normal(0, tick, S))
        notrade = n_trades == 0
        last[notrade], high[notrade], low[notrade] = np.nan, np.nan, np.nan
        add_b, add_a = np.exp(rng.normal(0, 1, S)) * (1 + 0.2 * tanh), np.exp(rng.normal(0, 1, S)) * (1 - 0.2 * tanh)
        rem_b, rem_a = np.exp(rng.normal(0, 1, S)) * (1 - 0.1 * tanh), np.exp(rng.normal(0, 1, S)) * (1 + 0.1 * tanh)
        big = rng.poisson(0.05 * (1 + np.abs(flow)), S) * np.exp(rng.normal(0.5, 0.5, S))
        big_buy = big * (rng.random(S) < buy_share)
        big_sell = big - big_buy
        depth = {k: (base * m * (1 + s * tanh), base * m * (1 - s * tanh))
                 for k, m, s in (("0.5", 0.3, 0.2), ("1", 0.6, 0.15), ("2", 1.2, 0.1), ("3", 1.8, 0.05))}
        lvl = np.empty((S, 40), dtype=np.float32)
        for i in range(10):
            lvl[:, i] = -(i * 0.04 + 0.01 + rng.uniform(0, 0.01, S))            # bid_dist_i, bps, negative
            lvl[:, 10 + i] = np.exp(rng.normal(-1, 1, S)) * (1 + 0.15 * tanh)   # bid_size_i
            lvl[:, 20 + i] = i * 0.04 + 0.01 + rng.uniform(0, 0.01, S)          # ask_dist_i, bps, positive
            lvl[:, 30 + i] = np.exp(rng.normal(-1, 1, S)) * (1 - 0.15 * tanh)   # ask_size_i
        lvl[:, 10], lvl[:, 30] = bid_sz, ask_sz
        if a.bybit_format:
            df = pd.DataFrame({
                "bid1": bid, "ask1": ask, "bsz1": bid_sz.astype(np.float32), "asz1": ask_sz.astype(np.float32),
                "mid_hi": mid + np.abs(rng.normal(0, 0.3 * tick, S)), "mid_lo": mid - np.abs(rng.normal(0, 0.3 * tick, S)),
                "ofi": (2 * flow + rng.normal(0, 2, S)).astype(np.float32),
                "add_b": add_b.astype(np.float32), "rem_b": rem_b.astype(np.float32),
                "add_a": add_a.astype(np.float32), "rem_a": rem_a.astype(np.float32),
                "msgs": np.full(S, 10, dtype=np.float32),
                "bd0.5": depth["0.5"][0].astype(np.float32), "bd1": depth["1"][0].astype(np.float32),
                "bd2": depth["2"][0].astype(np.float32), "bd3": depth["3"][0].astype(np.float32),
                "ad0.5": depth["0.5"][1].astype(np.float32), "ad1": depth["1"][1].astype(np.float32),
                "ad2": depth["2"][1].astype(np.float32), "ad3": depth["3"][1].astype(np.float32),
                "bdtot": (base * 4 * (1 + 0.05 * tanh)).astype(np.float32),
                "adtot": (base * 4 * (1 - 0.05 * tanh)).astype(np.float32),
                "breach": (4.2 + rng.normal(0, 0.3, S)).astype(np.float32),
                "areach": (4.2 + rng.normal(0, 0.3, S)).astype(np.float32),
                "buy_vol": buy_vol.astype(np.float32), "sell_vol": sell_vol.astype(np.float32),
                "n_trades": n_trades.astype(np.float32),
                "big_buy": big_buy.astype(np.float32), "big_sell": big_sell.astype(np.float32),
                "px_min": low, "px_max": high, "px_last": last,
            }, index=pd.Index(sec, name="sec"))
            p = os.path.join(a.out, "bybit_1s", f"{t0:%Y-%m-%d}.parquet")
            df.to_parquet(p)
            np.save(os.path.join(a.out, "bybit_lvl", f"{t0:%Y-%m-%d}.npy"), lvl)
        else:
            df = pd.DataFrame({
                "timestamp": sec * 1000, "bid_price": bid, "ask_price": ask, "bid_size": bid_sz, "ask_size": ask_sz,
                "spread": ask - bid,
                "bid_depth_0.5bps": depth["0.5"][0], "ask_depth_0.5bps": depth["0.5"][1],
                "bid_depth_1bps": depth["1"][0], "ask_depth_1bps": depth["1"][1],
                "bid_depth_2bps": depth["2"][0], "ask_depth_2bps": depth["2"][1],
                "bid_depth_3bps": depth["3"][0], "ask_depth_3bps": depth["3"][1],
                "ofi": 2 * flow + rng.normal(0, 2, S),
                "size_added_2bps": add_b + add_a, "size_removed_2bps": rem_b + rem_a,
                "buy_volume": buy_vol, "sell_volume": sell_vol, "trade_count": n_trades,
                "large_trades": big_buy + big_sell,
                "high": high, "low": low, "last": last,
            })
            if not a.no_l10:
                for i in range(1, 11):
                    df[f"bid_dist_{i}"] = lvl[:, i - 1]
                    df[f"bid_size_{i}"] = lvl[:, 9 + i]
                    df[f"ask_dist_{i}"] = lvl[:, 19 + i]
                    df[f"ask_size_{i}"] = lvl[:, 29 + i]
            p = os.path.join(a.out, f"btcusdt_{t0:%Y-%m-%d}.parquet")
            df.to_parquet(p, index=False)
        print(f"wrote {p}  rows={S:,}  mid {mid[0]:.0f}->{mid[-1]:.0f}")


if __name__ == "__main__":
    main()
