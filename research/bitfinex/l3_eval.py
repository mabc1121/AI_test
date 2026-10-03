#!/usr/bin/env python3
"""l3_eval.py — does order-level (level-3) information improve direction on Bitfinex, overall and when a big move is
predicted? Exploration/confirmation split, frozen Bybit hit model for the big-move condition, no tuning. See L3_PREREG.md."""
import glob
import math
import os
import pickle
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from scipy.stats import spearmanr

HERE = os.path.dirname(os.path.abspath(__file__))
V2 = os.path.abspath(os.path.join(HERE, "..", "real", "v2run"))
sys.path.insert(0, V2); sys.argv = ["x"]; os.chdir(V2)
import research as R  # noqa: E402
import config as C  # noqa: E402
os.chdir(HERE)


class GB:  # unpickling twin
    def predict(self, X):
        return self.m.predict_proba(X)[:, self.pos] if self.pos is not None else np.zeros(len(X))


class LR:
    def _prep(self, X):
        return np.clip(np.nan_to_num((X - self.mu) / self.sd, nan=0.0, posinf=0.0, neginf=0.0), -10, 10)

    def predict(self, X):
        return np.full(len(X), self.const) if self.const is not None else self.m.predict_proba(self._prep(X))[:, self.pos]


import __main__  # noqa: E402
__main__.GB, __main__.LR = GB, LR

OUT = "l3_out"; os.makedirs(OUT, exist_ok=True)
MODELS = "models_bybit_before20260924"
EXPL_END = pd.Timestamp("2026-09-29", tz="UTC")       # exploration < this, confirmation >= this
HS = [30, 60]
SLIP = 0.5
EPS = 1e-9
pd.set_option("display.width", 320); pd.set_option("display.max_rows", 400)

# ------------------------------------------------------------------ kit bars, features, frozen models
bf = pd.read_parquet("kit/out/bars_1m_v2.parquet")
fbf = R.build_features(bf)
P = [c for c in fbf.columns if c.startswith(("ret_", "lrv_", "rvr_", "rpos_", "hl15", "tod_", "dow", "mins_to"))]
F = [c for c in fbf.columns if c.startswith(("timb_", "volz_", "nlarge_", "ofi_", "netadd_"))]
B = [c for c in fbf.columns if c.startswith(("spread_", "bimb_", "micro_", "dimb", "ldtot", "dtot1_chg", "l10_"))]
L2 = [c for c in fbf.columns if c.startswith("l2_")]
ALL = P + F + B + L2
sig = fbf["sigma1"].values
quality = (bf["n_rows"].values >= C.MIN_ROWS_PER_MIN) & np.isfinite(fbf["lrv_1440"].values) & np.isfinite(sig)
spread = bf["spread_end"].ffill().fillna(0.0).values
day = bf.index.floor("D")
days = np.array(sorted(set(day)))

# ------------------------------------------------------------------ level-3 minute features
l3s = pd.concat([pd.read_parquet(f) for f in sorted(glob.glob("data_bitfinex/bitfinex_l3/*.parquet"))])
l3s.index = pd.to_datetime(l3s.index.astype("int64"), unit="s", utc=True)
minute = l3s.index.floor("min")
STATE = ["n1", "n3", "n10", "old3", "fresh3", "old10", "max3", "max10", "dmax10", "snap3", "medage3"]
FLOW = ["nadd", "ncxl", "cxl_fresh", "cxl_old", "exec", "nexec", "nexlvl", "refill", "bigadd", "bigcxl"]
st = l3s[[f"{c}_{s}" for c in STATE for s in "ba"]].groupby(minute).last()
fl = l3s[[f"{c}_{s}" for c in FLOW for s in "ba"]].groupby(minute).sum()
m3 = st.join(fl).reindex(bf.index)
for c in fl.columns:
    m3[c] = m3[c].fillna(0.0)
m3[st.columns] = m3[st.columns].ffill(limit=5)


def imb(a, b):  # (a - b) / (a + b)
    return (a - b) / (a + b + EPS)


