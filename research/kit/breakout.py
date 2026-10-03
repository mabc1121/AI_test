#!/usr/bin/env python3
"""
breakout.py — pre-registered volatility-breakout test.

PRIMARY CELL (fixed before running):
  condition  = forecast 60-min sigma / trailing 60-min sigma in the TOP TERCILE (per fold)   ["expansion"]
  trigger    = stop orders at close * exp(+-d), d = 1.0 * forecast sigma_60, armed for 60 minutes
  exit       = target at +2d, stop at -d from the entry level, 240-minute cap
  cost       = 15 bps per round trip (taker both legs + slippage)
  position   = one at a time; no re-arming while armed or in a position
  pass       = mean net > 0  AND  net > 0 in a majority of folds  AND  t(daily mean net per trade, trade days) >= 2
Everything else (other conditions, d = 0.5, 1:1 and trailing exits, session splits) is secondary diagnostics.

Inputs: out/bars_1m_v2.parquet (1-minute bars, intra-second extremes l2_mid_hi/l2_mid_lo) and out/oos_H60.parquet
(out-of-sample vol forecasts 'vol_hgb' = log mean per-minute variance over the next 60 minutes, per fold).
"""
import math
import sys
import time

import numpy as np
import pandas as pd

COST_BPS = 15.0
W_ARM = 60          # minutes a straddle stays armed
CAP = 240           # max holding minutes after entry
US_HOURS = range(13, 17)

bars = pd.read_parquet("out/bars_1m_v2.parquet")
oos = pd.read_parquet("out/oos_H60.parquet")[["ts", "fold", "vol_hgb", "sigma1"]].set_index("ts")
b = bars.join(oos, how="left")
b["fsig60"] = np.sqrt(np.exp(b["vol_hgb"])) * 1e4 * math.sqrt(60)      # forecast sigma over next 60 min, bps
b["trail60"] = b["sigma1"] * math.sqrt(60)                               # trailing sigma over 60 min, bps
b["ratio"] = b["fsig60"] / b["trail60"]
valid = b["fsig60"].notna() & b["fold"].notna()
b = b[valid.values].copy()
# per-fold terciles (same construction as the vol gate in the kit)
b["lvl_top"] = b.groupby("fold")["fsig60"].transform(lambda s: s >= s.quantile(2 / 3))
b["exp_top"] = b.groupby("fold")["ratio"].transform(lambda s: s >= s.quantile(2 / 3))
b["us"] = np.isin(b.index.hour, list(US_HOURS))

n = len(b)
ts = b.index
close = b["close"].to_numpy()
hi = (b["l2_mid_hi"] if "l2_mid_hi" in b else b["high"]).to_numpy()
lo = (b["l2_mid_lo"] if "l2_mid_lo" in b else b["low"]).to_numpy()
fsig = b["fsig60"].to_numpy()
fold = b["fold"].to_numpy().astype(int)
hour = b.index.hour.to_numpy()
day = b.index.floor("D").to_numpy()
# minute index continuity: the OOS frame has gaps between folds / dropped rows; a straddle or position never crosses a gap
tsec = (ts.tz_convert("UTC").tz_localize(None) - pd.Timestamp("1970-01-01")) // pd.Timedelta(minutes=1)
tmin = tsec.to_numpy().astype(np.int64)


def contiguous(i, j):
    return tmin[j] - tmin[i] == j - i


def simulate(cond, k, exit_mode):
    trades = []
    n_arm = n_trig = 0
    i = 0
    while i < n:
        if not cond[i] or not np.isfinite(fsig[i]):
            i += 1
            continue
        d = k * fsig[i]
        up, dn = close[i] * math.exp(d / 1e4), close[i] * math.exp(-d / 1e4)
        n_arm += 1
        trig = None
        j_end = min(i + W_ARM, n - 1)
        for j in range(i + 1, j_end + 1):
            if not contiguous(i, j):
                break
            hu, hd = hi[j] >= up, lo[j] <= dn
            if hu or hd:
                if hu and hd:
                    o = close[j - 1]
                    side = 1 if (up - o) <= (o - dn) else -1
                else:
                    side = 1 if hu else -1
                trig = (j, side)
                break
        if trig is None:
            i = j_end + 1
            continue
        n_trig += 1
        j, side = trig
        entry = up if side > 0 else dn
        tgt_mult = 1.0 if exit_mode == "1to1" else 2.0
        if side > 0:
            target, stop = entry * math.exp(tgt_mult * d / 1e4), entry * math.exp(-d / 1e4)
        else:
            target, stop = entry * math.exp(-tgt_mult * d / 1e4), entry * math.exp(d / 1e4)
        exit_px, exit_j, reason = None, None, None
        # remainder of the trigger minute: a stop touched in the same minute counts as stopped (conservative)
        if (side > 0 and lo[j] <= stop) or (side < 0 and hi[j] >= stop):
            exit_px, exit_j, reason = stop, j, "stop"
        else:
            last = j
            for m in range(j + 1, min(j + CAP, n - 1) + 1):
                if not contiguous(j, m):
                    break
                last = m
                if side > 0:
                    if lo[m] <= stop:
                        exit_px, exit_j, reason = stop, m, "stop"
                        break
                    if exit_mode != "trail" and hi[m] >= target:
                        exit_px, exit_j, reason = target, m, "target"
                        break
                    if exit_mode == "trail":
                        stop = max(stop, hi[m] * math.exp(-d / 1e4))
                else:
                    if hi[m] >= stop:
                        exit_px, exit_j, reason = stop, m, "stop"
                        break
                    if exit_mode != "trail" and lo[m] <= target:
                        exit_px, exit_j, reason = target, m, "target"
                        break
                    if exit_mode == "trail":
                        stop = min(stop, lo[m] * math.exp(d / 1e4))
            if exit_px is None:
                exit_px, exit_j, reason = close[last], last, "cap"
        gross = side * math.log(exit_px / entry) * 1e4
        trades.append({"arm_ts": ts[i], "trig_ts": ts[j], "exit_ts": ts[exit_j], "fold": fold[i], "hour": hour[j],
                       "side": side, "d_bps": d, "wait_min": j - i, "hold_min": exit_j - j, "reason": reason,
                       "gross_bps": gross, "net_bps": gross - COST_BPS, "day": day[j]})
        i = exit_j + 1
    T = pd.DataFrame(trades)
    return T, n_arm, n_trig


