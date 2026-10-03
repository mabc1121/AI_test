#!/usr/bin/env python3
"""
transfer_eval.py — external validation of the Bybit-trained models and strategies on Bitfinex, with NO fitting,
tuning or calibration on Bitfinex.

Models: reconstructed deterministically from the kit (same features, hyper-parameters, seed), fitted on the FULL 92
Bybit days (the deployable model), saved under models_bybit/.  Bitfinex is used only at prediction time.
Label-free operations on Bitfinex: feature construction from its own trailing windows (as the kit does on any venue),
score quantiles for confidence subsets, vol-forecast terciles for gates and breakout/fade conditions.
Costs on Bitfinex: zero fees; taker leg = half the actual spread at entry + 0.5 bps slippage (round trip = spread + 1).
"""
import math
import os
import pickle
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
V2 = os.path.abspath(os.path.join(HERE, "..", "real", "v2run"))
sys.path.insert(0, V2)
sys.argv = ["x"]
os.chdir(V2)
import research as R  # noqa: E402
import config as C  # noqa: E402


class GB:
    """picklable twin of research.fit_bin: identical estimator, parameters and seed"""
    def __init__(self, X, y):
        from sklearn.ensemble import HistGradientBoostingClassifier
        self.m = HistGradientBoostingClassifier(early_stopping=False, random_state=C.RANDOM_SEED, **R.gb_params()).fit(X, y)
        self.pos = list(self.m.classes_).index(1) if 1 in self.m.classes_ else None

    def predict(self, X):
        return self.m.predict_proba(X)[:, self.pos] if self.pos is not None else np.zeros(len(X))


class LR:
    """picklable twin of research.fit_lr: same standardisation, clipping, C and solver settings"""
    def __init__(self, X, y):
        from sklearn.linear_model import LogisticRegression
        self.mu, self.sd = np.nanmean(X, axis=0), np.nanstd(X, axis=0) + 1e-9
        self.const = float(y.mean()) if y.min() == y.max() else None
        if self.const is None:
            self.m = LogisticRegression(C=getattr(C, "LR_C", 0.1), max_iter=2000).fit(self._prep(X), y)
            self.pos = list(self.m.classes_).index(1)

    def _prep(self, X):
        return np.clip(np.nan_to_num((X - self.mu) / self.sd, nan=0.0, posinf=0.0, neginf=0.0), -10, 10)

    def predict(self, X):
        return np.full(len(X), self.const) if self.const is not None else self.m.predict_proba(self._prep(X))[:, self.pos]


HORIZONS = [5, 15, 30, 60, 120, 240]
TOP = [0.05, 0.10, 0.20, 1.00]
SLIP = 0.5
SHIFT_THRESHOLD = 0.4

# ------------------------------------------------------------------ data
by = R.load_bars()
TRAIN_END = os.environ.get("TRAIN_END")                 # e.g. 2026-09-24 -> train strictly before this UTC day; models frozen there
MODELS_DIR, OUT_DIR = "models_bybit", "."
if TRAIN_END:
    by = by[by.index < pd.Timestamp(TRAIN_END, tz="UTC")]
    tag = TRAIN_END.replace("-", "")
    MODELS_DIR, OUT_DIR = f"models_bybit_before{tag}", f"clean_before{tag}"
    if R.LONG_WIN != 1440:
        MODELS_DIR, OUT_DIR = f"{MODELS_DIR}_w{R.LONG_WIN}", f"{OUT_DIR}_w{R.LONG_WIN}"
    print(f"training window cut: Bybit {by.index[0]} .. {by.index[-1]} ({len(by):,} minutes)")
fby = R.build_features(by)
os.chdir(HERE)
os.makedirs(OUT_DIR, exist_ok=True)
bf = pd.read_parquet("kit/out/bars_1m_v2.parquet")
fbf = R.build_features(bf)
shift = pd.read_csv("feature_shift.csv").set_index("feature")["shift"] if os.path.exists("feature_shift.csv") else pd.Series(dtype=float)