def l3_features(m, shift=0):
    """directional level-3 features; shift>0 means use the values from `shift` minutes earlier (placebo)"""
    m = m.shift(shift) if shift else m
    X = pd.DataFrame(index=m.index)
    X["l3_cnt_imb1"] = imb(m.n1_b, m.n1_a)
    X["l3_cnt_imb3"] = imb(m.n3_b, m.n3_a)
    X["l3_cnt_imb10"] = imb(m.n10_b, m.n10_a)
    X["l3_old_imb3"] = imb(m.old3_b, m.old3_a)
    X["l3_old_imb10"] = imb(m.old10_b, m.old10_a)
    X["l3_fresh_imb3"] = imb(m.fresh3_b, m.fresh3_a)
    X["l3_wall_imb10"] = imb(m.max10_b, m.max10_a)
    X["l3_wall_dist"] = m.dmax10_a - m.dmax10_b
    X["l3_age_diff"] = np.log1p(m.medage3_b) - np.log1p(m.medage3_a)
    dep3 = bf["dtot3_end"].reindex(m.index)
    X["l3_old_share3"] = (m.old3_b + m.old3_a) / (dep3 + EPS)
    X["l3_fresh_share3"] = (m.fresh3_b + m.fresh3_a) / (dep3 + EPS)
    for w in (1, 5, 15):
        r = m.rolling(w, min_periods=1).sum() if w > 1 else m
        s = f"_{w}"
        X[f"l3_exec_imb{s}"] = imb(r.exec_a, r.exec_b)            # buyer-initiated executions minus seller-initiated
        X[f"l3_nexec_imb{s}"] = imb(r.nexec_a, r.nexec_b)
        X[f"l3_sweep_imb{s}"] = imb(r.nexlvl_a, r.nexlvl_b)
        X[f"l3_cxlfresh_imb{s}"] = imb(r.cxl_fresh_a, r.cxl_fresh_b)
        X[f"l3_cxlold_imb{s}"] = imb(r.cxl_old_a, r.cxl_old_b)
        X[f"l3_ncxl_imb{s}"] = imb(r.ncxl_a, r.ncxl_b)
        X[f"l3_nadd_imb{s}"] = imb(r.nadd_b, r.nadd_a)
        X[f"l3_refill_imb{s}"] = imb(r.refill_b, r.refill_a)
        X[f"l3_bigadd_imb{s}"] = imb(r.bigadd_b, r.bigadd_a)
        X[f"l3_bigcxl_imb{s}"] = imb(r.bigcxl_a, r.bigcxl_b)
        X[f"l3_nexec_tot{s}"] = np.log1p(r.nexec_a + r.nexec_b)
        X[f"l3_refill_tot{s}"] = np.log1p(r.refill_a + r.refill_b)
    return X


X3 = l3_features(m3)
X3p = l3_features(m3, shift=1440)                     # placebo: previous day, same minute
L3 = list(X3.columns)
L2REF = [c for c in ("bimb_end", "dimb05_end", "dimb1_end", "dimb3_end", "micro_end", "ofi_1", "ofi_5", "ofi_15", "timb_1", "timb_5", "timb_15",
                     "netadd_1", "netadd_5", "l2_imb3", "l2_imb10", "l2_wimb10", "l2_ofi_30", "l2_tot_imb") if c in fbf.columns]
print("level-3 features:", len(L3), "| level-2 reference features:", len(L2REF), "| ALL:", len(ALL))
print("level-3 coverage (non-NaN share) by day:\n", X3["l3_cnt_imb3"].notna().groupby(day).mean().round(3).to_string())


def tstat(x):
    x = np.asarray(x, float); x = x[np.isfinite(x)]
    return x.mean() / (x.std(ddof=1) / math.sqrt(len(x))) if len(x) > 2 and x.std(ddof=1) > 0 else np.nan


def auc(y, s):
    ok = np.isfinite(s) & np.isfinite(y)
    return roc_auc_score(y[ok], s[ok]) if ok.sum() > 50 and 0 < y[ok].mean() < 1 else np.nan


def ic(y, s):
    ok = np.isfinite(s) & np.isfinite(y)
    return spearmanr(s[ok], y[ok]).correlation if ok.sum() > 50 else np.nan


