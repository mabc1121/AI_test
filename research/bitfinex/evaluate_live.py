#!/usr/bin/env python3
"""
evaluate_live.py — score the app's live predictions per horizon from its prediction log alone.

The app writes one line per minute to <runtime>/aitest_predictions/YYYY-MM-DD.jsonl (on the server:
/var/lib/<app>/aitest_predictions/). Copy that folder here and run

  python evaluate_live.py --log <folder> [--costs 0 1 2 3] [--exclude-backfilled] [--min-history 240] [--out dir]

For each horizon (30/60/120/240 min) it reports, on minutes that pass the research quality rule:
  direction AUC (p_up vs sign of the forward mid return), big-move AUC (p_hit vs a barrier hit at k*sigma*sqrt(H)),
  volatility R2 (vol_raw vs log mean realised variance; needs the 'rv' column, written by app >= 1.0.0),
  and the signal trades (the app's own entry rule): count, win rate, gross bps (barrier exit on the minute high/low,
  at the mid), net bps at each round-trip cost, positive days and the daily t-statistic.
The app's paper account (actual fills, spread and slippage) is in its Trade page; this script is the model view.
"""
import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

HORIZONS = (30, 60, 120, 240)


def auc(y, s):
    ok = np.isfinite(y) & np.isfinite(s)
    y, s = y[ok], s[ok]
    n1 = int(y.sum()); n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return np.nan
    r = pd.Series(s).rank().to_numpy()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def tstat(x):
    x = np.asarray(x, float); x = x[np.isfinite(x)]
    return x.mean() / (x.std(ddof=1) / math.sqrt(len(x))) if len(x) > 2 and x.std(ddof=1) > 0 else np.nan


def load(folder):
    rows = []
    for p in sorted(Path(folder).glob("*.jsonl")):
        for line in p.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    if not rows:
        raise SystemExit(f"no prediction lines in {folder}")
    d = pd.json_normalize(rows, sep="_")
    d = d.drop_duplicates("t", keep="first").sort_values("t").set_index("t")   # a restart re-logs (identically) the minutes after the last save
    full = np.arange(d.index.min(), d.index.max() + 60_000, 60_000)
    return d.reindex(full)


def barrier(close, high, low, sig, H, k):
    """research.py triple_barrier: +1 / -1 for the first barrier touched within H minutes (mid prices), 0 if none"""
    n = len(close); lab = np.full(n, np.nan); fwd = np.full(n, np.nan); ret_at = np.full(n, np.nan)
    for i in range(n - H):
        c, s = close[i], sig[i]
        if not (np.isfinite(c) and np.isfinite(s)):
            continue
        d = k * s * math.sqrt(H) / 1e4
        up, dn = c * math.exp(d), c * math.exp(-d)
        lab[i] = 0
        for j in range(i + 1, i + H + 1):
            hu, ld = high[j] >= up, low[j] <= dn
            if hu and ld:
                lab[i] = 0; break
            if hu:
                lab[i] = 1; break
            if ld:
                lab[i] = -1; break
        fwd[i] = math.log(close[i + H] / c) * 1e4 if np.isfinite(close[i + H]) else np.nan
        ret_at[i] = d * 1e4
    return lab, fwd, ret_at


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--costs", nargs="*", type=float, default=[0.0, 1.0, 2.0, 3.0], help="round-trip costs in bps")
    ap.add_argument("--barrier-k", type=float, default=1.0)
    ap.add_argument("--min-history", type=int, default=240)
    ap.add_argument("--exclude-backfilled", action="store_true")
    ap.add_argument("--rows-only", action="store_true", help="use minutes with >= 30 rows even before the 12 h the research quality rule needs")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    d = load(a.log)
    close, high, low, sig = (d[c].to_numpy(float) for c in ("close", "high", "low", "sigma1"))
    good = (d["n_rows"].fillna(0).to_numpy() >= 30) if a.rows_only else d["quality"].fillna(False).astype(bool).to_numpy()
    use = good & (d["history_min"].fillna(0).to_numpy() >= a.min_history)
    if a.exclude_backfilled:
        use &= ~d["backfilled"].fillna(False).astype(bool).to_numpy()
    day = pd.to_datetime(d.index, unit="ms", utc=True).floor("D")
    skill, pnl = [], []
    for H in HORIZONS:
        if f"H{H}_p_up" not in d:
            continue
        lab, fwd, dist = barrier(close, high, low, sig, H, a.barrier_k)
        p_up, p_hit = d[f"H{H}_p_up"].to_numpy(float), d[f"H{H}_p_hit"].to_numpy(float)
        ok = use & np.isfinite(fwd)
        row = {"H_min": H, "minutes": int(ok.sum()), "days": int(pd.Series(day[ok]).nunique()),
               "dir_auc": auc((fwd[ok] > 0).astype(float), p_up[ok]),
               "hit_auc": auc((lab[ok] != 0).astype(float), p_hit[ok]),
               "hit_rate": float(np.nanmean(lab[ok] != 0)) if ok.any() else np.nan}
        if "rv" in d:
            rv = d["rv"].to_numpy(float)
            yv = np.full(len(rv), np.nan)
            for i in range(len(rv) - H):
                w = rv[i + 1:i + H + 1]
                if np.isfinite(w).all():
                    yv[i] = math.log(w.mean() + 1e-12)
            vr = d[f"H{H}_vol_raw"].to_numpy(float); okv = ok & np.isfinite(yv) & np.isfinite(vr)
            row["vol_r2_vs_own_mean"] = 1 - np.sum((yv[okv] - vr[okv]) ** 2) / np.sum((yv[okv] - yv[okv].mean()) ** 2) if okv.sum() > 10 else np.nan
        skill.append(row)
        # the app's entry rule (the signal column), barrier exit, one trade per signal minute
        sigcol = d[f"H{H}_signal"].to_numpy(object)
        idx = [i for i in np.where(ok)[0] if sigcol[i] in ("long", "short") and np.isfinite(lab[i])]
        g = []
        for i in idx:
            side = 1 if sigcol[i] == "long" else -1
            gross = side * (lab[i] * dist[i]) if lab[i] != 0 else side * fwd[i]
            g.append((i, gross))
        if not g:
            pnl.append({"H_min": H, "n_trades": 0}); continue
        gi = np.array([x[0] for x in g]); gv = np.array([x[1] for x in g])
        base = {"H_min": H, "n_trades": len(gv), "win_rate": float((gv > 0).mean()), "gross_bps": float(gv.mean())}
        for c in a.costs:
            net = gv - c
            dsum = pd.Series(net, index=day[gi]).groupby(level=0).sum().reindex(sorted(set(day[ok]))).fillna(0.0)
            base[f"net_bps_cost{c:g}"] = float(net.mean())
            base[f"days_pos_cost{c:g}"] = f"{int((dsum > 0).sum())}/{len(dsum)}"
            base[f"t_daily_cost{c:g}"] = tstat(dsum.values)
        pnl.append(base)
    S, P = pd.DataFrame(skill), pd.DataFrame(pnl)
    pd.set_option("display.width", 250)
    print("=== skill per horizon (quality minutes) ==="); print(S.round(4).to_string(index=False))
    print("\n=== the app's signal trades, barrier exit at the mid ==="); print(P.round(3).to_string(index=False))
    if a.out:
        Path(a.out).mkdir(parents=True, exist_ok=True)
        S.to_csv(Path(a.out) / "live_skill.csv", index=False); P.to_csv(Path(a.out) / "live_signal_pnl.csv", index=False)


if __name__ == "__main__":
    main()