P = [c for c in fby.columns if c.startswith(("ret_", "lrv_", "rvr_", "rpos_", "hl15", "tod_", "dow", "mins_to"))]
F = [c for c in fby.columns if c.startswith(("timb_", "volz_", "nlarge_", "ofi_", "netadd_"))]
B = [c for c in fby.columns if c.startswith(("spread_", "bimb_", "micro_", "dimb", "ldtot", "dtot1_chg", "l10_"))]
L2 = [c for c in fby.columns if c.startswith("l2_")]
GROUPS = {"P": P, "F": F, "B": B, "L2": L2, "PL2": P + L2, "ALL": P + F + B + L2}
HAR = [c for c in ("lrv_5", "lrv_15", "lrv_60", "lrv_240", "lrv_1440", "tod_sin", "tod_cos") if c in fby]


def shifted_share(cols):
    s = [shift.get(c, 0.0) for c in cols]
    return float(np.mean([v > SHIFT_THRESHOLD for v in s])) if s else 0.0


def prep(b, f):
    sig = f["sigma1"].values
    quality = (b["n_rows"].values >= C.MIN_ROWS_PER_MIN) & np.isfinite(f["lrv_1440" if "lrv_1440" in f else "lrv_240"].values) & np.isfinite(sig)
    rv = b["rv"].fillna(0.0)
    return sig, quality, rv


sig_by, q_by, rv_by = prep(by, fby)
sig_bf, q_bf, rv_bf = prep(bf, fbf)
spread_bf = bf["spread_end"].ffill().fillna(0.0).values
cost_rt_bf = spread_bf + 2 * SLIP                      # taker both legs, zero fees
day_bf = bf.index.floor("D").astype(str).to_numpy()
ALL_DAYS = np.array(sorted(set(day_bf[q_bf])))


def tstat(x):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return x.mean() / (x.std(ddof=1) / math.sqrt(len(x))) if len(x) > 2 and x.std(ddof=1) > 0 else np.nan


def daily_stats(net, days):
    s = pd.Series(net).groupby(days).sum().reindex(ALL_DAYS).fillna(0.0)
    return int((s > 0).sum()), len(s), tstat(s.values)


# ------------------------------------------------------------------ 1) reconstruct the Bybit models (full 92 days)
os.makedirs(MODELS_DIR, exist_ok=True)
t0 = time.time()
models = {}
for H in HORIZONS:
    path = f"{MODELS_DIR}/H{H}.pkl"
    if os.path.exists(path):
        models[H] = pickle.load(open(path, "rb"))
        continue
    lab = R.triple_barrier(by["close"].values, by["high"].values, by["low"].values, sig_by, H, C.BARRIER_K)
    y_vol = np.log(rv_by.rolling(H).sum().shift(-H).values / H + 1e-12)
    valid = q_by & np.isfinite(lab["label"]) & np.isfinite(y_vol)
    idx = np.where(valid)[0][:: C.TRAIN_STRIDE.get(H, 1)]
    y_dir, y_hit = (lab["fwd"][idx] > 0).astype(float), (lab["label"][idx] != 0).astype(float)
    m = {"prior_up": float(y_dir.mean()), "prior_hit": float(y_hit.mean()), "train_mean_yvol": float(y_vol[idx].mean()),
         "n_train": int(len(idx)), "dir": {}, "hit": {}}
    for g, cols in GROUPS.items():
        X = fby[cols].values[idx]
        for mdl, fitter in (("gb", GB), ("lr", LR)):
            m["dir"][f"{g}_{mdl}"] = fitter(X, y_dir)
            m["hit"][f"{g}_{mdl}"] = fitter(X, y_hit)
    m["vol_hgb"] = R.fit_reg(fby[GROUPS["ALL"]].values[idx], y_vol[idx])
    Xh = fby[HAR].values[idx]
    mu = np.nanmean(Xh, axis=0)
    from sklearn.linear_model import LinearRegression
    m["har"] = (LinearRegression().fit(np.where(np.isfinite(Xh), Xh, mu), y_vol[idx]), mu)
    models[H] = m
    pickle.dump(m, open(path, "wb"))
    print(f"fitted Bybit models H={H}: {len(idx):,} rows ({time.time()-t0:.0f}s)", flush=True)