rows_A, rows_B1, rows_B2, rows_B3 = [], [], [], []
for H in HS:
    m = pickle.load(open(f"{MODELS}/H{H}.pkl", "rb"))
    lab = R.triple_barrier(bf["close"].values, bf["high"].values, bf["low"].values, sig, H, C.BARRIER_K)
    fwd = lab["fwd"]; ydir = (fwd > 0).astype(float); big_real = (lab["label"] != 0)
    p_hit = m["hit"]["ALL_gb"].predict(fbf[ALL].values)
    s_bybit = m["dir"]["ALL_gb"].predict(fbf[ALL].values) - m["prior_up"]
    valid = quality & np.isfinite(fwd) & X3["l3_cnt_imb3"].notna().values
    expl = valid & np.asarray(bf.index < EXPL_END - pd.Timedelta(minutes=H))
    conf = valid & np.asarray(bf.index >= EXPL_END)
    thr = np.nanquantile(p_hit[expl], 2 / 3)                      # fixed on exploration
    big_pred = p_hit >= thr
    subsets = {"all": np.ones(len(bf), bool), "pred_big": big_pred, "real_big": big_real}
    print(f"\nH={H}: exploration {expl.sum()} min, confirmation {conf.sum()} min, p_hit threshold {thr:.3f}, "
          f"pred_big share conf {big_pred[conf].mean():.2f}, real_big share conf {big_real[conf].mean():.2f}, "
          f"hit AUC of frozen model on conf {auc(big_real[conf].astype(float), p_hit[conf]):.3f}")

    # ---------------- Stage A: per-feature screening on exploration
    for name, cols, src in (("l3", L3, X3), ("l2", L2REF, fbf)):
        for c in cols:
            v = src[c].values
            r = {"H": H, "family": name, "feature": c}
            for sn, sm in subsets.items():
                i = expl & sm
                r[f"ic_{sn}"] = ic(fwd[i], v[i]); r[f"auc_{sn}"] = auc(ydir[i], v[i]); r[f"n_{sn}"] = int(i.sum())
            rows_A.append(r)
    A = pd.DataFrame([r for r in rows_A if r["H"] == H])
    top3 = A[A.family == "l3"].reindex(A[A.family == "l3"]["ic_pred_big"].abs().sort_values(ascending=False).index).head(3)
    print(f"\nStage A (exploration) top |IC| in predicted-big subset, H={H}:")
    print(A.reindex(A["ic_pred_big"].abs().sort_values(ascending=False).index).head(12).round(3).to_string(index=False))

    # ---------------- Stage B1: the three chosen features on confirmation
    for _, t in top3.iterrows():
        sgn = np.sign(t["ic_pred_big"]); v = X3[t["feature"]].values * sgn
        i = conf & big_pred
        per_day = [ic(fwd[i & np.asarray(day == d)], v[i & np.asarray(day == d)]) for d in days if (i & np.asarray(day == d)).sum() > 50]
        rows_B1.append({"H": H, "feature": t["feature"], "sign": int(sgn), "expl_ic": sgn * t["ic_pred_big"], "expl_auc": max(t["auc_pred_big"], 1 - t["auc_pred_big"]),
                        "conf_ic_pred_big": ic(fwd[i], v[i]), "conf_auc_pred_big": auc(ydir[i], v[i]), "conf_days_sign_ok": f"{sum(1 for x in per_day if x > 0)}/{len(per_day)}",
                        "conf_auc_all": auc(ydir[conf], v[conf]), "conf_auc_real_big": auc(ydir[conf & big_real], v[conf & big_real]),
                        "pass": bool(auc(ydir[i], v[i]) >= 0.54 and sum(1 for x in per_day if x > 0) >= 4)})

    # ---------------- Stage B2: models trained on exploration, evaluated on confirmation
    sets = {"L2(ALL)": fbf[ALL].values, "L2+L3": np.column_stack([fbf[ALL].values, X3.values]), "L3only": X3.values,
            "L2+L3placebo": np.column_stack([fbf[ALL].values, X3p.values])}
    scores = {}
    for train_name, train_mask in (("all", expl), ("pred_big", expl & big_pred)):
        for fs, X in sets.items():
            if fs == "L3only" and train_name == "pred_big":
                continue
            idx = np.where(train_mask)[0]
            y = ydir[idx]
            for mdl in ("gb", "lr"):
                if mdl == "gb":
                    clf = HistGradientBoostingClassifier(early_stopping=False, random_state=C.RANDOM_SEED, **R.gb_params()).fit(X[idx], y)
                    s = clf.predict_proba(X)[:, 1] - y.mean()
                else:
                    mu, sd = np.nanmean(X[idx], 0), np.nanstd(X[idx], 0) + 1e-9
                    Z = np.clip(np.nan_to_num((X - mu) / sd, nan=0.0, posinf=0.0, neginf=0.0), -10, 10)
                    clf = LogisticRegression(C=C.LR_C, max_iter=3000).fit(Z[idx], y)
                    s = clf.predict_proba(Z)[:, 1] - y.mean()
                key = (train_name, fs, mdl); scores[key] = s
                r = {"H": H, "train_on": train_name, "features": fs, "model": mdl}
                for sn, sm in subsets.items():
                    r[f"auc_{sn}"] = auc(ydir[conf & sm], s[conf & sm])
                    per_day = [auc(ydir[conf & sm & np.asarray(day == d)], s[conf & sm & np.asarray(day == d)]) for d in days]
                    per_day = [x for x in per_day if np.isfinite(x)]
                    r[f"days>0.5_{sn}"] = f"{sum(1 for x in per_day if x > 0.5)}/{len(per_day)}"
                rows_B2.append(r)
    scores[("frozen", "Bybit ALL_gb", "gb")] = s_bybit
    r = {"H": H, "train_on": "frozen Bybit", "features": "Bybit ALL_gb", "model": "gb"}
    for sn, sm in subsets.items():
        r[f"auc_{sn}"] = auc(ydir[conf & sm], s_bybit[conf & sm])
        per_day = [auc(ydir[conf & sm & np.asarray(day == d)], s_bybit[conf & sm & np.asarray(day == d)]) for d in days]
        per_day = [x for x in per_day if np.isfinite(x)]
        r[f"days>0.5_{sn}"] = f"{sum(1 for x in per_day if x > 0.5)}/{len(per_day)}"
    rows_B2.append(r)

    # ---------------- Stage B3: trading on confirmation, predicted-big minutes, barrier exit
    close, high, low = bf["close"].values, bf["high"].values, bf["low"].values
    n = len(bf)

    def barrier_trade(i, side, H):
        """enter at close[i] (taker), TP/SL at 1 sigma_H, time cap H; returns gross bps at mid"""
        d = C.BARRIER_K * sig[i] * math.sqrt(H)
        e = close[i]; tp = e * math.exp(side * d); sl = e * math.exp(-side * d)
        for j in range(i + 1, min(i + H, n - 1) + 1):
            if side > 0:
                if low[j] <= sl: return -d * 1e4
                if high[j] >= tp: return d * 1e4
            else:
                if high[j] >= sl: return -d * 1e4
                if low[j] <= tp: return d * 1e4
        j = min(i + H, n - 1)
        return side * math.log(close[j] / e) * 1e4

    for key, s in scores.items():
        if key[0] == "pred_big" or key[1] in ("L3only", "L2+L3placebo"):
            continue
        q80 = np.nanquantile(np.abs(s[expl & big_pred]), 0.8)          # fixed on exploration
        cand = np.where(conf & big_pred & (np.abs(s) >= q80))[0]
        for mode in ("overlapping", "one_position"):
            res = []; busy_until = -1
            for i in cand:
                if mode == "one_position" and i < busy_until:
                    continue
                side = 1 if s[i] > 0 else -1
                g = barrier_trade(i, side, H)
                res.append((i, g, g - spread[i] - 2 * SLIP))
                busy_until = i + H
            if not res:
                rows_B3.append({"H": H, "train_on": key[0], "features": key[1], "model": key[2], "mode": mode, "n_trades": 0}); continue
            arr = np.array([(g, nt) for _, g, nt in res]); ii = np.array([i for i, _, _ in res])
            dsum = pd.Series(arr[:, 1], index=day[ii]).groupby(level=0).sum()
            dsum = dsum.reindex([d for d in days if d >= EXPL_END]).fillna(0.0)
            rows_B3.append({"H": H, "train_on": key[0], "features": key[1], "model": key[2], "mode": mode, "n_trades": len(res),
                            "win_rate": float((arr[:, 0] > 0).mean()), "gross_bps": arr[:, 0].mean(), "net_bps": arr[:, 1].mean(),
                            "days_pos": f"{int((dsum > 0).sum())}/{len(dsum)}", "t_daily": tstat(dsum.values),
                            "pass": bool(arr[:, 1].mean() > 0 and tstat(dsum.values) >= 2 and (dsum > 0).sum() >= 4)})

