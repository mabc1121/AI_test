#!/usr/bin/env python3
"""
cost_sensitivity.py — re-evaluate the existing out-of-sample strategies under zero trading fees and a grid of
spread + slippage assumptions. NO re-fitting: the model predictions, folds and selections are read from the saved
oos_H*.parquet files; the breakout and fade trades are re-simulated with the identical pre-registered rules at cost 0.

Definitions
  gross_bps    mean P&L per trade at mid prices (no fees, no spread, no slippage)
  breakeven    round-trip spread+slippage at which mean net = 0  (= gross)
  cost_at_t2   round-trip cost at which the daily t-statistic falls to 2.0 (statistical support lost)
  scenarios    round-trip spread+slippage: tight 1.5 bps, moderate 4 bps, wide 9 bps (taker both legs)
  t            t-stat of the daily mean of per-trade net P&L over days with trades (same definition as the kits)
Fixed-hold rows use side * forward mid return (enter at close, exit at close H minutes later).
Barrier rows use the kit's barrier exits (take-profit / stop-loss / time), still mid prices.
"""
import math
import numpy as np
import pandas as pd

SCEN = {"tight_1.5": 1.5, "moderate_4": 4.0, "wide_9": 9.0}
TOP = [0.05, 0.10, 0.20]
rows = []


def tstat(x):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    return x.mean() / (x.std(ddof=1) / math.sqrt(len(x))) if len(x) > 2 and x.std(ddof=1) > 0 else np.nan


ALL_DAYS = np.sort(pd.read_parquet("out/oos_H60.parquet")["day"].unique())   # the 63 out-of-sample days


def summarize(label, gross, day, fold, cost_legs=1.0):
    """cost_legs: fraction of the round-trip spread+slippage this strategy actually pays (1 = taker both legs).
    t-statistics use DAILY P&L SUMS over all out-of-sample days (zero on days without trades), i.e. the equity curve."""
    gross = np.asarray(gross, dtype=float)
    ok = np.isfinite(gross)
    gross, day, fold = gross[ok], np.asarray(day)[ok], np.asarray(fold)[ok]
    if len(gross) < 20:
        return
    daily = pd.Series(gross).groupby(day).sum().reindex(ALL_DAYS).fillna(0.0).to_numpy()
    cnt = pd.Series(np.ones(len(gross))).groupby(day).sum().reindex(ALL_DAYS).fillna(0.0).to_numpy()
    fsum = pd.Series(gross).groupby(fold).sum()
    fcnt = pd.Series(np.ones(len(gross))).groupby(fold).sum()

    def t_at(c):
        return tstat(daily - c * cost_legs * cnt)

    m = float(gross.mean())
    grid = np.arange(0, 30.001, 0.05)
    tv = np.array([t_at(c) for c in grid])
    c_t2 = float(grid[tv >= 2].max()) if np.any(tv >= 2) else np.nan
    r = {**label, "n_trades": int(len(gross)), "trades_per_day": len(gross) / len(ALL_DAYS), "gross_bps": m,
         "t_gross": t_at(0.0), "folds_pos_gross": f"{int((fsum > 0).sum())}/{len(fsum)}",
         "breakeven_cost_bps": m / cost_legs if cost_legs > 0 else np.nan, "cost_at_t2_bps": c_t2}
    for name, c in SCEN.items():
        net = gross - c * cost_legs
        fmn = fsum - c * cost_legs * fcnt
        r[f"net_{name}"] = float(net.mean())
        r[f"t_{name}"] = t_at(c)
        r[f"folds_pos_{name}"] = f"{int((fmn > 0).sum())}/{len(fmn)}"
    rows.append(r)


