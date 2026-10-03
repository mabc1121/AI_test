#!/usr/bin/env python3
"""export_models.py — turn the frozen Bybit bundles (research/models/H*.pkl) into the plain-array file the app loads
(trade/aitest_models.json.gz): gradient-boosting trees as arrays, the feature order, the training priors and the
label-free decision thresholds taken from the Bybit TRAINING distribution (nothing from Bitfinex).

  python export_models.py --bars <bybit bars_1m_v2.parquet> --models ../models --out ../../trade/aitest_models.json.gz
"""
import argparse
import gzip
import json
import os
import pickle
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "kit"))
sys.argv_backup = sys.argv


class GB:  # unpickling twins of the training wrappers
    def predict(self, X):
        return self.m.predict_proba(X)[:, self.pos] if self.pos is not None else np.zeros(len(X))


class LR:
    def _prep(self, X):
        return np.clip(np.nan_to_num((X - self.mu) / self.sd, nan=0.0, posinf=0.0, neginf=0.0), -10, 10)

    def predict(self, X):
        return np.full(len(X), self.const) if self.const is not None else self.m.predict_proba(self._prep(X))[:, self.pos]


import __main__  # noqa: E402
__main__.GB, __main__.LR = GB, LR

HORIZONS = (30, 60, 120, 240)
TRAIN_END = "2026-09-24"
GROUP = "ALL_gb"


def export_hgb(est):
    """sklearn HistGradientBoosting* -> {'baseline', 'trees': [[node,...],...]}; node = [feat, thr, miss_left, left, right, value]
    (feat == -1 marks a leaf). Prediction = baseline + sum of leaf values; classifier -> sigmoid."""
    base = np.asarray(est._baseline_prediction).ravel()
    assert base.size == 1, "binary / single-output models only"
    trees = []
    for stage in est._predictors:
        assert len(stage) == 1
        nodes = stage[0].nodes
        t = []
        for n in nodes:
            if n["is_leaf"]:
                t.append([-1, 0.0, 0, 0, 0, float(n["value"])])
            else:
                assert not n["is_categorical"]
                t.append([int(n["feature_idx"]), float(n["num_threshold"]), int(n["missing_go_to_left"]), int(n["left"]), int(n["right"]), 0.0])
        trees.append(t)
    return {"baseline": float(base[0]), "trees": trees, "n_features": int(est.n_features_in_)}