A = pd.DataFrame(rows_A); B1 = pd.DataFrame(rows_B1); B2 = pd.DataFrame(rows_B2); B3 = pd.DataFrame(rows_B3)
A.to_csv(f"{OUT}/stageA_features.csv", index=False); B1.to_csv(f"{OUT}/stageB1_features.csv", index=False)
B2.to_csv(f"{OUT}/stageB2_models.csv", index=False); B3.to_csv(f"{OUT}/stageB3_trading.csv", index=False)
print("\n=== Stage B1: chosen features on confirmation ==="); print(B1.round(3).to_string(index=False))
print("\n=== Stage B2: models on confirmation (AUC; days above 0.5) ==="); print(B2.round(3).to_string(index=False))
print("\n=== Stage B3: trading on confirmation, predicted-big minutes ==="); print(B3.round(2).to_string(index=False))
for H in HS:
    b = B2[(B2.H == H) & (B2.train_on == "all")]
    for mdl in ("gb", "lr"):
        a2 = b[(b.features == "L2(ALL)") & (b.model == mdl)]["auc_pred_big"].values[0]
        a3 = b[(b.features == "L2+L3") & (b.model == mdl)]["auc_pred_big"].values[0]
        ap = b[(b.features == "L2+L3placebo") & (b.model == mdl)]["auc_pred_big"].values[0]
        print(f"B2 verdict H={H} {mdl}: pred_big AUC L2 {a2:.3f} -> L2+L3 {a3:.3f} (delta {a3-a2:+.3f}; placebo delta {ap-a2:+.3f}) -> {'PASS' if a3-a2 >= 0.02 else 'FAIL'}")