# ------------------------------------------------------------------ 2) apply to Bitfinex
skill, pnl, volm, sims = [], [], [], []
close, high, low = bf["close"].values, bf["high"].values, bf["low"].values
for H in HORIZONS:
    m = models[H]
    lab = R.triple_barrier(close, high, low, sig_bf, H, C.BARRIER_K)
    y_vol = np.log(rv_bf.rolling(H).sum().shift(-H).values / H + 1e-12)
    valid = q_bf & np.isfinite(lab["label"]) & np.isfinite(y_vol)
    te = np.where(valid)[0]
    y_dir = (lab["fwd"][te] > 0).astype(float)
    y_hit = (lab["label"][te] != 0).astype(float)
    fwd, bw = lab["fwd"][te], lab["bw"][te]
    g_long, g_short, t_exit = lab["g_long"][te], lab["g_short"][te], lab["t_exit"][te]
    days = day_bf[te]
    cost = cost_rt_bf[te]
    ts_te = bf.index[te]
    # volatility model
    pv = m["vol_hgb"].predict(fbf[GROUPS["ALL"]].values[te])
    har_m, mu = m["har"]
    Xh = fbf[HAR].values[te]
    ph = har_m.predict(np.where(np.isfinite(Xh), Xh, mu))
    yv = y_vol[te]
    sst = ((yv - m["train_mean_yvol"]) ** 2).sum()
    for name, pred in (("vol_hgb", pv), ("vol_har", ph)):
        from scipy.stats import spearmanr
        volm.append({"H_min": H, "model": name, "r2_external": 1 - ((yv - pred) ** 2).sum() / sst,
                     "spearman": float(spearmanr(pred, yv)[0]), "n": len(te)})
    gate_hi = pv >= np.median(pv)
    ll0 = R.binll(np.full(len(te), m["prior_up"]), y_dir)
    for key in m["dir"]:
        g, mdl = key.rsplit("_", 1)
        X = fbf[GROUPS[g]].values[te]
        p_up, p_hit = m["dir"][key].predict(X), m["hit"][key].predict(X)
        score = p_up - m["prior_up"]
        ev = 2 * np.abs(score) * p_hit * bw
        from scipy.stats import spearmanr
        skill.append({"H_min": H, "group": g, "model": mdl, "shifted_feature_share": shifted_share(GROUPS[g]), "n": len(te),
                      "dir_auc": R.safe_auc(y_dir, p_up), "dir_skill_pct": (ll0 - R.binll(p_up, y_dir)) / ll0 * 100,
                      "ic_score_fwd": float(spearmanr(score, fwd, nan_policy="omit")[0]),
                      "hit_auc": R.safe_auc(y_hit, p_hit), "p_up_mean": float(p_up.mean()), "realised_up": float(y_dir.mean())})
        side = np.sign(score)
        for select, rank in (("score", np.abs(score)), ("ev", ev)):
            for q in TOP:
                tau = np.nanquantile(rank, 1 - q) if q < 1 else -np.inf
                for gate_name, gate in (("all", np.ones(len(te), bool)), ("pred_vol_high", gate_hi)):
                    if gate_name != "all" and (g != "ALL" or select != "ev"):
                        continue
                    sel = np.isfinite(score) & (score != 0) & (rank >= tau) & gate
                    if sel.sum() < 20:
                        continue
                    gross_fix = (side * fwd)[sel]
                    gross_bar = np.where(side[sel] > 0, g_long[sel], g_short[sel])
                    for basis, gross in (("fixed_hold", gross_fix), ("barrier", gross_bar)):
                        net = gross - cost[sel]
                        dpos, dn, t = daily_stats(net, days[sel])
                        pnl.append({"H_min": H, "group": g, "model": mdl, "select": select, "gate": gate_name, "top_frac": q,
                                    "exit": basis, "n_trades": int(sel.sum()), "win_rate": float((gross > 0).mean()),
                                    "gross_bps": float(gross.mean()), "spread_rt_bps": float(cost[sel].mean() - 2 * SLIP),
                                    "net_bps": float(net.mean()), "days_pos": f"{dpos}/{dn}", "t_daily": t})
        # one-position-at-a-time, fixed hold, top 10% by ev (label-free quantile)
        tau = np.nanquantile(ev, 0.9)
        sel = np.isfinite(score) & (score != 0) & (ev >= tau)
        tmin = ((ts_te.tz_convert("UTC").tz_localize(None) - pd.Timestamp("1970-01-01")) // pd.Timedelta(minutes=1)).to_numpy()
        taken, block = [], -1
        for i in np.where(sel)[0]:
            if tmin[i] < block:
                continue
            taken.append(i)
            block = tmin[i] + H + 1
        taken = np.array(taken, dtype=int)
        if len(taken) >= 10:
            gross = (side * fwd)[taken]
            net = gross - cost[taken]
            dpos, dn, t = daily_stats(net, days[taken])
            sims.append({"H_min": H, "group": g, "model": mdl, "sim": "sequential fixed-hold top10% ev", "n_trades": len(taken),
                         "trades_per_day": len(taken) / len(ALL_DAYS), "win_rate": float((gross > 0).mean()),
                         "gross_bps": float(gross.mean()), "net_bps": float(net.mean()), "days_pos": f"{dpos}/{dn}", "t_daily": t})
    print(f"Bitfinex H={H} evaluated ({time.time()-t0:.0f}s)", flush=True)

S, Pn, V, Sm = pd.DataFrame(skill), pd.DataFrame(pnl), pd.DataFrame(volm), pd.DataFrame(sims)
S.to_csv(os.path.join(OUT_DIR, "transfer_skill.csv"), index=False)
Pn.to_csv(os.path.join(OUT_DIR, "transfer_pnl.csv"), index=False)
V.to_csv(os.path.join(OUT_DIR, "transfer_vol.csv"), index=False)
Sm.to_csv(os.path.join(OUT_DIR, "transfer_sims.csv"), index=False)

# ------------------------------------------------------------------ 3) breakout and fade with the Bybit vol model's forecast
m60 = models[60]
pv60 = np.full(len(bf), np.nan)
ok60 = q_bf
pv60[ok60] = m60["vol_hgb"].predict(fbf[GROUPS["ALL"]].values[ok60])
fsig = np.sqrt(np.exp(pv60)) * 1e4 * math.sqrt(60)
ratio = fsig / (sig_bf * math.sqrt(60))
exp_top = ratio >= np.nanquantile(ratio[ok60], 2 / 3)
lvl_top = fsig >= np.nanquantile(fsig[ok60], 2 / 3)
hi_, lo_ = bf["l2_mid_hi"].values, bf["l2_mid_lo"].values
tmin_all = ((bf.index.tz_convert("UTC").tz_localize(None) - pd.Timestamp("1970-01-01")) // pd.Timedelta(minutes=1)).to_numpy()
n = len(bf)


def contiguous(i, j):
    return tmin_all[j] - tmin_all[i] == j - i


def breakout(cond, k=1.0, exit_mode="2to1", W=60, CAP=240):
    out, i = [], 0
    while i < n:
        if not cond[i] or not np.isfinite(fsig[i]) or not ok60[i]:
            i += 1
            continue
        d = k * fsig[i]
        up, dn = close[i] * math.exp(d / 1e4), close[i] * math.exp(-d / 1e4)
        trig, j_end = None, min(i + W, n - 1)
        for j in range(i + 1, j_end + 1):
            if not contiguous(i, j):
                break
            hu, hd = hi_[j] >= up, lo_[j] <= dn
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
        target, stop = entry * math.exp(side * tm * d / 1e4), entry * math.exp(-side * d / 1e4)
        exit_px, exit_j = None, None
        if (side > 0 and lo_[j] <= stop) or (side < 0 and hi_[j] >= stop):
            exit_px, exit_j = stop, j
        else:
            last = j
            for mm in range(j + 1, min(j + CAP, n - 1) + 1):
                if not contiguous(j, mm):
                    break
                last = mm
                if side > 0:
                    if lo_[mm] <= stop:
                        exit_px, exit_j = stop, mm
                        break
                    if hi_[mm] >= target:
                        exit_px, exit_j = target, mm
                        break
                else:
                    if hi_[mm] >= stop:
                        exit_px, exit_j = stop, mm
                        break
                    if lo_[mm] <= target:
                        exit_px, exit_j = target, mm
                        break
            if exit_px is None:
                exit_px, exit_j = close[last], last
        out.append((side * math.log(exit_px / entry) * 1e4, day_bf[j], cost_rt_bf[j]))
        i = exit_j + 1
    return out


def fade(cond, k, r, s, W=60, CAP=240, through=0.5 / 1e4):
    out, i = [], 0
    while i < n:
        if not cond[i] or not np.isfinite(fsig[i]) or not ok60[i]:
            i += 1
            continue
        d = k * fsig[i]
        up, dn = close[i] * math.exp(d / 1e4), close[i] * math.exp(-d / 1e4)
        hit, j_end = None, min(i + W, n - 1)
        for j in range(i + 1, j_end + 1):
            if not contiguous(i, j):
                break
            hu, hd = hi_[j] >= up * (1 + through), lo_[j] <= dn * (1 - through)
            if hu or hd:
                side = (-1 if (up - close[j - 1]) <= (close[j - 1] - dn) else 1) if (hu and hd) else (-1 if hu else 1)
                hit = (j, side)
                break
        if hit is None:
            i = j_end + 1
            continue
        j, side = hit
        entry = up if side < 0 else dn
        target, stop = entry * math.exp(side * r * d / 1e4), entry * math.exp(-side * s * d / 1e4)
        exit_px, exit_j, kind = None, None, None
        if (side > 0 and lo_[j] <= stop) or (side < 0 and hi_[j] >= stop):
            exit_px, exit_j, kind = stop, j, "market"
        else:
            last = j
            for mm in range(j + 1, min(j + CAP, n - 1) + 1):
                if not contiguous(j, mm):
                    break
                last = mm
                if side > 0:
                    if lo_[mm] <= stop:
                        exit_px, exit_j, kind = stop, mm, "market"
                        break
                    if hi_[mm] >= target * (1 + through):
                        exit_px, exit_j, kind = target, mm, "limit"
                        break
                else:
                    if hi_[mm] >= stop:
                        exit_px, exit_j, kind = stop, mm, "market"
                        break
                    if lo_[mm] <= target * (1 - through):
                        exit_px, exit_j, kind = target, mm, "limit"
                        break
            if exit_px is None:
                exit_px, exit_j, kind = close[last], last, "market"
        c = (cost_rt_bf[exit_j] / 2) if kind == "market" else 0.0      # only market legs pay half spread + slippage
        out.append((side * math.log(exit_px / entry) * 1e4, day_bf[j], c))
        i = exit_j + 1
    return out


strat = []
for name, cond, fn, kw in (("breakout PRIMARY expansion d=1 2to1", exp_top, breakout, {}),
                           ("breakout level d=1 2to1", lvl_top, breakout, {}),
                           ("breakout unconditional d=1 2to1", np.ones(n, bool), breakout, {}),
                           ("breakout expansion d=1 1to1", exp_top, breakout, {"exit_mode": "1to1"}),
                           ("fade level d=1.5 target1d stop2d", lvl_top, fade, {"k": 1.5, "r": 1.0, "s": 2.0}),
                           ("fade expansion d=1 target0.5d stop3d", exp_top, fade, {"k": 1.0, "r": 0.5, "s": 3.0}),
                           ("fade unconditional d=1.5 target1d stop3d", np.ones(n, bool), fade, {"k": 1.5, "r": 1.0, "s": 3.0})):
    T = fn(cond, **kw)
    if not T:
        continue
    g, d_, c_ = (np.array(x) for x in zip(*T))
    net = g - c_
    dpos, dn, t = daily_stats(net, d_)
    strat.append({"strategy": name, "n_trades": len(g), "win_rate": float((g > 0).mean()), "gross_bps": float(g.mean()),
                  "net_bps": float(net.mean()), "days_pos": f"{dpos}/{dn}", "t_daily": t})
St = pd.DataFrame(strat)
St.to_csv(os.path.join(OUT_DIR, "transfer_strategies.csv"), index=False)
pd.set_option("display.width", 260)
print("\n=== skill (Bitfinex, external) ===")
print(S.round(4).to_string(index=False))
print("\n=== vol ===")
print(V.round(3).to_string(index=False))
print("\n=== strategies ===")
print(St.round(3).to_string(index=False))
print(f"\ndone in {time.time()-t0:.0f}s")