def seq_fixed(ts, sel, side, fwd, H):
    """one position at a time, fixed hold H minutes: returns indices of entries taken"""
    tmin = ((ts.tz_convert("UTC").tz_localize(None) - pd.Timestamp("1970-01-01")) // pd.Timedelta(minutes=1)).to_numpy()
    cand = np.where(sel & np.isfinite(fwd))[0]
    taken, pos, block_until = [], 0, -1
    for i in cand:
        if tmin[i] < block_until:
            continue
        taken.append(i)
        block_until = tmin[i] + H + 1
    return np.array(taken, dtype=int)


# ---------------------------------------------------------------- 1) model strategies from the saved OOS predictions
for H in (5, 15, 60, 240):
    O = pd.read_parquet(f"out/oos_H{H}.parquet")
    ts = pd.DatetimeIndex(O["ts"])
    fwd, day, fold = O["fwd"].to_numpy(), O["day"].to_numpy(), O["fold"].to_numpy()
    for key in ("PL2_gb", "PL2_lr", "P_gb", "P_lr", "L2_gb", "L2_lr", "ALL_gb"):
        if f"score_{key}" not in O:
            continue
        score = O[f"score_{key}"].to_numpy()
        side = np.sign(score)
        for q in TOP:
            tau = np.nanquantile(np.abs(score), 1 - q)
            sel = np.isfinite(score) & (score != 0) & (np.abs(score) >= tau)
            summarize({"strategy": f"model H={H}", "variant": f"{key} top{int(q*100)}% fixed-hold (all signals)"},
                      (side * fwd)[sel], day[sel], fold[sel])
            gb = np.where(side > 0, O["g_long"].to_numpy(), O["g_short"].to_numpy())
            summarize({"strategy": f"model H={H}", "variant": f"{key} top{int(q*100)}% barrier exits (all signals)"},
                      gb[sel], day[sel], fold[sel])
            idx = seq_fixed(ts, sel, side, fwd, H)
            summarize({"strategy": f"model H={H}", "variant": f"{key} top{int(q*100)}% fixed-hold SEQUENTIAL"},
                      (side * fwd)[idx], day[idx], fold[idx])
    print(f"model H={H} done")

# ---------------------------------------------------------------- 2) raw 1- and 5-minute book-imbalance rules (no fitting)
b = pd.read_parquet("out/bars_1m_v2.parquet")
oos60 = pd.read_parquet("out/oos_H60.parquet")[["ts", "fold"]].set_index("ts")
b = b.join(oos60, how="inner")                      # same out-of-sample days as the models
lc = np.log(b["close"].to_numpy())
dayb = b.index.floor("D").astype(str).to_numpy()
foldb = b["fold"].to_numpy()
for sig in ("dimb05_end", "bimb_end", "l2_wimb10_end"):
    x = b[sig].to_numpy()
    for H in (1, 5):
        fwdH = np.full(len(lc), np.nan)
        fwdH[:-H] = (lc[H:] - lc[:-H]) * 1e4
        for thr in (0.3, 0.5, 0.7):
            sel = np.isfinite(x) & (np.abs(x) >= thr)
            side = np.sign(x)
            summarize({"strategy": f"raw book rule H={H}", "variant": f"{sig} |x|>={thr} (all signals)"},
                      (side * fwdH)[sel], dayb[sel], foldb[sel])
            if thr == 0.5:
                idx = seq_fixed(b.index, sel, side, fwdH, H)
                summarize({"strategy": f"raw book rule H={H}", "variant": f"{sig} |x|>=0.5 SEQUENTIAL"},
                          (side * fwdH)[idx], dayb[idx], foldb[idx])
print("raw rules done")

# ---------------------------------------------------------------- 3) breakout (pre-registered rules, cost 0)
oos = pd.read_parquet("out/oos_H60.parquet")[["ts", "fold", "vol_hgb", "sigma1"]].set_index("ts")
bb = pd.read_parquet("out/bars_1m_v2.parquet").join(oos, how="left")
bb["fsig60"] = np.sqrt(np.exp(bb["vol_hgb"])) * 1e4 * math.sqrt(60)
bb["ratio"] = bb["fsig60"] / (bb["sigma1"] * math.sqrt(60))
bb = bb[(bb["fsig60"].notna() & bb["fold"].notna()).values].copy()
bb["exp_top"] = bb.groupby("fold")["ratio"].transform(lambda s: s >= s.quantile(2 / 3))
bb["lvl_top"] = bb.groupby("fold")["fsig60"].transform(lambda s: s >= s.quantile(2 / 3))
bb["us"] = np.isin(bb.index.hour, list(range(13, 17)))
n = len(bb)
tsb = bb.index
close, hi, lo = bb["close"].to_numpy(), bb["l2_mid_hi"].to_numpy(), bb["l2_mid_lo"].to_numpy()
fsig, fold_b = bb["fsig60"].to_numpy(), bb["fold"].to_numpy().astype(int)
day_b = bb.index.floor("D").astype(str).to_numpy()
tmin = ((tsb.tz_convert("UTC").tz_localize(None) - pd.Timestamp("1970-01-01")) // pd.Timedelta(minutes=1)).to_numpy().astype(np.int64)


def contiguous(i, j):
    return tmin[j] - tmin[i] == j - i


def breakout(cond, k, exit_mode, W=60, CAP=240):
    out = []
    i = 0
    while i < n:
        if not cond[i] or not np.isfinite(fsig[i]):
            i += 1
            continue
        d = k * fsig[i]
        up, dn = close[i] * math.exp(d / 1e4), close[i] * math.exp(-d / 1e4)
        trig, j_end = None, min(i + W, n - 1)
        for j in range(i + 1, j_end + 1):
            if not contiguous(i, j):
                break
            hu, hd = hi[j] >= up, lo[j] <= dn
            if hu or hd:
                side = (1 if (up - close[j - 1]) <= (close[j - 1] - dn) else -1) if (hu and hd) else (1 if hu else -1)
                trig = (j, side)
                break
        if trig is None:
            i = j_end + 1
            continue
        j, side = trig
        entry = up if side > 0 else dn
        tm = 1.0 if exit_mode == "1to1" else 2.0
        target = entry * math.exp(side * tm * d / 1e4)
        stop = entry * math.exp(-side * d / 1e4)
        exit_px, exit_j = None, None
        if (side > 0 and lo[j] <= stop) or (side < 0 and hi[j] >= stop):
            exit_px, exit_j = stop, j
        else:
            last = j
            for m in range(j + 1, min(j + CAP, n - 1) + 1):
                if not contiguous(j, m):
                    break
                last = m
                if side > 0:
                    if lo[m] <= stop:
                        exit_px, exit_j = stop, m
                        break
                    if exit_mode != "trail" and hi[m] >= target:
                        exit_px, exit_j = target, m
                        break
                    if exit_mode == "trail":
                        stop = max(stop, hi[m] * math.exp(-d / 1e4))
                else:
                    if hi[m] >= stop:
                        exit_px, exit_j = stop, m
                        break
                    if exit_mode != "trail" and lo[m] <= target:
                        exit_px, exit_j = target, m
                        break
                    if exit_mode == "trail":
                        stop = min(stop, lo[m] * math.exp(d / 1e4))
            if exit_px is None:
                exit_px, exit_j = close[last], last
        out.append((side * math.log(exit_px / entry) * 1e4, day_b[j], fold_b[i]))
        i = exit_j + 1
    return out


for name, cond in (("expansion_top3 all hours (PRIMARY)", bb["exp_top"].to_numpy()),
                   ("expansion_top3 US 13-17", (bb["exp_top"] & bb["us"]).to_numpy()),
                   ("level_top3 all hours", bb["lvl_top"].to_numpy()),
                   ("unconditional all hours", np.ones(n, dtype=bool))):
    for k, e in ((1.0, "2to1"), (1.0, "1to1"), (1.0, "trail"), (0.5, "2to1")):
        T = breakout(cond, k, e)
        if T:
            g, d_, f_ = zip(*T)
            summarize({"strategy": "breakout", "variant": f"{name} d={k}sig {e}"}, g, d_, f_)
print("breakout done")

# ---------------------------------------------------------------- 4) passive range fade (corrected rules, zero fees):
# entry and target are resting limits (no spread paid); stop and cap are market legs: they pay half the
# round-trip spread+slippage each, so cost_legs = share of trades exiting by stop/cap * 0.5
THROUGH = 0.5 / 1e4


def fade(cond, k, r, s, W=60, CAP=240):
    out = []
    i = 0
    while i < n:
        if not cond[i] or not np.isfinite(fsig[i]):
            i += 1
            continue
        d = k * fsig[i]
        up, dn = close[i] * math.exp(d / 1e4), close[i] * math.exp(-d / 1e4)
        hit, j_end = None, min(i + W, n - 1)
        for j in range(i + 1, j_end + 1):
            if not contiguous(i, j):
                break
            hu, hd = hi[j] >= up * (1 + THROUGH), lo[j] <= dn * (1 - THROUGH)
            if hu or hd:
                side = (-1 if (up - close[j - 1]) <= (close[j - 1] - dn) else 1) if (hu and hd) else (-1 if hu else 1)
                hit = (j, side)
                break
        if hit is None:
            i = j_end + 1
            continue
        j, side = hit
        entry = up if side < 0 else dn
        target = entry * math.exp(side * r * d / 1e4)
        stop = entry * math.exp(-side * s * d / 1e4)
        exit_px, exit_j, reason = None, None, None
        if (side > 0 and lo[j] <= stop) or (side < 0 and hi[j] >= stop):
            exit_px, exit_j, reason = stop, j, "market"
        else:
            last = j
            for m in range(j + 1, min(j + CAP, n - 1) + 1):
                if not contiguous(j, m):
                    break
                last = m
                if side > 0:
                    if lo[m] <= stop:
                        exit_px, exit_j, reason = stop, m, "market"
                        break
                    if hi[m] >= target * (1 + THROUGH):
                        exit_px, exit_j, reason = target, m, "limit"
                        break
                else:
                    if hi[m] >= stop:
                        exit_px, exit_j, reason = stop, m, "market"
                        break
                    if lo[m] <= target * (1 - THROUGH):
                        exit_px, exit_j, reason = target, m, "limit"
                        break
            if exit_px is None:
                exit_px, exit_j, reason = close[last], last, "market"
        out.append((side * math.log(exit_px / entry) * 1e4, day_b[j], fold_b[i], reason))
        i = exit_j + 1
    return out


for name, cond in (("level_top3", bb["lvl_top"].to_numpy()), ("expansion_top3", bb["exp_top"].to_numpy()),
                   ("unconditional", np.ones(n, dtype=bool))):
    for k, r, s in ((1.5, 1.0, 2.0), (1.5, 1.0, 3.0), (1.0, 0.5, 3.0), (1.0, 1.0, 2.0), (0.75, 0.5, 2.0)):
        T = fade(cond, k, r, s)
        if T:
            g, d_, f_, reason = zip(*T)
            mkt_share = np.mean([x == "market" for x in reason])
            summarize({"strategy": "passive fade", "variant": f"{name} d={k}sig target {r}d stop {s}d (market-exit share {mkt_share:.2f})"},
                      g, d_, f_, cost_legs=0.5 * mkt_share)
print("fade done")

R = pd.DataFrame(rows)
R.to_csv("out/cost_sensitivity.csv", index=False)
pd.set_option("display.width", 260)
cols = ["strategy", "variant", "n_trades", "gross_bps", "t_gross", "folds_pos_gross", "breakeven_cost_bps", "cost_at_t2_bps",
        "net_tight_1.5", "t_tight_1.5", "net_moderate_4", "t_moderate_4", "net_wide_9", "folds_pos_moderate_4"]
print("\n=== strategies with positive gross, sorted by cost_at_t2 (how much round-trip cost keeps t >= 2) ===")
print(R[R.gross_bps > 0].sort_values("cost_at_t2_bps", ascending=False).head(25)[cols].round(2).to_string(index=False))
print("\n=== primary cells of each family ===")
pri = R[R.variant.str.contains("PRIMARY|PL2_gb top10% fixed-hold SEQUENTIAL|dimb05_end \\|x\\|>=0.5 SEQUENTIAL|level_top3 d=1.5sig target 1.0d stop 2.0d", regex=True)]
print(pri[cols].round(2).to_string(index=False))