def summarize(T, n_arm, n_trig, label):
    if T.empty:
        return {**label, "n_arm": n_arm, "n_trades": 0}
    daily_mean = T.groupby("day")["net_bps"].mean()
    all_days = pd.Series(0.0, index=pd.DatetimeIndex(np.unique(day)))
    daily_sum = T.groupby("day")["net_bps"].sum()
    all_days.loc[daily_sum.index] = daily_sum.values
    fm = T.groupby("fold")["net_bps"].mean()
    t_trade_days = daily_mean.mean() / (daily_mean.std(ddof=1) / math.sqrt(len(daily_mean))) if len(daily_mean) > 2 and daily_mean.std(ddof=1) > 0 else np.nan
    t_all_days = all_days.mean() / (all_days.std(ddof=1) / math.sqrt(len(all_days))) if all_days.std(ddof=1) > 0 else np.nan
    eq = all_days.cumsum()
    return {**label, "n_arm": n_arm, "trigger_rate": n_trig / max(1, n_arm), "n_trades": len(T),
            "d_mean_bps": T["d_bps"].mean(), "wait_mean_min": T["wait_min"].mean(), "hold_mean_min": T["hold_min"].mean(),
            "p_target": (T["reason"] == "target").mean(), "p_stop": (T["reason"] == "stop").mean(),
            "p_cap": (T["reason"] == "cap").mean(), "long_share": (T["side"] > 0).mean(),
            "gross_bps": T["gross_bps"].mean(), "net_bps": T["net_bps"].mean(), "total_net_bps": T["net_bps"].sum(),
            "folds_pos": f"{int((fm > 0).sum())}/{len(fm)}", "t_trade_days": t_trade_days, "t_all_days": t_all_days,
            "max_dd_bps": float((eq - eq.cummax()).min()),
            "pass": bool(T["net_bps"].mean() > 0 and (fm > 0).sum() > len(fm) / 2 and np.isfinite(t_trade_days) and t_trade_days >= 2)}


conds = {"expansion_top3": b["exp_top"].to_numpy(), "level_top3": b["lvl_top"].to_numpy(),
         "unconditional": np.ones(n, dtype=bool)}
sessions = {"all": np.ones(n, dtype=bool), "US_13-17utc": b["us"].to_numpy(), "other_hours": ~b["us"].to_numpy()}
rows = []
t0 = time.time()
primary = ("expansion_top3", 1.0, "2to1", "all")
order = [primary] + [(c, k, e, s) for c in conds for k in (1.0, 0.5) for e in ("2to1", "1to1", "trail") for s in sessions
                     if (c, k, e, s) != primary]
for c, k, e, s in order:
    T, n_arm, n_trig = simulate(conds[c] & sessions[s], k, e)
    label = {"condition": c, "k": k, "exit": e, "session": s, "cell": "PRIMARY" if (c, k, e, s) == primary else "secondary"}
    rows.append(summarize(T, n_arm, n_trig, label))
    if (c, k, e, s) == primary:
        T.to_csv("out/breakout_trades_primary.csv", index=False)
        P = rows[-1]
        print("PRIMARY CELL:", {k_: (round(v, 4) if isinstance(v, float) else v) for k_, v in P.items()})
        print("VERDICT:", "PASS" if P.get("pass") else "FAIL", "| benchmark continuation under a random walk for 2d/d = 0.333")
        if not T.empty:
            print("by fold:\n", T.groupby("fold").agg(n=("net_bps", "size"), p_target=("reason", lambda x: (x == "target").mean()),
                                                      gross=("gross_bps", "mean"), net=("net_bps", "mean")).round(2).to_string())
            print("by hour band:\n", T.assign(band=pd.cut(T["hour"], [-1, 7, 12, 16, 23], labels=["00-07", "08-12", "13-16", "17-23"]))
                  .groupby("band", observed=True).agg(n=("net_bps", "size"), p_target=("reason", lambda x: (x == "target").mean()),
                                                      net=("net_bps", "mean")).round(2).to_string())
R = pd.DataFrame(rows)
R.to_csv("out/breakout_cells.csv", index=False)
print(f"\n{len(R)} cells in {time.time()-t0:.0f}s -> out/breakout_cells.csv")
cols = ["cell", "condition", "k", "exit", "session", "n_trades", "trigger_rate", "p_target", "p_stop", "gross_bps", "net_bps",
        "folds_pos", "t_trade_days", "t_all_days", "pass"]
print(R[cols].round(3).to_string(index=False))
