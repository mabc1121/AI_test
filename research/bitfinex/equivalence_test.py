#!/usr/bin/env python3
"""
equivalence_test.py — does the app strategy (trade/trade.py) build the same minute bars, features and predictions as
the research pipeline did?  Replays the raw Bitfinex websocket captures through ScientificCore.ingest() with the same
session/checkpoint rules as bitfinex_replay.py and compares minute by minute with
  * the research bars   (bitfinex_replay.py -> research.py bars2)          --kit-bars
  * research features   (research.py build_features on those bars)
  * sklearn predictions (the frozen bundles in research/models on those features)

  python equivalence_test.py --raw <raw dir YYYY/MM/DD/HH-*.jsonl.gz> --kit-bars <bars_1m_v2.parquet> [--days ...] [--out dir]
"""
import argparse
import gzip
import importlib.util
import json
import math
import os
import pickle
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
try:
    import orjson
    loads = orjson.loads
except ImportError:
    loads = json.loads


def load_trade_module():
    spec = importlib.util.spec_from_file_location("aitest_trade", REPO / "trade" / "trade.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["aitest_trade"] = m
    spec.loader.exec_module(m)
    return m


def replay_into(core, root, days=None, progress=True):
    """bitfinex_replay.replay() with the Book replaced by core.ingest(); returns message counts"""
    files = sorted(Path(root).glob("*/*/*/*.jsonl.gz"))
    if days:
        files = [f for f in files if "-".join(re.search(r"(\d{4})/(\d{2})/(\d{2})/", str(f)).groups()) in days]
    starts = {}
    for f in files:
        with gzip.open(f, "rt") as fh:
            starts[f] = loads(fh.readline())["r"]
    files.sort(key=lambda f: starts[f])
    sess_of = {f: int(re.search(r"-(\d+)\.jsonl\.gz$", str(f)).group(1)) for f in files}
    first = {}
    for f in files:
        first[sess_of[f]] = min(first.get(sess_of[f], starts[f]), starts[f])
    order = sorted(first, key=lambda s: first[s])
    cutoff = {s: (first[order[i + 1]] if i + 1 < len(order) else math.inf) for i, s in enumerate(order)}
    chan = {}
    n = {"updates": 0, "snapshots": 0, "trades": 0, "bootstrapped": 0}
    book_loaded = False
    t0 = time.time()
    for f in files:
        cut = cutoff[sess_of[f]]
        with gzip.open(f, "rt") as fh:
            for line in fh:
                rec = loads(line)
                r = rec["r"]
                if r >= cut:
                    continue
                if "k" in rec:
                    k = rec["k"]
                    if k == "connect":
                        chan.clear()
                    elif k == "book" and "cp" in rec and not core.book.orders:
                        rows = [(int(o[0]), float(o[1]), float(o[2])) for o in rec["cp"]["orders"]]
                        core.ingest("l3_snapshot", r, rows)          # bootstrap as the replay did (book from the checkpoint)
                        for cid, name in rec["cp"].get("channels", {}).items():
                            chan[int(cid)] = name
                        n["bootstrapped"] += 1
                    continue
                m = loads(rec["m"])
                if isinstance(m, dict):
                    if m.get("event") == "subscribed":
                        chan[m["chanId"]] = m["channel"]
                    continue
                ch = chan.get(m[0])
                if ch is None:
                    continue
                ms = m[-1] if isinstance(m[-1], (int, float)) and m[-1] > 1e12 else r
                tag = m[1]
                if tag == "hb":
                    core._advance(int(ms) // 1000)                   # heartbeats advance the replay clock too
                    continue
                if ch == "book":
                    if isinstance(tag, list) and tag and isinstance(tag[0], list):
                        core.ingest("l3_snapshot", ms, tag); n["snapshots"] += 1
                    elif isinstance(tag, list):
                        core.ingest("l3_update", ms, tag); n["updates"] += 1
                    elif tag == "cs":
                        core._advance(int(ms) // 1000)
                elif ch == "trades":
                    if tag == "tu":
                        core.ingest("public_trade", ms, m[2]); n["trades"] += 1
                    else:
                        core._advance(int(ms) // 1000)
        if progress:
            print(f"  {f.relative_to(root)} ({time.time()-t0:.0f}s, {len(core.hist)} minutes)", flush=True)
    core._advance(core.cur_sec + 1)                                  # close the last open second
    if core.acc is not None:
        core._finish_minute()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--kit-bars", required=True)
    ap.add_argument("--days", nargs="*")
    ap.add_argument("--out", default=str(HERE / "equivalence_out"))
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)

    m = load_trade_module()
    core = m.ScientificCore(m.TradeConfig(save_history=0, write_prediction_log=0))
    core.history_mode, core.history_checked = "off", True
    rows = []
    orig = core._minute_closed

    def tap(backfill=False):
        orig(backfill)
        fv = getattr(core, "feature_values", {})
        rows.append({"t": int(core.hist[-1][m.FI["t"]]), "bar": list(core.hist[-1]), "f": dict(fv),
                     "p": {H: dict(v) for H, v in core.predictions.items()}})
    core._minute_closed = tap
    t0 = time.time()
    counts = replay_into(core, a.raw, a.days)
    print(f"replayed in {time.time()-t0:.0f}s: {counts}, {len(rows)} minutes")

    idx = pd.to_datetime([r["t"] for r in rows], unit="ms", utc=True)
    app_bars = pd.DataFrame([r["bar"] for r in rows], index=idx, columns=m.BAR_FIELDS)
    app_feat = pd.DataFrame([r["f"] for r in rows], index=idx)
    app_pred = pd.DataFrame([{f"{k}_{H}": v[k] for H, v in r["p"].items() for k in ("p_up", "p_hit", "vol_raw")} for r in rows], index=idx)
    app_bars = app_bars[~app_bars.index.duplicated(keep="last")]
    app_feat = app_feat[~app_feat.index.duplicated(keep="last")]
    app_pred = app_pred[~app_pred.index.duplicated(keep="last")]

    sys.path.insert(0, str(REPO / "research" / "kit")); sys.argv = ["x"]
    import research as R
    kb = pd.read_parquet(a.kit_bars)
    if kb.index.tz is None:
        kb.index = kb.index.tz_localize("UTC")
    kf = R.build_features(kb)
    common = kb.index.intersection(app_bars.index)
    print(f"minutes: app {len(app_bars)}, research {len(kb)}, common {len(common)}")

    def compare(A, B, cols, label, rtol=1e-7, atol=1e-9):
        res = []
        for c in cols:
            x = A.loc[common, c].to_numpy(float); y = B.loc[common, c].to_numpy(float)
            both_nan = np.isnan(x) & np.isnan(y)
            nan_mismatch = np.isnan(x) ^ np.isnan(y)
            d = np.where(both_nan | nan_mismatch, 0.0, np.abs(x - y))
            tol = atol + rtol * np.abs(np.where(np.isnan(y), 0, y))
            bad = (d > tol) | nan_mismatch
            res.append({"set": label, "column": c, "minutes": len(common), "mismatch_minutes": int(bad.sum()),
                        "nan_mismatch": int(nan_mismatch.sum()), "max_abs_diff": float(d.max()) if len(d) else 0.0})
        return pd.DataFrame(res)

    bar_cols = [c for c in m.BAR_FIELDS if c in kb.columns and c != "t"]
    rb = compare(app_bars, kb, bar_cols, "bars")
    models = m.load_models()
    feat_cols = list(models["feature_names"]) + ["sigma1"]
    rf = compare(app_feat, kf, feat_cols, "features")

    # predictions: sklearn on research features vs the app's own
    class GB:
        def predict(self, X):
            return self.m.predict_proba(X)[:, self.pos] if self.pos is not None else np.zeros(len(X))

    class LR:
        pass
    import __main__
    __main__.GB, __main__.LR = GB, LR
    X = kf.loc[common, models["feature_names"]].to_numpy(float)
    pr = []
    ref = pd.DataFrame(index=common)
    for H in (30, 60, 120, 240):
        b = pickle.load(open(REPO / "research" / "models" / f"H{H}.pkl", "rb"))
        ref[f"p_up_{H}"] = b["dir"]["ALL_gb"].m.predict_proba(X)[:, 1]
        ref[f"p_hit_{H}"] = b["hit"]["ALL_gb"].m.predict_proba(X)[:, 1]
        ref[f"vol_raw_{H}"] = b["vol_hgb"].predict(X)
    rp = compare(app_pred, ref, list(ref.columns), "predictions", rtol=1e-6, atol=1e-7)
    # how close are the predictions where they differ (feature differences propagate through the trees)
    agree = {}
    for H in (30, 60, 120, 240):
        d = (app_pred.loc[common, f"p_up_{H}"] - ref[f"p_up_{H}"]).abs()
        agree[H] = {"p_up_max_abs_diff": float(d.max()), "p_up_diff_gt_0.001": int((d > 1e-3).sum()),
                    "p_up_corr": float(np.corrcoef(app_pred.loc[common, f"p_up_{H}"].fillna(0), ref[f"p_up_{H}"])[0, 1])}
    allr = pd.concat([rb, rf, rp])
    allr.to_csv(out / "equivalence_columns.csv", index=False)
    pd.set_option("display.width", 250); pd.set_option("display.max_rows", 400)
    for label, r in (("BARS", rb), ("FEATURES", rf), ("PREDICTIONS", rp)):
        bad = r[r.mismatch_minutes > 0].sort_values("mismatch_minutes", ascending=False)
        print(f"\n=== {label}: {len(r)} columns, {len(bad)} with any mismatch; worst:")
        print(bad.head(25).to_string(index=False) if len(bad) else "  none")
    print("\nprediction agreement:", json.dumps(agree, indent=1))
    summary = {"minutes_common": len(common), "counts": counts, "bars_cols": len(rb), "bars_cols_mismatch": int((rb.mismatch_minutes > 0).sum()),
               "feature_cols": len(rf), "feature_cols_mismatch": int((rf.mismatch_minutes > 0).sum()),
               "prediction_cols_mismatch": int((rp.mismatch_minutes > 0).sum()), "agreement": agree}
    (out / "equivalence_summary.json").write_text(json.dumps(summary, indent=1))
    app_bars.to_parquet(out / "app_bars.parquet"); app_feat.to_parquet(out / "app_features.parquet"); app_pred.to_parquet(out / "app_predictions.parquet")
    print(json.dumps({k: v for k, v in summary.items() if k != "agreement"}))


if __name__ == "__main__":
    main()