def predict_arrays(model, X):
    """reference implementation of the app's tree walker (numpy, row loop) used to verify the export"""
    out = np.full(len(X), model["baseline"])
    for t in model["trees"]:
        for i, x in enumerate(X):
            k = 0
            while True:
                f, thr, ml, l, r, v = t[k]
                if f < 0:
                    out[i] += v
                    break
                xv = x[f]
                k = (l if ml else r) if np.isnan(xv) else (l if xv <= thr else r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True, help="Bybit bars_1m_v2.parquet (training data, for feature order and thresholds)")
    ap.add_argument("--models", default=os.path.join(HERE, "..", "models"))
    ap.add_argument("--out", default=os.path.join(HERE, "..", "..", "trade", "aitest_models.json.gz"))
    ap.add_argument("--verify-bars", default=None, help="optional second bars file to verify the export on (e.g. Bitfinex)")
    a = ap.parse_args()
    sys.argv = ["x"]
    import research as R  # noqa: E402
    import config as C  # noqa: E402

    by = pd.read_parquet(a.bars)
    if by.index.tz is None:
        by.index = by.index.tz_localize("UTC")
    by = by[by.index < pd.Timestamp(TRAIN_END, tz="UTC")]
    f = R.build_features(by)
    P = [c for c in f.columns if c.startswith(("ret_", "lrv_", "rvr_", "rpos_", "hl15", "tod_", "dow", "mins_to"))]
    F = [c for c in f.columns if c.startswith(("timb_", "volz_", "nlarge_", "ofi_", "netadd_"))]
    B = [c for c in f.columns if c.startswith(("spread_", "bimb_", "micro_", "dimb", "ldtot", "dtot1_chg", "l10_"))]
    L2 = [c for c in f.columns if c.startswith("l2_")]
    ALL = P + F + B + L2
    sig = f["sigma1"].values
    quality = (by["n_rows"].values >= C.MIN_ROWS_PER_MIN) & np.isfinite(f["lrv_1440"].values) & np.isfinite(sig)
    X = f[ALL].values
    sig_all = ["l2_imb10", "l2_wimb10", "micro_end", "l2_ofi_15", "l2_timb_15", "l2_net_asym_15",
               "l2_flow_dep_60", "l2_slope_asym", "l2_reach_asym", "l2_tot_imb", "l2_big_imb", "l2_dep05_asym_d15"]
    l2_signals = [s_ for s_ in sig_all if s_ in f.columns and f[s_].notna().mean() > 0.5]   # the filter build_features applied in training
    out = {"feature_names": ALL, "l2_signals": l2_signals, "group": GROUP, "train_window": ["2026-07-01", "2026-09-23"], "source": "Bybit BTCUSDT perp 1-min bars",
           "barrier_k": float(C.BARRIER_K), "sigma_window_min": int(C.SIGMA_WINDOW_MIN), "min_rows_per_min": int(C.MIN_ROWS_PER_MIN),
           "funding_hours_utc": list(C.FUNDING_HOURS_UTC), "l2_weight_bps": float(C.L2_WEIGHT_BPS), "horizons": {}}
    Xv = None
    if a.verify_bars:
        bv = pd.read_parquet(a.verify_bars)
        if bv.index.tz is None:
            bv.index = bv.index.tz_localize("UTC")
        fv = R.build_features(bv)
        assert list(fv[ALL].columns) == ALL
        sig_v = [s_ for s_ in sig_all if s_ in fv.columns and fv[s_].notna().mean() > 0.5]
        assert sig_v == l2_signals, ("signal filter differs on the verify file", sig_v, l2_signals)
        Xv = fv[ALL].values[-3000:]
    for H in HORIZONS:
        m = pickle.load(open(os.path.join(a.models, f"H{H}.pkl"), "rb"))
        d, h, v = m["dir"][GROUP].m, m["hit"][GROUP].m, m["vol_hgb"]
        assert d.n_features_in_ == len(ALL) == h.n_features_in_ == v.n_features_in_, (d.n_features_in_, len(ALL))
        ex = {"dir": export_hgb(d), "hit": export_hgb(h), "vol": export_hgb(v)}
        # verification against sklearn on a sample of the training rows (and on the verify file if given)
        for name, est, kind in (("dir", d, "clf"), ("hit", h, "clf"), ("vol", v, "reg")):
            for XX in ([X[quality][-2000:]] + ([Xv] if Xv is not None else [])):
                ref = est.predict_proba(XX)[:, 1] if kind == "clf" else est.predict(XX)
                raw = predict_arrays(ex[name], XX)
                mine = 1 / (1 + np.exp(-raw)) if kind == "clf" else raw
                err = float(np.nanmax(np.abs(mine - ref)))
                assert err < 1e-8, (H, name, err)
        # label-free thresholds from the training distribution
        idx = np.where(quality)[0][:: C.TRAIN_STRIDE.get(H, 1)]
        p_hit = h.predict_proba(X[idx])[:, 1]
        score = d.predict_proba(X[idx])[:, 1] - m["prior_up"]
        qs = [0.5, 0.6, 2 / 3, 0.7, 0.8, 0.9, 0.95]
        out["horizons"][str(H)] = {
            "prior_up": float(m["prior_up"]), "prior_hit": float(m["prior_hit"]), "train_mean_yvol": float(m["train_mean_yvol"]),
            "n_train": int(m["n_train"]), "models": ex,
            "thresholds": {"p_hit_top_tercile": float(np.quantile(p_hit, 2 / 3)), "abs_score_q80": float(np.quantile(np.abs(score), 0.8)),
                           "p_hit_quantiles": {str(q): float(np.quantile(p_hit, q)) for q in qs},
                           "abs_score_quantiles": {str(q): float(np.quantile(np.abs(score), q)) for q in qs}}}
        print(f"H={H}: trees dir/hit/vol = {len(ex['dir']['trees'])}/{len(ex['hit']['trees'])}/{len(ex['vol']['trees'])}, "
              f"p_hit tercile {out['horizons'][str(H)]['thresholds']['p_hit_top_tercile']:.3f}, |score| q80 {out['horizons'][str(H)]['thresholds']['abs_score_q80']:.4f}, verified")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with gzip.open(a.out, "wt", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print("wrote", a.out, f"{os.path.getsize(a.out)/1e6:.2f} MB, {len(ALL)} features")


if __name__ == "__main__":
    main()