# ---------------- post-hoc diagnostics (not pre-registered): every feature on confirmation, and level-3 vs level-2 redundancy
rows_C = []
for H in HS:
    lab = R.triple_barrier(bf["close"].values, bf["high"].values, bf["low"].values, sig, H, C.BARRIER_K)
    fwd = lab["fwd"]; ydir = (fwd > 0).astype(float)
    m = pickle.load(open(f"{MODELS}/H{H}.pkl", "rb")); p_hit = m["hit"]["ALL_gb"].predict(fbf[ALL].values)
    valid = quality & np.isfinite(fwd) & X3["l3_cnt_imb3"].notna().values
    expl = valid & np.asarray(bf.index < EXPL_END - pd.Timedelta(minutes=H)); conf = valid & np.asarray(bf.index >= EXPL_END)
    big = p_hit >= np.nanquantile(p_hit[expl], 2 / 3)
    for name, cols, src in (("l3", L3, X3), ("l2", L2REF, fbf)):
        for c in cols:
            v = src[c].values
            per_day = [ic(fwd[conf & big & np.asarray(day == d)], v[conf & big & np.asarray(day == d)]) for d in days if (conf & big & np.asarray(day == d)).sum() > 50]
            rows_C.append({"H": H, "family": name, "feature": c, "expl_ic_pred_big": ic(fwd[expl & big], v[expl & big]), "conf_ic_pred_big": ic(fwd[conf & big], v[conf & big]),
                           "conf_auc_pred_big": auc(ydir[conf & big], v[conf & big]), "conf_days_same_sign": sum(1 for x in per_day if np.sign(x) == np.sign(ic(fwd[expl & big], v[expl & big]))),
                           "conf_ic_all": ic(fwd[conf], v[conf]), "max_abs_corr_with_l2": (np.nanmax(np.abs([pd.Series(v[valid]).corr(pd.Series(fbf[c2].values[valid]), method="spearman") for c2 in L2REF])) if name == "l3" else np.nan)})
Cc = pd.DataFrame(rows_C); Cc.to_csv(f"{OUT}/posthoc_confirmation_features.csv", index=False)
for H in HS:
    c = Cc[Cc.H == H]
    print(f"\n=== post-hoc, H={H}: features by |confirmation IC| in predicted-big subset (sign agreement with exploration) ===")
    c = c.assign(same_sign=np.sign(c.expl_ic_pred_big) == np.sign(c.conf_ic_pred_big))
    print(c.reindex(c.conf_ic_pred_big.abs().sort_values(ascending=False).index).head(15).round(3).to_string(index=False))
    l3c = c[c.family == "l3"]
    print(f"level-3 features with same-sign IC in both periods and |conf IC| >= 0.05: {int((l3c.same_sign & (l3c.conf_ic_pred_big.abs() >= 0.05)).sum())} of {len(l3c)} "
          f"(expected by chance with no signal: about {len(l3c) * 0.5 * 0.1:.0f}); level-2 reference: {int(((c.family == 'l2') & c.same_sign & (c.conf_ic_pred_big.abs() >= 0.05)).sum())} of {int((c.family == 'l2').sum())}")
    print("median max |Spearman corr| of a level-3 feature with the closest level-2 feature:", round(float(l3c.max_abs_corr_with_l2.median()), 3))
