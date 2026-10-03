#!/usr/bin/env python3
"""
explore.py — EXPLORATORY battery: ways to monetise "a big move is coming" without knowing its direction,
standing on the mean-reversion side instead of the continuation side.

Guards against fooling ourselves:
  * exploration folds 0-4 (first 5 OOS weeks) vs confirmation folds 5-8 (last 4 weeks), reported separately;
  * a placebo run per family where the volatility condition is shuffled within fold (same frequency, no information);
  * every cell is reported, nothing is dropped.

Families
  F1 passive range fade: at minute t, rest a SELL limit at close*(1+d) and a BUY limit at close*(1-d), armed 60 min.
     Whichever is traded through first (by THROUGH bps, a queue-position proxy) fills at the level (maker fee).
     Exit: resting limit at a partial retrace (maker) or stop (market) or cap (market).
  F2 vol-gated reversal: when |60-min return| is in its top decile (per fold) and the condition holds, fade it with a
     passive entry at the close (filled only if the next minute trades through the close), target r*sigma, stop s*sigma.
Costs: maker leg 2.0 bps (fee), market leg 7.5 bps (5.5 fee + 2 slippage). Spread is one tick here, ignored.
"""
import math
import time

import numpy as np
import pandas as pd

MAKER, TAKER = 2.0, 7.5
THROUGH = 0.5          # bps the price must trade through a resting limit for us to assume a fill
W_ARM = 60
RNG = np.random.default_rng(11)

bars = pd.read_parquet("out/bars_1m_v2.parquet")
oos = pd.read_parquet("out/oos_H60.parquet")[["ts", "fold", "vol_hgb", "sigma1"]].set_index("ts")
b = bars.join(oos, how="left")
b["fsig60"] = np.sqrt(np.exp(b["vol_hgb"])) * 1e4 * math.sqrt(60)
b["trail60"] = b["sigma1"] * math.sqrt(60)
b["ratio"] = b["fsig60"] / b["trail60"]
b["ret60"] = (np.log(b["close"]) - np.log(b["close"].shift(60))) * 1e4
b = b[(b["fsig60"].notna() & b["fold"].notna()).values].copy()
b["lvl_top"] = b.groupby("fold")["fsig60"].transform(lambda s: s >= s.quantile(2 / 3))
b["exp_top"] = b.groupby("fold")["ratio"].transform(lambda s: s >= s.quantile(2 / 3))
b["big_ret"] = b.groupby("fold")["ret60"].transform(lambda s: s.abs() >= s.abs().quantile(0.9))
b["us"] = np.isin(b.index.hour, list(range(13, 17)))

