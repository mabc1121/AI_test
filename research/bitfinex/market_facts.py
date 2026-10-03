"""market_facts.py — venue comparison from 1-second files: Bitfinex tBTCUSD (replayed) vs Bybit BTCUSDT (sample days)."""
import glob
import sys

import numpy as np
import pandas as pd


def facts(path, label):
    d = pd.read_parquet(path)
    if "sec" in d.columns:
        d = d.set_index("sec")
    mid = (d["bid1"] + d["ask1"]) / 2
    sp = (d["ask1"] - d["bid1"]) / mid * 1e4
    r1 = np.log(mid).diff() * 1e4
    m1 = mid.groupby(d.index // 60).last()
    r1m = np.log(m1).diff() * 1e4
    ok = d["ok"].mean() if "ok" in d else 1.0
    return {"venue": label, "day": path.split("/")[-1][:10], "ok_pct": ok * 100,
            "spread_med_bps": sp.median(), "spread_p90_bps": sp.quantile(0.9),
            "touch_size_med_btc": pd.concat([d["bsz1"], d["asz1"]]).median(),
            "depth_1bps_med_btc": (d["bd1"] + d["ad1"]).median(), "depth_3bps_med_btc": (d["bd3"] + d["ad3"]).median(),
            "trades_per_day": d["n_trades"].sum(), "volume_btc_per_day": (d["buy_vol"] + d["sell_vol"]).sum(),
            "sec_with_trade_pct": (d["n_trades"] > 0).mean() * 100, "msgs_per_sec": d["msgs"].mean(),
            "sigma_1min_bps": r1m.std(), "sec_mid_unchanged_pct": (r1 == 0).mean() * 100}


rows = [facts(f, "bitfinex_spot") for f in sorted(glob.glob("data_bitfinex/bitfinex_1s/*.parquet"))]
rows += [facts(f, "bybit_perp") for f in sorted(glob.glob("../real/raw_sample/2026-*.parquet"))]
R = pd.DataFrame(rows)
pd.set_option("display.width", 250)
print(R.round(3).to_string(index=False))
print()
print(R.groupby("venue").median(numeric_only=True).round(3).T.to_string())
R.to_csv("market_facts.csv", index=False)