n = len(b)
ts = b.index
close, hi, lo = b["close"].to_numpy(), b["l2_mid_hi"].to_numpy(), b["l2_mid_lo"].to_numpy()
fsig, fold, ret60 = b["fsig60"].to_numpy(), b["fold"].to_numpy().astype(int), b["ret60"].to_numpy()
day = b.index.floor("D").to_numpy()
tmin = ((ts.tz_convert("UTC").tz_localize(None) - pd.Timestamp("1970-01-01")) // pd.Timedelta(minutes=1)).to_numpy().astype(np.int64)
thr = THROUGH / 1e4


def contiguous(i, j):
    return tmin[j] - tmin[i] == j - i


def manage(side, entry, j, target, stop, cap, d_bps):
    """From entry at minute j (already filled): resting-limit target (needs trade-through), market stop, cap.
    Conservative: stop checked before target each minute; in the entry minute only the stop can trigger."""
    if (side > 0 and lo[j] <= stop) or (side < 0 and hi[j] >= stop):
        return stop, j, "stop"
    last = j
    for m in range(j + 1, min(j + cap, n - 1) + 1):
        if not contiguous(j, m):
            break
        last = m
        if side > 0:
            if lo[m] <= stop:
                return stop, m, "stop"
            if hi[m] >= target * (1 + thr):
                return target, m, "target"
        else:
            if hi[m] >= stop:
                return stop, m, "stop"
            if lo[m] <= target * (1 - thr):
                return target, m, "target"
    return close[last], last, "cap"


def finish(trades, side, entry, exit_px, reason, i, j, exit_j, d):
    gross = side * math.log(exit_px / entry) * 1e4
    cost = MAKER + (MAKER if reason == "target" else TAKER)
    trades.append({"arm_ts": ts[i], "entry_ts": ts[j], "exit_ts": ts[exit_j], "fold": fold[i], "hour": ts[j].hour,
                   "side": side, "d_bps": d, "wait_min": j - i, "hold_min": exit_j - j, "reason": reason,
                   "gross_bps": gross, "net_bps": gross - cost, "day": day[j]})


def fade_range(cond, k, r, s, cap):
    trades, n_arm, i = [], 0, 0
    while i < n:
        if not cond[i] or not np.isfinite(fsig[i]):
            i += 1
            continue
        d = k * fsig[i]
        up, dn = close[i] * math.exp(d / 1e4), close[i] * math.exp(-d / 1e4)
        n_arm += 1
        hit = None
        j_end = min(i + W_ARM, n - 1)
        for j in range(i + 1, j_end + 1):
            if not contiguous(i, j):
                break
            hu, hd = hi[j] >= up * (1 + thr), lo[j] <= dn * (1 - thr)
            if hu or hd:
                if hu and hd:
                    o = close[j - 1]
                    side = -1 if (up - o) <= (o - dn) else 1   # the nearer level fills first
                else:
                    side = -1 if hu else 1                      # sold into the up-move / bought into the down-move
                hit = (j, side)
                break
        if hit is None:
            i = j_end + 1
            continue
        j, side = hit
        entry = up if side < 0 else dn
        target = entry * math.exp(side * r * d / 1e4)           # partial retrace toward the arming price (long: above, short: below)
        stop = entry * math.exp(-side * s * d / 1e4)            # further continuation against us
        exit_px, exit_j, reason = manage(side, entry, j, target, stop, cap, d)
        finish(trades, side, entry, exit_px, reason, i, j, exit_j, d)
        i = exit_j + 1
    return pd.DataFrame(trades), n_arm


def fade_reversal(cond, r, s, cap):
    trades, n_arm, i = [], 0, 0
    while i < n - 1:
        if not (cond[i] and b["big_ret"].values[i]) or not np.isfinite(fsig[i]) or not np.isfinite(ret60[i]):
            i += 1
            continue
        n_arm += 1
        side = -1 if ret60[i] > 0 else 1
        j = i + 1
        if not contiguous(i, j):
            i += 1
            continue
        filled = (lo[j] <= close[i] * (1 - thr)) if side > 0 else (hi[j] >= close[i] * (1 + thr))
        if not filled:
            i += 1
            continue
        entry = close[i]
        d = fsig[i]
        target = entry * math.exp(side * r * d / 1e4)
        stop = entry * math.exp(-side * s * d / 1e4)
        exit_px, exit_j, reason = manage(side, entry, j, target, stop, cap, d)
        finish(trades, side, entry, exit_px, reason, i, j, exit_j, d)
        i = exit_j + 1
    return pd.DataFrame(trades), n_arm


def stats(T, folds_sel):
    T = T[T["fold"].isin(folds_sel)]
    if len(T) < 10:
        return {"n": len(T)}
    dm = T.groupby("day")["net_bps"].mean()
    fm = T.groupby("fold")["net_bps"].mean()
    t = dm.mean() / (dm.std(ddof=1) / math.sqrt(len(dm))) if len(dm) > 2 and dm.std(ddof=1) > 0 else np.nan
    return {"n": len(T), "p_target": (T["reason"] == "target").mean(), "p_stop": (T["reason"] == "stop").mean(),
            "gross": T["gross_bps"].mean(), "net": T["net_bps"].mean(), "folds_pos": f"{int((fm > 0).sum())}/{len(fm)}", "t": t}


def shuffled(cond):
    out = cond.copy()
    for f in np.unique(fold):
        m = fold == f
        out[m] = RNG.permutation(cond[m])
    return out


conds = {"expansion_top3": b["exp_top"].to_numpy(), "level_top3": b["lvl_top"].to_numpy(), "unconditional": np.ones(n, dtype=bool)}
EXPL, CONF = [0, 1, 2, 3, 4], [5, 6, 7, 8]
rows, best_trades = [], {}
t0 = time.time()


def record(family, label, T, n_arm):
    e, c, a = stats(T, EXPL), stats(T, CONF), stats(T, EXPL + CONF)
    row = {"family": family, **label, "n_arm": n_arm, "n_all": a.get("n", 0), "p_target_all": a.get("p_target"),
           "p_stop_all": a.get("p_stop"), "gross_all": a.get("gross"), "net_all": a.get("net"), "folds_pos_all": a.get("folds_pos"),
           "t_all": a.get("t"), "n_expl": e.get("n", 0), "net_expl": e.get("net"), "t_expl": e.get("t"),
           "n_conf": c.get("n", 0), "net_conf": c.get("net"), "t_conf": c.get("t"), "folds_pos_conf": c.get("folds_pos")}
    rows.append(row)
    best_trades[(family, tuple(label.values()))] = T
    return row


for cname, cond in conds.items():
    for k in (0.75, 1.0, 1.5):
        for r in (0.5, 1.0):
            for s in (2.0, 3.0):
                T, n_arm = fade_range(cond, k, r, s, 240)
                record("F1_range_fade", {"condition": cname, "k": k, "target_r": r, "stop_s": s, "session": "all"}, T, n_arm)
    for k in (1.0,):
        for r in (0.5, 1.0):
            for s in (2.0,):
                for sname, smask in (("US_13-17", b["us"].to_numpy()), ("other", ~b["us"].to_numpy())):
                    T, n_arm = fade_range(cond & smask, k, r, s, 240)
                    record("F1_range_fade", {"condition": cname, "k": k, "target_r": r, "stop_s": s, "session": sname}, T, n_arm)
    for r in (0.5, 1.0):
        for s in (1.5, 2.0):
            for cap in (60, 120):
                T, n_arm = fade_reversal(cond, r, s, cap)
                record("F2_reversal", {"condition": cname, "k": np.nan, "target_r": r, "stop_s": s, "session": f"cap{cap}"}, T, n_arm)
    print(f"{cname} done ({time.time()-t0:.0f}s)")

# placebo: shuffle the expansion condition within fold, re-run the F1 grid's main cells and the F2 cells
pl = shuffled(b["exp_top"].to_numpy())
for k in (0.75, 1.0, 1.5):
    for r in (0.5, 1.0):
        for s in (2.0, 3.0):
            T, n_arm = fade_range(pl, k, r, s, 240)
            record("F1_range_fade", {"condition": "PLACEBO_shuffled_expansion", "k": k, "target_r": r, "stop_s": s, "session": "all"}, T, n_arm)
for r in (0.5, 1.0):
    for s in (1.5, 2.0):
        for cap in (60, 120):
            T, n_arm = fade_reversal(pl, r, s, cap)
            record("F2_reversal", {"condition": "PLACEBO_shuffled_expansion", "k": np.nan, "target_r": r, "stop_s": s, "session": f"cap{cap}"}, T, n_arm)

R = pd.DataFrame(rows)
R.to_csv("out/explore_cells.csv", index=False)
print(f"\n{len(R)} cells in {time.time()-t0:.0f}s -> out/explore_cells.csv")
cols = ["family", "condition", "k", "target_r", "stop_s", "session", "n_all", "p_target_all", "gross_all", "net_all",
        "folds_pos_all", "t_all", "net_expl", "t_expl", "net_conf", "t_conf", "folds_pos_conf"]
pd.set_option("display.width", 250)
print("\n=== ranked by exploration t-stat (folds 0-4); confirmation = folds 5-8 ===")
print(R[R.condition != "PLACEBO_shuffled_expansion"].sort_values("t_expl", ascending=False).head(12)[cols].round(3).to_string(index=False))
print("\n=== placebo cells (expansion condition shuffled within fold) ===")
print(R[R.condition == "PLACEBO_shuffled_expansion"][cols].round(3).to_string(index=False))
print("\n=== all cells, pooled, sorted by net ===")
print(R.sort_values("net_all", ascending=False)[cols].round(3).to_string(index=False))
# save trades of the top exploration cell for inspection
top = R[R.condition != "PLACEBO_shuffled_expansion"].sort_values("t_expl", ascending=False).iloc[0]
key = (top["family"], (top["condition"], top["k"], top["target_r"], top["stop_s"], top["session"]))
for kk, T in best_trades.items():
    if kk[0] == key[0] and all((str(a) == str(b_)) for a, b_ in zip(kk[1], key[1])):
        T.to_csv("out/explore_top_trades.csv", index=False)
        break
