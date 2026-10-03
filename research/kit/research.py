#!/usr/bin/env python3
"""
research.py — v2. Horizon / cost / predictability screen for BTCUSDT perpetual 1-second L2 data,
plus the pre-registered "full L2 features" direction test.

Stages (each writes CSV/PNG files into OUT_DIR):

  python research.py sanity    # (v1) schema + column-mapping check on the first DATA_GLOB file
  python research.py bars      # (v1) 1-second rows -> 1-minute bars from DATA_GLOB (column-based files)
  python research.py bars2     # (v2) native Bybit files + top-10 level files -> bars_1m_v2.parquet
                               #      = every v1 column, bit-identical, + the l2_* columns; verifies against V1_BARS_FOR_CHECK
  python research.py horizon   # size of moves vs. cost per horizon, triple-barrier base rates, breakeven hit rates
  python research.py events    # signal decay (IC), big-move follow-through, time of day, funding, passive fills
  python research.py model     # walk-forward models; v2 adds per-fold AUC and the pre-registered criterion table
  python research.py report    # assemble OUT_DIR/REPORT.md
  python research.py all       # horizon -> events -> model -> report (bars/bars2 must have run before)

Only config.py is meant to be edited.
"""
import glob
import json
import math
import os
import re
import sys
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)

try:
    import config as C
except ImportError:
    sys.exit("config.py not found next to research.py")

REQUIRED = ["ts", "bid_px", "ask_px"]
EPS = 1e-9

# per-minute aggregation of the 1-second derived frame (v1, unchanged): (output column, input column, function)
AGG = [
    ("open", "mid", "first"), ("high", "mid", "max"), ("low", "mid", "min"), ("close", "mid", "last"),
    ("n_rows", "mid", "count"), ("rv", "r2", "sum"),
    ("t_high", "trade_high", "max"), ("t_low", "trade_low", "min"), ("t_last", "trade_last", "last"),
    ("buy_vol", "buy_vol", "sum"), ("sell_vol", "sell_vol", "sum"), ("n_trades", "n_trades", "sum"),
    ("n_large", "n_large", "sum"), ("buy_tail", "buy_tail", "sum"), ("sell_tail", "sell_tail", "sum"),
    ("ofi", "ofi", "sum"), ("ofi_tail", "ofi_tail", "sum"),
    ("added", "added_2bps", "sum"), ("removed", "removed_2bps", "sum"),
    ("spread_sum", "spread_bps", "sum"), ("spread_end", "spread_bps", "last"),
    ("bimb_sum", "book_imb", "sum"), ("bimb_end", "book_imb", "last"),
    ("micro_end", "micro_dev_bps", "last"),
]
for _lvl in ("05", "1", "3"):
    AGG += [(f"dimb{_lvl}_sum", f"dimb{_lvl}", "sum"), (f"dimb{_lvl}_end", f"dimb{_lvl}", "last"),
            (f"dtot{_lvl}_sum", f"dtot{_lvl}", "sum"), (f"dtot{_lvl}_end", f"dtot{_lvl}", "last")]
AGG += [("l10_imb_end", "l10_imb", "last"), ("l10_bslope_end", "l10_bslope", "last"),
        ("l10_aslope_end", "l10_aslope", "last")]
MERGE = {"first": "first", "last": "last", "max": "max", "min": "min", "sum": "sum", "count": "sum"}
SUM_COLS = [o for o, _, f in AGG if f in ("sum", "count")]

PAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
GRID, INK, MUTED, SURFACE = "#e1e0d9", "#0b0b0b", "#898781", "#fcfcfb"


# ====================================================================== utilities
def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def out_path(name):
    os.makedirs(C.OUT_DIR, exist_ok=True)
    return os.path.join(C.OUT_DIR, name)


def save_csv(df, name):
    p = out_path(name)
    df.to_csv(p, index=False)
    log(f"wrote {p}")
    return p


def md_table(df, fmt="{:.3f}", max_rows=80):
    df = df.head(max_rows)
    cols = list(df.columns)
    intcols = set()
    for c in cols:
        if pd.api.types.is_numeric_dtype(df[c]):
            v = pd.to_numeric(df[c], errors="coerce").dropna()
            if len(v) and np.all(np.isfinite(v)) and np.all(v == np.round(v)) and np.abs(v).max() < 1e12:
                intcols.add(c)
    lines = ["| " + " | ".join(str(c) for c in cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if isinstance(v, (float, np.floating, int, np.integer)):
                if pd.isna(v):
                    cells.append("")
                elif c in intcols:
                    cells.append(f"{int(round(float(v)))}")
                else:
                    cells.append(fmt.format(float(v)))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def unix_seconds(ts_index):
    """DatetimeIndex (any resolution, tz-aware or not) -> int64 Unix seconds."""
    t = pd.DatetimeIndex(ts_index)
    t = t.tz_convert("UTC").tz_localize(None) if t.tz is not None else t
    return ((t - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy().astype(np.int64)


def tstat(x):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 3 or x.std(ddof=1) == 0:
        return np.nan
    return x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))


def cost_bps(spread_bps):
    """Round-trip cost in bps for three execution styles, given the spread (bps) at entry."""
    f = C.FEES
    return {
        "taker": (2 * f["taker"] + 2 * f["slippage"]) * 1e4 + spread_bps,
        "mixed": (f["taker"] + f["maker"] + f["slippage"]) * 1e4 + spread_bps / 2,
        "maker": (2 * f["maker"]) * 1e4,
    }


def setup_plot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "axes.edgecolor": "#c3c2b7",
        "axes.labelcolor": INK, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.spines.top": False,
        "axes.spines.right": False, "font.size": 10, "font.family": "sans-serif",
    })
    return plt


# ====================================================================== loading (v1, column-based files)
def list_files():
    files = sorted(glob.glob(C.DATA_GLOB))
    if C.MAX_FILES:
        files = files[: C.MAX_FILES]
    if not files:
        sys.exit(f"no files match DATA_GLOB={C.DATA_GLOB!r}")
    return files


def rename_map(available):
    """source column -> canonical column, for the columns present in the file."""
    rn = {}
    for canon, src in C.COLS.items():
        if src is not None and src in available:
            rn[src] = canon
    if C.L10:
        for i in range(1, int(C.L10["n"]) + 1):
            for key in ("bid_dist", "bid_size", "ask_dist", "ask_size"):
                src = C.L10[key].format(i=i)
                if src in available:
                    rn[src] = f"{key}_{i}"
    return rn


def file_columns(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".parquet", ".pq"):
        import pyarrow.parquet as pq
        return list(pq.ParquetFile(path).schema_arrow.names)
    return list(pd.read_csv(path, nrows=0).columns)


def iter_chunks(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".parquet", ".pq"):
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(path)
        rn = rename_map(pf.schema_arrow.names)
        for batch in pf.iter_batches(batch_size=C.CHUNK_ROWS, columns=list(rn)):
            yield batch.to_pandas().rename(columns=rn)
    else:
        rn = rename_map(file_columns(path))
        for chunk in pd.read_csv(path, usecols=list(rn), chunksize=C.CHUNK_ROWS):
            yield chunk.rename(columns=rn)


def parse_ts(s):
    if pd.api.types.is_datetime64_any_dtype(s):
        t = pd.DatetimeIndex(s)
        return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")
    if C.TS_UNIT == "iso" or not pd.api.types.is_numeric_dtype(s):
        return pd.DatetimeIndex(pd.to_datetime(s, utc=True))
    unit = C.TS_UNIT
    if unit == "auto":
        v = float(pd.Series(s).dropna().iloc[0])
        unit = "s" if v < 1e11 else "ms" if v < 1e14 else "us" if v < 1e17 else "ns"
    if unit == "ns":
        return pd.DatetimeIndex(pd.to_datetime(s.astype("int64"), unit="ns", utc=True))
    return pd.DatetimeIndex(pd.to_datetime(s.astype("float64"), unit=unit, utc=True))


# ====================================================================== 1-second -> 1-minute (v1, unchanged values)
def derive_seconds(raw):
    """Canonical 1-second chunk -> (derived per-second frame, per-day quality frame, numeric source frame).
    The first two are exactly the v1 outputs; the third is aligned with the first and used by the v2 L2 features."""
    ts = parse_ts(raw["ts"])
    df = raw.drop(columns=["ts"])
    df.index = ts
    df = df.sort_index()
    day = df.index.floor("D")
    dup = df.index.duplicated(keep="last")
    dups_by_day = pd.Series(dup.astype(float), index=df.index).groupby(day).sum()
    if dup.any():
        df = df[~dup]
    num = df.apply(pd.to_numeric, errors="coerce").astype("float64")

    bid, ask = num["bid_px"], num["ask_px"]
    mid = (bid + ask) / 2.0
    d = pd.DataFrame(index=num.index)
    d["mid"] = mid
    d["spread_bps"] = (ask - bid) / mid * 1e4
    d["crossed"] = (bid >= ask).astype(float)
    if "bid_sz" in num and "ask_sz" in num:
        tot = (num["bid_sz"] + num["ask_sz"]).replace(0, np.nan)
        d["book_imb"] = (num["bid_sz"] - num["ask_sz"]) / tot
        micro = (bid * num["ask_sz"] + ask * num["bid_sz"]) / tot
        d["micro_dev_bps"] = (micro - mid) / mid * 1e4
    r = np.log(mid).diff()
    d["r2"] = r ** 2
    d["abs_r_bps"] = r.abs() * 1e4
    for c in ("buy_vol", "sell_vol", "n_trades", "n_large", "ofi", "added_2bps", "removed_2bps"):
        if c in num:
            d[c] = num[c].fillna(0.0)
    for c in ("trade_high", "trade_low"):
        if c in num:
            d[c] = num[c]
    if "trade_last" in num:
        d["trade_last"] = num["trade_last"].ffill()
    for lvl in ("05", "1", "3"):
        bcol, acol = f"depth_bid_{lvl}", f"depth_ask_{lvl}"
        if bcol in num and acol in num:
            tot = (num[bcol] + num[acol]).replace(0, np.nan)
            d[f"dimb{lvl}"] = (num[bcol] - num[acol]) / tot
            d[f"dtot{lvl}"] = tot
    n10 = int(C.L10["n"]) if C.L10 else 0
    bs = [f"bid_size_{i}" for i in range(1, n10 + 1) if f"bid_size_{i}" in num]
    as_ = [f"ask_size_{i}" for i in range(1, n10 + 1) if f"ask_size_{i}" in num]
    bd = [f"bid_dist_{i}" for i in range(1, n10 + 1) if f"bid_dist_{i}" in num]
    ad = [f"ask_dist_{i}" for i in range(1, n10 + 1) if f"ask_dist_{i}" in num]
    if bs and as_:
        bsum, asum = num[bs].sum(axis=1), num[as_].sum(axis=1)
        d["l10_imb"] = (bsum - asum) / (bsum + asum).replace(0, np.nan)
        if len(bd) == len(bs) and len(ad) == len(as_):
            d["l10_bslope"] = (num[bs].values * np.abs(num[bd].values)).sum(axis=1) / bsum.replace(0, np.nan)
            d["l10_aslope"] = (num[as_].values * np.abs(num[ad].values)).sum(axis=1) / asum.replace(0, np.nan)
    sec = d.index.second
    tail = (sec >= 50).astype(float)
    if "ofi" in d:
        d["ofi_tail"] = d["ofi"] * tail
    if "buy_vol" in d and "sell_vol" in d:
        d["buy_tail"] = d["buy_vol"] * tail
        d["sell_tail"] = d["sell_vol"] * tail
        d["zero_trade"] = ((d["buy_vol"] + d["sell_vol"]) == 0).astype(float)
    else:
        d["zero_trade"] = np.nan

    q = pd.DataFrame({"rows": np.ones(len(d)), "crossed": d["crossed"], "spread_sum": d["spread_bps"].fillna(0.0),
                      "zero_trade": d["zero_trade"], "max_abs_r": d["abs_r_bps"].fillna(0.0)}, index=d.index)
    qd = q.groupby(d.index.floor("D")).agg(rows=("rows", "sum"), crossed=("crossed", "sum"),
                                           spread_sum=("spread_sum", "sum"), zero_trade=("zero_trade", "sum"),
                                           max_abs_r=("max_abs_r", "max"))
    qd["dups"] = dups_by_day.reindex(qd.index).fillna(0.0)
    return d, qd, num


def finish_bars(bars):
    """v1 post-processing of the per-minute frame (gap filling, means, forward-fill of end-of-minute state)."""
    full = pd.date_range(bars.index.min(), bars.index.max(), freq="min", tz="UTC")
    bars = bars.reindex(full)
    bars.index.name = "ts"
    n_gap = int(bars["n_rows"].isna().sum())
    bars["n_rows"] = bars["n_rows"].fillna(0.0)
    bars["close"] = bars["close"].ffill()
    for c in ("open", "high", "low"):
        bars[c] = bars[c].fillna(bars["close"])
    for c in SUM_COLS:
        if c in bars:
            bars[c] = bars[c].fillna(0.0)
    n = bars["n_rows"].replace(0, np.nan)
    for base in ("spread", "bimb", "dimb05", "dtot05", "dimb1", "dtot1", "dimb3", "dtot3"):
        if f"{base}_sum" in bars:
            bars[f"{base}_mean"] = bars[f"{base}_sum"] / n
            bars = bars.drop(columns=[f"{base}_sum"])
    for c in [c for c in bars.columns if c.endswith("_end")] + ["t_last"]:
        if c in bars:
            bars[c] = bars[c].ffill()
    return bars, n_gap


def quality_table(quals):
    q = pd.concat(quals).groupby(level=0).agg(rows=("rows", "sum"), crossed=("crossed", "sum"),
                                              spread_sum=("spread_sum", "sum"), zero_trade=("zero_trade", "sum"),
                                              max_abs_r=("max_abs_r", "max"), dups=("dups", "sum"))
    q["missing_sec_pct"] = (1 - q["rows"] / 86400).clip(lower=0) * 100
    q["spread_mean_bps"] = q["spread_sum"] / q["rows"]
    q["zero_trade_pct"] = q["zero_trade"] / q["rows"] * 100
    q.index.name = "day"
    q = q.reset_index()
    q["day"] = pd.to_datetime(q["day"]).dt.strftime("%Y-%m-%d")
    return q[["day", "rows", "missing_sec_pct", "dups", "crossed", "spread_mean_bps", "zero_trade_pct", "max_abs_r"]]


def stage_sanity():
    files = list_files()
    log(f"{len(files)} file(s) match {C.DATA_GLOB!r}; first: {files[0]}; last: {files[-1]}")
    cols = file_columns(files[0])
    rn = rename_map(cols)
    print("\ncolumns in first file:")
    for c in cols:
        print(f"  {c:40s} -> {rn.get(c, '(not used)')}")
    missing_req = [k for k in REQUIRED if k not in rn.values()]
    missing_opt = [k for k, v in C.COLS.items() if v is not None and k not in rn.values() and k not in REQUIRED]
    if missing_req:
        sys.exit(f"\nREQUIRED canonical columns not mapped: {missing_req} -> fix COLS in config.py")
    print(f"\noptional canonical columns not found (tests using them are skipped): {missing_opt or 'none'}")
    raw = next(iter_chunks(files[0])).head(200_000)
    ts = parse_ts(raw["ts"])
    print(f"\nfirst chunk: {len(raw):,} rows, ts {ts[0]} .. {ts[-1]}  (unit={C.TS_UNIT})")
    dt = np.diff(ts.values.astype("datetime64[ns]").astype("int64")) / 1e9
    print(f"median step between rows: {np.median(dt):.3f} s ; rows with step > 1.5 s: {(dt > 1.5).mean()*100:.2f}%")
    d, qd, _ = derive_seconds(raw)
    print(f"mid: {d['mid'].min():.1f} .. {d['mid'].max():.1f} ; spread bps median {d['spread_bps'].median():.3f}, "
          f"p99 {d['spread_bps'].quantile(0.99):.3f} ; crossed book rows {int(d['crossed'].sum())}")
    print("\nsanity OK -> next: python research.py bars")


def stage_bars():
    files = list_files()
    parts, quals = [], []
    t0 = time.time()
    for path in files:
        for ci, raw in enumerate(iter_chunks(path)):
            missing = [c for c in REQUIRED if c not in raw.columns]
            if missing:
                sys.exit(f"{path}: required columns missing after mapping: {missing}")
            d, qd, _ = derive_seconds(raw)
            minute = d.index.floor("min")
            spec = {o: (i, f) for o, i, f in AGG if i in d.columns}
            parts.append(d.groupby(minute, sort=True).agg(**spec))
            quals.append(qd)
            log(f"{os.path.basename(path)} chunk {ci}: {len(d):,} s-rows -> {len(parts[-1]):,} minutes")
    allp = pd.concat(parts).sort_index(kind="mergesort")
    merge_spec = {o: MERGE[f] for o, _, f in AGG if o in allp.columns}
    bars, n_gap = finish_bars(allp.groupby(level=0).agg(merge_spec))
    bars.to_parquet(out_path("bars_1m.parquet"))
    log(f"bars: {len(bars):,} minutes {bars.index[0]} .. {bars.index[-1]} ; empty minutes filled: {n_gap} ; "
        f"{time.time()-t0:.0f}s")
    q = quality_table(quals)
    save_csv(q, "data_quality_by_day.csv")
    print(md_table(q.describe().T.reset_index().rename(columns={"index": "stat"}), "{:.2f}"))


# ====================================================================== v2: native files + level files -> bars_1m_v2
LVL_COLS = ([f"bid_dist_{i}" for i in range(1, 11)] + [f"bid_size_{i}" for i in range(1, 11)] +
            [f"ask_dist_{i}" for i in range(1, 11)] + [f"ask_size_{i}" for i in range(1, 11)])


def load_native_day(path):
    """Original Bybit 1-second file (index 'sec' = Unix seconds, start of second) -> frame with a 'ts' column,
    the original columns, and the three summed columns the v1 mapping expects (same formulas as the kit_input copy)."""
    d = pd.read_parquet(path)
    if "sec" not in d.columns:
        d = d.reset_index()
        if "sec" not in d.columns:
            d = d.rename(columns={d.columns[0]: "sec"})
    d = d.rename(columns={"sec": "ts"})
    for out, a, b in (("added_2bps", "add_b", "add_a"), ("removed_2bps", "rem_b", "rem_a"),
                      ("large_trades", "big_buy", "big_sell")):
        if a in d.columns and b in d.columns and out not in d.columns:
            d[out] = d[a].add(d[b], fill_value=0)
    return d


def load_levels(day, ts_index):
    """Top-10 level file for one day -> float64 array (len(ts_index), 40) aligned with ts_index by second,
    column order bid_dist_1..10, bid_size_1..10, ask_dist_1..10, ask_size_1..10. None if the file is missing."""
    p = C.V2_LVL_PATTERN.format(date=day)
    if not os.path.exists(p):
        return None
    if p.endswith(".npy"):
        arr = np.load(p).astype(np.float64)
        day_start = int(pd.Timestamp(day, tz="UTC").timestamp())
        sec = np.arange(day_start, day_start + arr.shape[0])
    else:
        df = pd.read_parquet(p)
        if "sec" not in df.columns:
            df = df.reset_index().rename(columns={df.index.name or "index": "sec"})
        sec = df["sec"].to_numpy().astype(np.int64)
        arr = df[LVL_COLS].to_numpy().astype(np.float64)
    if arr.shape[1] != 40:
        raise SystemExit(f"{p}: expected 40 columns, got {arr.shape[1]}")
    want = unix_seconds(ts_index)
    pos = np.searchsorted(sec, want)
    pos = np.clip(pos, 0, len(sec) - 1)
    ok = sec[pos] == want
    out = np.full((len(want), 40), np.nan)
    out[ok] = arr[pos[ok]]
    if (~ok).sum():
        log(f"  levels {day}: {(~ok).sum()} seconds without a level row")
    return out


def derive_l2(d, num, lv):
    """Per-second quantities for the L2 features (aligned with d.index). Original column names via the COLS mapping."""
    o = pd.DataFrame(index=d.index)
    if lv is not None:
        bd, bs = np.abs(lv[:, 0:10]), np.clip(lv[:, 10:20], 0, None)
        ad, asz = np.abs(lv[:, 20:30]), np.clip(lv[:, 30:40], 0, None)
        for k in (3, 5, 10):
            sb, sa = bs[:, :k].sum(1), asz[:, :k].sum(1)
            o[f"imb{k}"] = (sb - sa) / (sb + sa + EPS)
        wb, wa = np.exp(-bd / C.L2_WEIGHT_BPS), np.exp(-ad / C.L2_WEIGHT_BPS)
        swb, swa = (bs * wb).sum(1), (asz * wa).sum(1)
        o["wimb10"] = (swb - swa) / (swb + swa + EPS)
        sb10, sa10 = bs.sum(1), asz.sum(1)
        o["slope_b"] = (bs * bd).sum(1) / (sb10 + EPS)
        o["slope_a"] = (asz * ad).sum(1) / (sa10 + EPS)
        o["conc_b"] = bs[:, 0] / (sb10 + EPS)
        o["conc_a"] = asz[:, 0] / (sa10 + EPS)
    if "depth_bid_05" in num and "depth_bid_3" in num:
        o["near_b"] = num["depth_bid_05"] / (num["depth_bid_3"] + EPS)
        o["near_a"] = num["depth_ask_05"] / (num["depth_ask_3"] + EPS)
        o["bd05"], o["ad05"] = num["depth_bid_05"], num["depth_ask_05"]
    if "depth_bid_1" in num:
        o["bd1"], o["ad1"] = num["depth_bid_1"], num["depth_ask_1"]
    if "depth_bid_tot" in num and "depth_ask_tot" in num:
        o["tot_imb"] = (num["depth_bid_tot"] - num["depth_ask_tot"]) / (num["depth_bid_tot"] + num["depth_ask_tot"] + EPS)
    if "reach_bid" in num and "reach_ask" in num:
        o["reach_asym"] = (num["reach_bid"] - num["reach_ask"]) / (num["reach_bid"] + num["reach_ask"] + EPS)
    o["mid"] = d["mid"]
    if "micro_dev_bps" in d:
        o["micro"] = d["micro_dev_bps"]
        o["bimb"] = d["book_imb"]
    if "dimb05" in d:
        o["dimb05"] = d["dimb05"]
    for c in ("ofi", "buy_vol", "sell_vol"):
        if c in d:
            o[c] = d[c]
    for c in ("add_b", "rem_b", "add_a", "rem_a", "big_buy", "big_sell"):
        if c in num:
            o[c] = num[c].fillna(0.0)
    if "mid_hi" in num and "mid_lo" in num:
        o["mid_hi"], o["mid_lo"] = num["mid_hi"], num["mid_lo"]
    return o


def aggregate_l2(o, minute):
    """Per-minute L2 columns (all prefixed l2_): end-of-minute states, last-15-second means, values at fixed seconds
    (for sub-minute changes), window sums over the last 5/15/30 seconds, per-side minute sums, resiliency sums."""
    sec = o.index.second.values
    tmp, spec = {}, {}

    def put(name, values, fn):
        tmp[name] = values
        spec[name] = (name, fn)

    for c in ("imb3", "imb5", "imb10", "wimb10", "slope_b", "slope_a", "conc_b", "conc_a", "near_b", "near_a",
              "tot_imb", "reach_asym", "bd05", "ad05"):
        if c in o:
            put(f"l2_{c}_end", o[c].values, "last")
    for c in ("imb10", "wimb10", "micro"):
        if c in o:
            put(f"l2_{c}_m15", np.where(sec >= 45, o[c].values, np.nan), "mean")
    at = {"imb10": (44, 0), "wimb10": (44,), "micro": (54, 44, 29, 0), "bimb": (44, 0), "dimb05": (44,),
          "mid": (54, 44, 29), "bd05": (44,), "ad05": (44,)}
    for c, secs in at.items():
        if c in o:
            for s in secs:
                put(f"l2_{c}_at{s}", np.where(sec == s, o[c].values, np.nan), "last")
    for w in (5, 15, 30):
        m = sec >= 60 - w
        for c in ("ofi", "buy_vol", "sell_vol", "add_b", "rem_b", "add_a", "rem_a"):
            if c in o:
                put(f"l2_{c}_w{w}", np.where(m, o[c].values, 0.0), "sum")
    for c in ("add_b", "rem_b", "add_a", "rem_a", "big_buy", "big_sell"):
        if c in o:
            put(f"l2_{c}_sum", o[c].values, "sum")
    for c in ("bd1", "ad1"):
        if c in o:
            put(f"l2_{c}_mean", o[c].values, "mean")
    if "add_a" in o and "buy_vol" in o:
        hit_a, hit_b = o["buy_vol"].values > 0, o["sell_vol"].values > 0
        put("l2_resa_num", np.where(hit_a, o["add_a"].values, 0.0), "sum")
        put("l2_resa_den", np.where(hit_a, o["rem_a"].values, 0.0), "sum")
        put("l2_resb_num", np.where(hit_b, o["add_b"].values, 0.0), "sum")
        put("l2_resb_den", np.where(hit_b, o["rem_b"].values, 0.0), "sum")
    if "mid_hi" in o:
        put("l2_mid_hi", o["mid_hi"].values, "max")
        put("l2_mid_lo", o["mid_lo"].values, "min")
    return pd.DataFrame(tmp, index=o.index).groupby(minute, sort=True).agg(**spec)


def check_against_v1(bars):
    p = C.V1_BARS_FOR_CHECK
    if not p or not os.path.exists(p):
        log(f"no v1 bars file at {p!r}; skipping the identity check")
        return
    v1 = pd.read_parquet(p)
    if v1.index.tz is None:
        v1.index = v1.index.tz_localize("UTC")
    common = v1.index.intersection(bars.index)
    real = (v1.loc[common, "n_rows"].to_numpy() > 0) & (bars.loc[common, "n_rows"].to_numpy() > 0)
    common = common[real]                                   # ignore gap minutes that only one side filled
    cols = [c for c in v1.columns if c in bars.columns]
    worst = 0.0
    bad = []
    for c in cols:
        a, b = v1.loc[common, c].to_numpy(dtype=float), bars.loc[common, c].to_numpy(dtype=float)
        both_nan = np.isnan(a) & np.isnan(b)
        diff = np.where(both_nan, 0.0, np.abs(a - b))
        m = float(np.nanmax(diff)) if len(diff) else 0.0
        if not np.isfinite(m) or m > 1e-9:
            bad.append((c, m))
        worst = max(worst, m if np.isfinite(m) else np.inf)
    log(f"identity check vs {p}: {len(common):,} shared minutes, {len(cols)} shared columns, "
        f"max |diff| = {worst:.3e} -> {'PASS' if not bad else 'FAIL ' + str(bad[:5])}")


def stage_bars2():
    files = sorted(glob.glob(C.V2_RAW_GLOB))
    if C.MAX_FILES:
        files = files[: C.MAX_FILES]
    if not files:
        sys.exit(f"no files match V2_RAW_GLOB={C.V2_RAW_GLOB!r}")
    parts1, parts2, quals = [], [], []
    t0 = time.time()
    n_lvl = 0
    for path in files:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
        day = m.group(1) if m else None
        raw = load_native_day(path)
        raw = raw.rename(columns=rename_map(raw.columns))
        missing = [c for c in REQUIRED if c not in raw.columns]
        if missing:
            sys.exit(f"{path}: required columns missing after mapping: {missing} (check COLS in config.py)")
        d, qd, num = derive_seconds(raw)
        if day is None:
            day = f"{d.index[0]:%Y-%m-%d}"
        minute = d.index.floor("min")
        spec = {o: (i, f) for o, i, f in AGG if i in d.columns}
        parts1.append(d.groupby(minute, sort=True).agg(**spec))
        lv = load_levels(day, d.index)
        n_lvl += lv is not None
        parts2.append(aggregate_l2(derive_l2(d, num, lv), minute))
        quals.append(qd)
        log(f"{os.path.basename(path)}: {len(d):,} s-rows -> {len(parts1[-1]):,} minutes, "
            f"levels {'yes' if lv is not None else 'MISSING'} ({time.time()-t0:.0f}s)")
    allp = pd.concat(parts1).sort_index(kind="mergesort")
    merge_spec = {o: MERGE[f] for o, _, f in AGG if o in allp.columns}
    v1 = allp.groupby(level=0).agg(merge_spec)
    bars, n_gap = finish_bars(v1)
    l2 = pd.concat(parts2)
    l2 = l2[~l2.index.duplicated(keep="last")].reindex(bars.index)
    for c in [c for c in l2.columns if c.endswith("_end")]:
        l2[c] = l2[c].ffill()
    bars = bars.join(l2)
    check_against_v1(bars)
    bars.to_parquet(out_path("bars_1m_v2.parquet"))
    log(f"bars2: {len(bars):,} minutes {bars.index[0]} .. {bars.index[-1]} ; empty minutes filled: {n_gap} ; "
        f"level files found for {n_lvl}/{len(files)} days ; {len(l2.columns)} l2_* columns ; {time.time()-t0:.0f}s")
    q = quality_table(quals)
    save_csv(q, "data_quality_by_day.csv")
    print(md_table(q.describe().T.reset_index().rename(columns={"index": "stat"}), "{:.2f}"))
    print("\nbars2 done -> send out/bars_1m_v2.parquet and this console output")


def load_bars():
    cands = [getattr(C, "BARS_FILE", "bars_1m_v2.parquet"), "bars_1m_v2.parquet", "bars_1m.parquet"]
    for name in cands:
        p = out_path(name)
        if os.path.exists(p):
            b = pd.read_parquet(p)
            if b.index.tz is None:
                b.index = b.index.tz_localize("UTC")
            log(f"bars: {name} ({len(b):,} minutes, {sum(c.startswith('l2_') for c in b.columns)} l2_* columns)")
            return b
    sys.exit("no bars file found: run `python research.py bars2` (or `bars`) first")


# ====================================================================== features
LONG_WIN = int(os.environ.get("LONG_WIN", 1440))          # history window of the slow normalisers (minutes)
LONG_MIN = int(os.environ.get("LONG_MIN", 240))           # minimum history before they are defined


def build_features(b):
    f = pd.DataFrame(index=b.index)
    idx = b.index
    c = b["close"]
    lc = np.log(c)
    for n in (1, 5, 15, 60, 240):
        f[f"ret_{n}"] = (lc - lc.shift(n)) * 1e4
    rv = b["rv"].fillna(0.0)
    for n in (5, 15, 60, 240, 1440):
        if n > LONG_WIN:
            continue
        f[f"lrv_{n}"] = np.log(rv.rolling(n, min_periods=max(2, n // 2)).sum() / n + 1e-12)
    f["rvr_5_60"] = f["lrv_5"] - f["lrv_60"]
    if "lrv_1440" in f:
        f["rvr_60_1440"] = f["lrv_60"] - f["lrv_1440"]
    sig = np.sqrt(rv.rolling(C.SIGMA_WINDOW_MIN, min_periods=60).mean()) * 1e4
    sig_alt = f["ret_1"].rolling(C.SIGMA_WINDOW_MIN, min_periods=60).std()
    sig = sig.where(sig > 0, sig_alt).clip(lower=0.5)
    f["sigma1"] = sig
    for n in (60, 240):
        hi, lo = b["high"].rolling(n).max(), b["low"].rolling(n).min()
        f[f"rpos_{n}"] = (c - lo) / (hi - lo).replace(0, np.nan)
    f["hl15_sig"] = (np.log(b["high"].rolling(15).max() / b["low"].rolling(15).min()) * 1e4) / (sig * math.sqrt(15))
    mins = idx.hour * 60 + idx.minute
    f["tod_sin"] = np.sin(2 * np.pi * mins / 1440)
    f["tod_cos"] = np.cos(2 * np.pi * mins / 1440)
    f["dow"] = idx.dayofweek
    f["mins_to_funding"] = np.min(np.stack([(h * 60 - mins) % 1440 for h in C.FUNDING_HOURS_UTC]), axis=0)

    if "buy_vol" in b and "sell_vol" in b:
        buy, sell = b["buy_vol"].fillna(0.0), b["sell_vol"].fillna(0.0)
        vol = buy + sell
        for n in (1, 5, 15, 60):
            bs, ss = buy.rolling(n).sum(), sell.rolling(n).sum()
            f[f"timb_{n}"] = (bs - ss) / (bs + ss).replace(0, np.nan)
        if "buy_tail" in b:
            tt = b["buy_tail"] + b["sell_tail"]
            f["timb_tail"] = (b["buy_tail"] - b["sell_tail"]) / tt.replace(0, np.nan)
        vma = vol.rolling(LONG_WIN, min_periods=LONG_MIN).mean()
        for n in (1, 5, 60):
            f[f"volz_{n}"] = np.log((vol.rolling(n).mean() + 1e-9) / (vma + 1e-9))
    if "n_large" in b:
        nl = b["n_large"].fillna(0.0)
        nlma = nl.rolling(LONG_WIN, min_periods=LONG_MIN).mean()
        f["nlarge_5"] = np.log((nl.rolling(5).mean() + 1e-3) / (nlma + 1e-3))
        f["nlarge_60"] = np.log((nl.rolling(60).mean() + 1e-3) / (nlma + 1e-3))
    ofi_sd = None
    if "ofi" in b:
        ofi = b["ofi"].fillna(0.0)
        ofi_sd = ofi.rolling(LONG_WIN, min_periods=LONG_MIN).std().replace(0, np.nan)
        for n in (1, 5, 15):
            f[f"ofi_{n}"] = ofi.rolling(n).sum() / (ofi_sd * math.sqrt(n))
        if "ofi_tail" in b:
            f["ofi_tail"] = b["ofi_tail"] / ofi_sd
    if "added" in b and "removed" in b:
        net = b["added"].fillna(0.0) - b["removed"].fillna(0.0)
        sd = net.rolling(LONG_WIN, min_periods=LONG_MIN).std().replace(0, np.nan)
        f["netadd_1"] = net / sd
        f["netadd_5"] = net.rolling(5).sum() / (sd * math.sqrt(5))

    f["spread_end"] = b["spread_end"]
    if "spread_mean" in b:
        f["spread_m5"] = b["spread_mean"].rolling(5).mean()
    if "bimb_end" in b:
        f["bimb_end"] = b["bimb_end"]
        f["bimb_m5"] = b["bimb_mean"].rolling(5).mean()
        f["bimb_m15"] = b["bimb_mean"].rolling(15).mean()
    if "micro_end" in b:
        f["micro_end"] = b["micro_end"]
    for lvl in ("05", "1", "3"):
        if f"dimb{lvl}_end" in b:
            f[f"dimb{lvl}_end"] = b[f"dimb{lvl}_end"]
            f[f"dimb{lvl}_m5"] = b[f"dimb{lvl}_mean"].rolling(5).mean()
            f[f"ldtot{lvl}_end"] = np.log(b[f"dtot{lvl}_end"] + 1e-9)
    if "dtot1_end" in b:
        f["dtot1_chg"] = np.log((b["dtot1_end"] + 1e-9) / (b["dtot1_mean"].rolling(60).mean() + 1e-9))
    for cname in ("l10_imb_end", "l10_bslope_end", "l10_aslope_end"):
        if cname in b:
            f[cname] = b[cname]

    # ------------------------------------------------------------------ v2: full L2 features (all prefixed l2_)
    if any(col.startswith("l2_") for col in b.columns):
        def g(col):
            return b[col] if col in b.columns else pd.Series(np.nan, index=b.index)
        # 1) multi-level imbalance across the top 10 levels
        for k in (3, 5, 10):
            f[f"l2_imb{k}"] = g(f"l2_imb{k}_end")
        f["l2_wimb10"] = g("l2_wimb10_end")
        f["l2_imb10_m15"], f["l2_wimb10_m15"] = g("l2_imb10_m15"), g("l2_wimb10_m15")
        f["l2_imb10_d15"] = g("l2_imb10_end") - g("l2_imb10_at44")
        f["l2_imb10_d60"] = g("l2_imb10_end") - g("l2_imb10_at0")
        f["l2_wimb10_d15"] = g("l2_wimb10_end") - g("l2_wimb10_at44")
        # 2) book slope / shape / liquidity concentration
        f["l2_slope_b"], f["l2_slope_a"] = g("l2_slope_b_end"), g("l2_slope_a_end")
        f["l2_slope_asym"] = np.log((g("l2_slope_a_end") + 1e-3) / (g("l2_slope_b_end") + 1e-3))
        f["l2_conc_b"], f["l2_conc_a"] = g("l2_conc_b_end"), g("l2_conc_a_end")
        f["l2_conc_asym"] = g("l2_conc_b_end") - g("l2_conc_a_end")
        f["l2_near_b"], f["l2_near_a"] = g("l2_near_b_end"), g("l2_near_a_end")
        f["l2_near_asym"] = g("l2_near_b_end") - g("l2_near_a_end")
        f["l2_tot_imb"], f["l2_reach_asym"] = g("l2_tot_imb_end"), g("l2_reach_asym_end")
        # 3) microprice and its sub-minute changes; sub-minute mid returns
        f["l2_micro_m15"] = g("l2_micro_m15")
        for w, s in ((5, 54), (15, 44), (30, 29), (60, 0)):
            f[f"l2_micro_d{w}"] = g("micro_end") - g(f"l2_micro_at{s}")
        for w, s in ((5, 54), (15, 44), (30, 29)):
            f[f"l2_mret_{w}"] = np.log(c / g(f"l2_mid_at{s}")) * 1e4
        # 5) changes in imbalance and OFI over 5/15/30/60 s; sub-minute trade imbalance
        f["l2_bimb_d15"] = g("bimb_end") - g("l2_bimb_at44")
        f["l2_bimb_d60"] = g("bimb_end") - g("l2_bimb_at0")
        f["l2_dimb05_d15"] = g("dimb05_end") - g("l2_dimb05_at44")
        if ofi_sd is not None:
            for w in (5, 15, 30):
                f[f"l2_ofi_{w}"] = g(f"l2_ofi_w{w}") / (ofi_sd * math.sqrt(w / 60))
        for w in (5, 15, 30):
            bw_, sw_ = g(f"l2_buy_vol_w{w}"), g(f"l2_sell_vol_w{w}")
            f[f"l2_timb_{w}"] = (bw_ - sw_) / (bw_ + sw_).replace(0, np.nan)
        # 4) liquidity pulling / replenishment: net adds per side relative to depth, touch depletion, resiliency
        bd1m, ad1m = g("l2_bd1_mean"), g("l2_ad1_mean")
        f["l2_net_b_15"] = np.arcsinh((g("l2_add_b_w15") - g("l2_rem_b_w15")) / (bd1m + EPS))
        f["l2_net_a_15"] = np.arcsinh((g("l2_add_a_w15") - g("l2_rem_a_w15")) / (ad1m + EPS))
        f["l2_net_asym_15"] = f["l2_net_b_15"] - f["l2_net_a_15"]
        f["l2_net_b_60"] = np.arcsinh((g("l2_add_b_sum") - g("l2_rem_b_sum")) / (bd1m + EPS))
        f["l2_net_a_60"] = np.arcsinh((g("l2_add_a_sum") - g("l2_rem_a_sum")) / (ad1m + EPS))
        f["l2_net_asym_60"] = f["l2_net_b_60"] - f["l2_net_a_60"]
        f["l2_dep05_b_d15"] = np.log((g("l2_bd05_end") + 1e-3) / (g("l2_bd05_at44") + 1e-3))
        f["l2_dep05_a_d15"] = np.log((g("l2_ad05_end") + 1e-3) / (g("l2_ad05_at44") + 1e-3))
        f["l2_dep05_asym_d15"] = f["l2_dep05_b_d15"] - f["l2_dep05_a_d15"]
        f["l2_resil_b"] = np.log((g("l2_resb_num") + 0.01) / (g("l2_resb_den") + 0.01))
        f["l2_resil_a"] = np.log((g("l2_resa_num") + 0.01) / (g("l2_resa_den") + 0.01))
        f["l2_resil_asym"] = f["l2_resil_b"] - f["l2_resil_a"]
        # 6) aggressive flow relative to available depth; per-side large trades
        f["l2_flow_dep_60"] = np.arcsinh((g("buy_vol") - g("sell_vol")) / (0.5 * (bd1m + ad1m) + EPS))
        f["l2_buy_dep_15"] = np.arcsinh(g("l2_buy_vol_w15") / (ad1m + EPS))
        f["l2_sell_dep_15"] = np.arcsinh(g("l2_sell_vol_w15") / (bd1m + EPS))
        f["l2_flow_dep_15"] = f["l2_buy_dep_15"] - f["l2_sell_dep_15"]
        bb, bsell = g("l2_big_buy_sum").fillna(0.0), g("l2_big_sell_sum").fillna(0.0)
        f["l2_big_imb"] = (bb - bsell) / (bb + bsell + 1e-6)
        bb15, bs15 = bb.rolling(15).sum(), bsell.rolling(15).sum()
        f["l2_big_imb_15m"] = (bb15 - bs15) / (bb15 + bs15 + 1e-6)
        # 7) agreement / extremes across sign-aligned signals (positive = bullish), trailing z-scores
        signals = ["l2_imb10", "l2_wimb10", "micro_end", "l2_ofi_15", "l2_timb_15", "l2_net_asym_15",
                   "l2_flow_dep_60", "l2_slope_asym", "l2_reach_asym", "l2_tot_imb", "l2_big_imb", "l2_dep05_asym_d15"]
        signals = [s for s in signals if s in f.columns and f[s].notna().mean() > 0.5]
        zs = []
        for s in signals:
            x = f[s]
            mu, sd = x.rolling(LONG_WIN, min_periods=LONG_MIN).mean(), x.rolling(LONG_WIN, min_periods=LONG_MIN).std().replace(0, np.nan)
            zs.append(((x - mu) / sd).clip(-5, 5))
        if zs:
            Z = pd.concat(zs, axis=1)
            f["l2_agree"] = (np.sign(Z) * (Z.abs() > 1)).sum(axis=1, min_count=1)
            f["l2_zsum"] = Z.clip(-3, 3).sum(axis=1, min_count=1)
            f["l2_n_extreme"] = (Z.abs() > 2).sum(axis=1, min_count=1)
            f["l2_n_signals"] = Z.notna().sum(axis=1)
        f.drop(columns=[c_ for c_ in f.columns if c_ == "l2_n_signals"], inplace=True)
    return f


def feature_groups(f):
    P = [c for c in f.columns if c.startswith(("ret_", "lrv_", "rvr_", "rpos_", "hl15", "tod_", "dow", "mins_to"))]
    F = [c for c in f.columns if c.startswith(("timb_", "volz_", "nlarge_", "ofi_", "netadd_"))]
    B = [c for c in f.columns if c.startswith(("spread_", "bimb_", "micro_", "dimb", "ldtot", "dtot1_chg", "l10_"))]
    L2 = [c for c in f.columns if c.startswith("l2_")]
    groups = {"P": P}
    if L2:
        groups["L2"] = L2
        groups["PL2"] = P + L2
        groups["ALL"] = P + F + B + L2
    else:
        if F:
            groups["F"] = F
        if B:
            groups["B"] = B
        if F or B:
            groups["PFB"] = P + F + B
    return groups


# ====================================================================== labels
def triple_barrier(close, high, low, sigma1, H, k):
    """Entry at close[t]; take-profit = stop-loss = k*sigma1*sqrt(H) bps; path = minutes t+1..t+H."""
    n = len(close)
    bw = k * sigma1 * math.sqrt(H)
    up, dn = close * np.exp(bw / 1e4), close * np.exp(-bw / 1e4)
    BIG = 10 ** 6
    t_up, t_dn = np.full(n, BIG), np.full(n, BIG)
    for j in range(1, H + 1):
        hi, lo = np.full(n, np.nan), np.full(n, np.nan)
        hi[: n - j], lo[: n - j] = high[j:], low[j:]
        m = (hi >= up) & (t_up == BIG)
        t_up[m] = j
        m = (lo <= dn) & (t_dn == BIG)
        t_dn[m] = j
    fwd = np.full(n, np.nan)
    fwd[: n - H] = (np.log(close[H:]) - np.log(close[: n - H])) * 1e4
    valid = (np.arange(n) < n - H) & np.isfinite(bw) & np.isfinite(fwd)
    hu, hd = t_up < BIG, t_dn < BIG
    label = np.zeros(n)
    label[hu & (~hd | (t_up < t_dn))] = 1
    label[hd & (~hu | (t_dn < t_up))] = -1
    tie = hu & hd & (t_up == t_dn)
    label[tie] = -1
    t_exit = np.minimum(np.minimum(t_up, t_dn), H).astype(float)
    g_long = np.where(tie, -bw, np.where(label == 1, bw, np.where(label == -1, -bw, fwd)))
    g_short = np.where(tie, -bw, np.where(label == -1, bw, np.where(label == 1, -bw, -fwd)))
    for arr in (label, t_exit, g_long, g_short, fwd):
        arr[~valid] = np.nan
    return {"label": label, "bw": bw, "tie": tie.astype(float), "t_exit": t_exit, "fwd": fwd,
            "g_long": g_long, "g_short": g_short}


def fwd_return(close, H):
    n = len(close)
    out = np.full(n, np.nan)
    out[: n - H] = (np.log(close[H:]) - np.log(close[: n - H])) * 1e4
    return out


def fwd_rolling_min(x, H):
    s = pd.Series(x)
    return s[::-1].rolling(H, min_periods=1).min()[::-1].shift(-1).values


# ====================================================================== stage: horizon
def stage_horizon():
    b = load_bars()
    f = build_features(b)
    sig = f["sigma1"].values
    close, high, low = b["close"].values, b["high"].values, b["low"].values
    good = b["n_rows"].values >= C.MIN_ROWS_PER_MIN
    spread_med = float(np.nanmedian(b["spread_end"].values))
    costs = cost_bps(spread_med)
    pd.DataFrame([{"spread_median_bps": spread_med, **{f"cost_{k}_bps": v for k, v in costs.items()},
                   **{f"fee_{k}": v for k, v in C.FEES.items()}}]).to_csv(out_path("costs.csv"), index=False)
    log(f"median spread {spread_med:.3f} bps ; round-trip cost bps: " +
        ", ".join(f"{k}={v:.2f}" for k, v in costs.items()))
    rows, brows = [], []
    for H in C.HORIZONS:
        fwd = fwd_return(close, H)
        ok = np.isfinite(fwd) & good & np.isfinite(sig)
        am = np.abs(fwd[ok])
        s_ok = sig[ok] * math.sqrt(H)
        terc = np.nanpercentile(s_ok, [33.3, 66.7])
        r = {"H_min": H, "n": int(ok.sum()), "sigma_H_med_bps": float(np.median(s_ok)),
             "absmove_med_bps": float(np.median(am)), "absmove_mean_bps": float(am.mean()),
             "absmove_p75_bps": float(np.percentile(am, 75)),
             "absmove_med_lowvol": float(np.median(am[s_ok <= terc[0]])),
             "absmove_med_highvol": float(np.median(am[s_ok > terc[1]]))}
        for name, cst in costs.items():
            r[f"frac_gt_{name}"] = float((am > cst).mean())
            r[f"frac_gt_2x_{name}"] = float((am > 2 * cst).mean())
        r["room_taker_bps"] = r["absmove_med_bps"] - costs["taker"]
        r["room_maker_bps"] = r["absmove_med_bps"] - costs["maker"]
        rows.append(r)
        for k in C.K_LIST:
            lab = triple_barrier(close, high, low, sig, H, k)
            okk = np.isfinite(lab["label"]) & good
            L, bw = lab["label"][okk], lab["bw"][okk]
            bmed = float(np.median(bw))
            br = {"H_min": H, "k": k, "n": int(okk.sum()), "barrier_med_bps": bmed,
                  "p_up": float((L == 1).mean()), "p_down": float((L == -1).mean()), "p_flat": float((L == 0).mean()),
                  "tie_frac": float(lab["tie"][okk].mean()), "mean_exit_min": float(np.nanmean(lab["t_exit"][okk])),
                  "random_long_gross_bps": float(np.nanmean(lab["g_long"][okk]))}
            for name, cst in costs.items():
                br[f"breakeven_hit_{name}"] = 0.5 + cst / (2 * bmed)
            brows.append(br)
        log(f"H={H}: median |move| {r['absmove_med_bps']:.1f} bps, P(|move|>taker cost) {r['frac_gt_taker']:.2f}")
    room, base = pd.DataFrame(rows), pd.DataFrame(brows)
    save_csv(room, "horizon_room.csv")
    save_csv(base, "barrier_base_rates.csv")
    plt = setup_plot()
    fig, ax = plt.subplots(figsize=(7, 4))
    x = np.arange(len(room))
    ax.set_axisbelow(True)
    ax.bar(x, room["absmove_med_bps"], width=0.6, color=PAL[0], label="median |move|")
    for i, (name, ls) in enumerate([("taker", "-"), ("mixed", "--"), ("maker", ":")]):
        ax.axhline(costs[name], color=PAL[i + 1], lw=2, ls=ls, label=f"{name} round-trip cost")
    ax.set_xticks(x)
    ax.set_xticklabels([f"{h}m" for h in room["H_min"]])
    ax.set_ylabel("bps")
    ax.set_title("Median absolute move per horizon vs. round-trip cost")
    ax.legend(frameon=False, loc="upper left")
    fig.tight_layout()
    fig.savefig(out_path("horizon_room.png"), dpi=130)
    plt.close(fig)
    print(md_table(room, "{:.2f}"))
    print()
    print(md_table(base, "{:.3f}"))


# ====================================================================== stage: events
def ic_stats(x, y, H, week):
    """Spearman IC on non-overlapping subsamples (every H-th minute, several offsets); t uses n/H observations;
    share_weeks_pos = share of 7-day blocks whose own IC is positive (stability only)."""
    ok = np.isfinite(x) & np.isfinite(y)
    xs, ys, ws = x[ok], y[ok], week[ok]
    n = len(xs)
    if n < 10 * H + 100:
        return np.nan, np.nan, n, np.nan
    ics = []
    for o in range(0, H, max(1, H // 8)):
        xi, yi = xs[o::H], ys[o::H]
        if len(xi) > 50 and np.std(xi) > 0 and np.std(yi) > 0:
            ics.append(pd.Series(xi).corr(pd.Series(yi), method="spearman"))
    if not ics:
        return np.nan, np.nan, n, np.nan
    ic = float(np.nanmean(ics))
    n_eff = n / H
    wk = []
    for w in np.unique(ws):
        m = ws == w
        if m.sum() >= 5 * H + 50:
            wk.append(pd.Series(xs[m]).corr(pd.Series(ys[m]), method="spearman"))
    wk = [v for v in wk if np.isfinite(v)]
    return ic, ic * math.sqrt(n_eff), int(n_eff), (float(np.mean(np.array(wk) > 0)) if wk else np.nan)


def stage_events():
    b = load_bars()
    f = build_features(b)
    idx = b.index
    day = idx.floor("D")
    close, high, low = b["close"].values, b["high"].values, b["low"].values
    sig = f["sigma1"].values
    good = (b["n_rows"].values >= C.MIN_ROWS_PER_MIN) & np.isfinite(f["lrv_1440" if "lrv_1440" in f else "lrv_240"].values)
    hz = [1] + [h for h in C.HORIZONS if h != 1]
    fwd = {H: fwd_return(close, H) for H in hz}

    cands = ["ret_1", "ret_5", "ret_15", "ret_60", "ret_240", "timb_1", "timb_5", "timb_15", "timb_60", "timb_tail",
             "ofi_1", "ofi_5", "ofi_15", "ofi_tail", "netadd_1", "netadd_5", "bimb_end", "bimb_m5", "micro_end",
             "dimb05_end", "dimb1_end", "dimb3_end", "dimb1_m5", "l10_imb_end", "rpos_60", "rpos_240",
             "l2_imb3", "l2_imb10", "l2_wimb10", "l2_imb10_m15", "l2_imb10_d15", "l2_imb10_d60", "l2_micro_m15",
             "l2_micro_d15", "l2_micro_d60", "l2_mret_5", "l2_mret_15", "l2_mret_30", "l2_ofi_5", "l2_ofi_15",
             "l2_ofi_30", "l2_timb_5", "l2_timb_15", "l2_timb_30", "l2_net_asym_15", "l2_net_asym_60",
             "l2_resil_asym", "l2_flow_dep_15", "l2_flow_dep_60", "l2_slope_asym", "l2_conc_asym", "l2_near_asym",
             "l2_reach_asym", "l2_tot_imb", "l2_big_imb", "l2_big_imb_15m", "l2_dep05_asym_d15", "l2_bimb_d15",
             "l2_zsum", "l2_agree"]
    cands = [c for c in cands if c in f.columns]
    week = ((idx - idx[0]).days // 7).values
    rows = []
    for s in cands:
        x = np.where(good, f[s].values, np.nan)
        for H in hz:
            ic, t, n_eff, share_pos = ic_stats(x, fwd[H], H, week)
            rows.append({"signal": s, "H_min": H, "ic_mean": ic, "ic_t": t, "n_nonoverlap": n_eff,
                         "share_weeks_pos": share_pos})
        log(f"IC {s}: " + " ".join(f"{H}m={r['ic_mean']:+.3f}" for H, r in zip(hz, rows[-len(hz):])))
    ic = pd.DataFrame(rows)
    save_csv(ic, "ic_decay.csv")
    piv = ic.pivot(index="signal", columns="H_min", values="ic_mean").reindex(cands)
    save_csv(piv.reset_index(), "ic_decay_matrix.csv")
    try:
        plt = setup_plot()
        from matplotlib.colors import LinearSegmentedColormap
        cmap = LinearSegmentedColormap.from_list("div", ["#2a78d6", "#f0efec", "#e34948"])
        v = np.nanmax(np.abs(piv.values)) or 0.01
        fig, ax = plt.subplots(figsize=(7.5, 0.3 * len(piv) + 1.5))
        im = ax.imshow(piv.values, cmap=cmap, vmin=-v, vmax=v, aspect="auto")
        ax.set_xticks(range(len(piv.columns)))
        ax.set_xticklabels([f"{h}m" for h in piv.columns])
        ax.set_yticks(range(len(piv)))
        ax.set_yticklabels(piv.index, fontsize=8)
        ax.grid(False)
        for i in range(piv.shape[0]):
            for j in range(piv.shape[1]):
                val = piv.values[i, j]
                if np.isfinite(val):
                    ax.text(j, i, f"{val:+.3f}", ha="center", va="center", fontsize=7, color=INK)
        fig.colorbar(im, ax=ax, fraction=0.03, label="Spearman IC (non-overlapping subsamples)")
        ax.set_title("Signal -> forward return: information coefficient by horizon")
        fig.tight_layout()
        fig.savefig(out_path("ic_decay.png"), dpi=130)
        plt.close(fig)
    except Exception as e:
        log(f"ic plot skipped: {e}")

    r15 = f["ret_15"].values
    thr = 2.0 * sig * math.sqrt(15)
    ev = np.where(good & np.isfinite(r15) & (np.abs(r15) > thr))[0]
    keep, last = [], -10 ** 9
    for i in ev:
        if i - last >= 240:
            keep.append(i)
            last = i
    keep = np.array(keep, dtype=int)
    brows = []
    for subset, name in ((keep, "all"), (keep[r15[keep] > 0] if len(keep) else keep, "up_moves"),
                         (keep[r15[keep] < 0] if len(keep) else keep, "down_moves")):
        for H in (15, 60, 240):
            if len(subset) == 0:
                continue
            x = np.sign(r15[subset]) * fwd[H][subset]
            x = x[np.isfinite(x)]
            brows.append({"events": name, "n": len(x), "H_min": H, "mean_follow_bps": float(x.mean()) if len(x) else np.nan,
                          "t": tstat(x), "share_continuing": float((x > 0).mean()) if len(x) else np.nan})
    save_csv(pd.DataFrame(brows), "bigmove_followthrough.csv")

    vol = (b["buy_vol"] + b["sell_vol"]).values if "buy_vol" in b else np.full(len(b), np.nan)
    hod = pd.DataFrame({"hour_utc": idx.hour, "abs_ret60_bps": np.abs(fwd[60]), "ret60_bps": fwd[60],
                        "spread_bps": b["spread_end"].values, "vol_per_min": vol, "sigma1_bps": sig})
    hod = hod[good].groupby("hour_utc").agg(abs_ret60_bps=("abs_ret60_bps", "mean"), ret60_bps=("ret60_bps", "mean"),
                                            spread_bps=("spread_bps", "mean"), vol_per_min=("vol_per_min", "mean"),
                                            sigma1_bps=("sigma1_bps", "mean"), n=("ret60_bps", "size")).reset_index()
    save_csv(hod, "hour_of_day.csv")

    fmask = np.isin(idx.hour, C.FUNDING_HOURS_UTC) & (idx.minute == 0) & good
    before, after = f["ret_60"].values[fmask], fwd[60][fmask]
    save_csv(pd.DataFrame([{"window": "60min_before_funding", "n": int(np.isfinite(before).sum()),
                            "mean_ret_bps": float(np.nanmean(before)), "t": tstat(before)},
                           {"window": "60min_after_funding", "n": int(np.isfinite(after).sum()),
                            "mean_ret_bps": float(np.nanmean(after)), "t": tstat(after)}]), "funding_window.csv")

    lowsrc = b["t_low"].values if "t_low" in b and np.isfinite(b["t_low"].values).mean() > 0.5 else low
    bid_end = close * (1 - b["spread_end"].values / 2e4)
    mrows = []
    for X in (1, 5, 15):
        fmin = fwd_rolling_min(lowsrc, X)
        filled = fmin < bid_end
        ok = good & np.isfinite(fmin) & np.isfinite(fwd[60])
        gain_from_bid = fwd[60] + b["spread_end"].values / 2
        d1 = pd.DataFrame({"d": day[ok], "filled": filled[ok], "r": gain_from_bid[ok]})
        fill_by_day = d1.groupby("d")["filled"].mean()
        r_f = d1[d1["filled"]].groupby("d")["r"].mean()
        r_nf = d1[~d1["filled"]].groupby("d")["r"].mean()
        mrows.append({"wait_min": X, "fill_prob": float(fill_by_day.mean()),
                      "ret60_if_filled_bps": float(r_f.mean()), "t_filled": tstat(r_f.values),
                      "ret60_if_not_filled_bps": float(r_nf.mean()),
                      "adverse_selection_bps": float(r_f.mean() - r_nf.mean())})
    save_csv(pd.DataFrame(mrows), "maker_fill.csv")
    print(md_table(piv.reset_index(), "{:+.3f}"))


# ====================================================================== stage: model
def make_folds(idx):
    days = pd.DatetimeIndex(sorted(pd.unique(idx.floor("D"))))
    n, folds, start = len(days), [], C.TRAIN_MIN_DAYS
    while start < n:
        end = min(start + C.TEST_DAYS, n)
        if n - end < 3:
            end = n
        folds.append((days[start], days[end - 1] + pd.Timedelta(days=1)))
        start = end
    return folds


def gb_params():
    p = dict(max_iter=150, learning_rate=0.03, max_leaf_nodes=8, min_samples_leaf=500, l2_regularization=1.0)
    p.update(getattr(C, "GB_PARAMS", {}))
    return p


def fit_bin(X, y):
    from sklearn.ensemble import HistGradientBoostingClassifier
    m = HistGradientBoostingClassifier(early_stopping=False, random_state=C.RANDOM_SEED, **gb_params())
    m.fit(X, y)
    pos = list(m.classes_).index(1) if 1 in m.classes_ else None

    def predict(Xn):
        if pos is None:
            return np.zeros(len(Xn))
        return m.predict_proba(Xn)[:, pos]
    return predict


def fit_lr(X, y):
    from sklearn.linear_model import LogisticRegression
    mu, sd = np.nanmean(X, axis=0), np.nanstd(X, axis=0) + 1e-9

    def prep(Xn):
        return np.clip(np.nan_to_num((Xn - mu) / sd, nan=0.0, posinf=0.0, neginf=0.0), -10, 10)
    if y.min() == y.max():
        return lambda Xn: np.full(len(Xn), float(y.mean()))
    m = LogisticRegression(C=getattr(C, "LR_C", 0.1), max_iter=2000).fit(prep(X), y)
    pos = list(m.classes_).index(1)
    return lambda Xn: m.predict_proba(prep(Xn))[:, pos]


def fit_reg(X, y):
    from sklearn.ensemble import HistGradientBoostingRegressor
    m = HistGradientBoostingRegressor(early_stopping=False, random_state=C.RANDOM_SEED, **gb_params())
    m.fit(X, y)
    return m


def binll(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def har_fit_predict(Xtr, ytr, Xte):
    from sklearn.linear_model import LinearRegression
    mu = np.nanmean(Xtr, axis=0)
    Xtr = np.where(np.isfinite(Xtr), Xtr, mu)
    Xte = np.where(np.isfinite(Xte), Xte, mu)
    return LinearRegression().fit(Xtr, ytr).predict(Xte)


def perm_importance(predict, X, y, cols, n_rep=3):
    rng = np.random.default_rng(C.RANDOM_SEED)
    base = binll(predict(X), y)
    out = []
    for j, cname in enumerate(cols):
        s = 0.0
        for _ in range(n_rep):
            Xp = X.copy()
            Xp[:, j] = rng.permutation(Xp[:, j])
            s += binll(predict(Xp), y) - base
        out.append({"feature": cname, "logloss_increase": s / n_rep})
    return pd.DataFrame(out).sort_values("logloss_increase", ascending=False)


def safe_auc(y, p):
    from sklearn.metrics import roc_auc_score
    try:
        return float(roc_auc_score(y, p))
    except ValueError:
        return np.nan


def pnl_rows(O, H, key, select, gate=None, gate_name="all"):
    g, mdl = key.rsplit("_", 1)
    score = O[f"score_{key}"].values
    rank = np.abs(score) if select == "score" else O[f"ev_{key}"].values
    days, folds = O["day"].values, O["fold"].values
    n_days = len(np.unique(days))
    rows = []
    for q in C.TOP_FRACTIONS:
        tau = np.nanquantile(rank, 1 - q) if q < 1 else -np.inf
        sel = np.isfinite(score) & (score != 0) & (rank >= tau)
        if gate is not None:
            sel &= gate
        if sel.sum() < 20:
            continue
        side = np.sign(score[sel])
        gross = np.where(side > 0, O["g_long"].values[sel], O["g_short"].values[sel])
        nets = {"taker": gross - O["cost_taker"].values[sel], "mixed": gross - O["cost_mixed"].values[sel],
                "maker": gross - O["cost_maker"].values[sel]}
        r = {"H_min": H, "group": g, "model": mdl, "select": select, "gate": gate_name, "top_frac": q,
             "n_trades": int(sel.sum()), "trades_per_day": sel.sum() / n_days, "hit_rate": float((gross > 0).mean()),
             "barrier_mean_bps": float(O["bw"].values[sel].mean()), "exit_mean_min": float(O["t_exit"].values[sel].mean()),
             "gross_bps": float(gross.mean())}
        dsel, fsel = days[sel], folds[sel]
        for name, net in nets.items():
            r[f"net_{name}_bps"] = float(net.mean())
            r[f"t_{name}"] = tstat(pd.Series(net).groupby(dsel).mean().values)
            fm = pd.Series(net).groupby(fsel).mean()
            r[f"folds_pos_{name}"] = f"{int((fm > 0).sum())}/{len(fm)}"
        rows.append(r)
    return rows


def simulate(O, H, key, q):
    g, mdl = key.rsplit("_", 1)
    O = O.sort_values("ts").reset_index(drop=True)
    rank, score = O[f"ev_{key}"].values, O[f"score_{key}"].values
    tau = np.nanquantile(rank, 1 - q)
    ts = O["ts"].dt.tz_convert("UTC").dt.tz_localize(None).values.astype("datetime64[ns]")
    cand = np.where(np.isfinite(score) & (score != 0) & (rank >= tau))[0]
    trades, pos = [], 0
    while pos < len(cand):
        i = cand[pos]
        side = 1 if score[i] > 0 else -1
        gross = O["g_long"].iat[i] if side > 0 else O["g_short"].iat[i]
        exit_ts = ts[i] + np.timedelta64(int(O["t_exit"].iat[i]), "m")
        trades.append({"entry_ts": O["ts"].iat[i], "exit_ts": pd.Timestamp(exit_ts, tz="UTC"), "side": side,
                       "hold_min": O["t_exit"].iat[i], "gross_bps": gross,
                       "net_taker_bps": gross - O["cost_taker"].iat[i], "net_mixed_bps": gross - O["cost_mixed"].iat[i],
                       "net_maker_bps": gross - O["cost_maker"].iat[i]})
        j = np.searchsorted(ts, exit_ts)
        pos = np.searchsorted(cand, max(j, i + 1))
    T = pd.DataFrame(trades)
    if T.empty:
        return [], T, None
    T.to_csv(out_path(f"sim_trades_H{H}_{key}.csv"), index=False)
    n_days = max(1, (O["ts"].iat[-1] - O["ts"].iat[0]).total_seconds() / 86400)
    rows, daily_mixed = [], None
    T["exit_day"] = T["exit_ts"].dt.floor("D")
    full_days = pd.date_range(O["ts"].iat[0].floor("D"), O["ts"].iat[-1].floor("D"), freq="D", tz="UTC")
    for name in ("taker", "mixed", "maker"):
        col = f"net_{name}_bps"
        daily = T.groupby("exit_day")[col].sum().reindex(full_days).fillna(0.0)
        if name == "mixed":
            daily_mixed = daily
        eq = daily.cumsum()
        rows.append({"H_min": H, "group": g, "model": mdl, "cost": name, "n_trades": len(T),
                     "trades_per_day": len(T) / n_days, "hit_rate": float((T["gross_bps"] > 0).mean()),
                     "mean_hold_min": float(T["hold_min"].mean()), "mean_net_bps_per_trade": float(T[col].mean()),
                     "total_net_bps": float(T[col].sum()), "daily_mean_bps": float(daily.mean()),
                     "daily_std_bps": float(daily.std(ddof=1)),
                     "sharpe_ann": float(daily.mean() / daily.std(ddof=1) * math.sqrt(365)) if daily.std(ddof=1) > 0 else np.nan,
                     "max_drawdown_bps": float((eq - eq.cummax()).min()), "pos_days_frac": float((daily > 0).mean())})
    return rows, T, daily_mixed


def plot_equity(H, curves, q):
    try:
        plt = setup_plot()
        fig, ax = plt.subplots(figsize=(7, 3.6))
        for i, (key, daily) in enumerate(curves.items()):
            if daily is None:
                continue
            ax.plot(daily.index, daily.cumsum().values, color=PAL[i % len(PAL)], lw=2, label=key)
        ax.axhline(0, color="#c3c2b7", lw=1)
        ax.set_ylabel("cumulative net P&L (bps), mixed cost")
        ax.set_title(f"Out-of-sample simulation, H={H} min, top {int(q*100)}% minutes by expected value", fontsize=10)
        ax.legend(frameon=False, title="features_model", fontsize=8, title_fontsize=8)
        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(out_path(f"equity_H{H}.png"), dpi=130)
        plt.close(fig)
    except Exception as e:
        log(f"equity plot skipped: {e}")


def criterion_rows(skill, by_fold, pnl):
    """Pre-registered test: CRIT_TEST vs CRIT_BASE, per horizon and learner, plus the trading criterion."""
    rows = []
    base, test = getattr(C, "CRIT_BASE", "P"), getattr(C, "CRIT_TEST", "PL2")
    for H in sorted(skill["H_min"].unique()):
        for mdl in ("gb", "lr"):
            sb = skill[(skill.H_min == H) & (skill.group == base) & (skill.model == mdl)]
            st = skill[(skill.H_min == H) & (skill.group == test) & (skill.model == mdl)]
            if sb.empty or st.empty:
                continue
            auc_b, auc_t = float(sb["dir_auc"].iloc[0]), float(st["dir_auc"].iloc[0])
            fb = by_fold[(by_fold.H_min == H) & (by_fold.key == f"{base}_{mdl}")].set_index("fold")["dir_auc"]
            ft = by_fold[(by_fold.H_min == H) & (by_fold.key == f"{test}_{mdl}")].set_index("fold")["dir_auc"]
            common = fb.index.intersection(ft.index)
            improved, n_f = int((ft[common] > fb[common]).sum()), len(common)
            info_pass = (auc_t - auc_b >= C.CRIT_DELTA_AUC) and (improved >= C.CRIT_FOLDS_IMPROVED)
            tr = pnl[(pnl.H_min == H) & (pnl.group == test) & (pnl.model == mdl) & (pnl.select == "ev") &
                     (pnl.gate == "all") & (np.isclose(pnl.top_frac, C.CRIT_TOP_FRAC))]
            if tr.empty:
                net, t, fp, fn = np.nan, np.nan, 0, 0
            else:
                net, t = float(tr[f"net_{C.CRIT_COST}_bps"].iloc[0]), float(tr[f"t_{C.CRIT_COST}"].iloc[0])
                fp, fn = [int(v) for v in str(tr[f"folds_pos_{C.CRIT_COST}"].iloc[0]).split("/")]
            trade_pass = bool(np.isfinite(net) and net > 0 and fn > 0 and fp > fn / 2 and np.isfinite(t) and t >= C.CRIT_T_MIN)
            role = "PRIMARY" if H == C.PRIMARY_H else ("diagnostic" if H < 30 else "secondary")
            rows.append({"H_min": H, "role": role, "model": mdl, f"auc_{base}": auc_b, f"auc_{test}": auc_t,
                         "delta_auc": auc_t - auc_b, "folds_improved": f"{improved}/{n_f}", "info_pass": info_pass,
                         f"net_{C.CRIT_COST}_top{int(C.CRIT_TOP_FRAC*100)}": net, "t_daily": t,
                         "folds_pos": f"{fp}/{fn}", "trade_pass": trade_pass})
    return pd.DataFrame(rows)


def stage_model():
    from scipy.stats import spearmanr
    b = load_bars()
    f = build_features(b)
    groups = feature_groups(f)
    log("feature groups: " + ", ".join(f"{k}={len(v)}" for k, v in groups.items()))
    idx = f.index
    sig = f["sigma1"].values
    close, high, low = b["close"].values, b["high"].values, b["low"].values
    quality = (b["n_rows"].values >= C.MIN_ROWS_PER_MIN) & np.isfinite(f["lrv_1440" if "lrv_1440" in f else "lrv_240"].values) & np.isfinite(sig)
    spread = b["spread_end"].ffill().fillna(0.0).values
    cst = cost_bps(spread)
    rv = b["rv"].fillna(0.0)
    folds = make_folds(idx)
    log(f"{len(folds)} walk-forward folds; first test block starts {folds[0][0]:%Y-%m-%d}")
    summary, by_fold, pnl, volm, sims, imps = [], [], [], [], [], []
    gl = list(groups)[-1]
    sim_keys = [k for k in ("PL2", "P", "L2", "ALL", "PFB", "F") if k in groups][:4]
    for H in C.MODEL_HORIZONS:
        t0 = time.time()
        lab = triple_barrier(close, high, low, sig, H, C.BARRIER_K)
        y_vol = np.log(rv.rolling(H).sum().shift(-H).values / H + 1e-12)
        valid = quality & np.isfinite(lab["label"]) & np.isfinite(y_vol)
        y_dir_all = (lab["fwd"] > 0).astype(float)
        y_hit_all = (lab["label"] != 0).astype(float)
        har_cols = [c for c in ("lrv_5", "lrv_15", "lrv_60", "lrv_240", "lrv_1440", "tod_sin", "tod_cos") if c in f]
        stride = C.TRAIN_STRIDE.get(H, 1)
        oos, last_model = [], None
        for fi, (t0f, t1f) in enumerate(folds):
            tr = valid & (idx < t0f - pd.Timedelta(minutes=H))
            te = valid & (idx >= t0f) & (idx < t1f)
            tr_idx, te_idx = np.where(tr)[0][::stride], np.where(te)[0]
            if len(te_idx) < 100 or len(tr_idx) < 2000:
                continue
            y_dir, y_hit = y_dir_all[tr_idx], y_hit_all[tr_idx]
            rec = pd.DataFrame({"ts": idx[te_idx], "fold": fi, "label": lab["label"][te_idx], "bw": lab["bw"][te_idx],
                                "tie": lab["tie"][te_idx], "t_exit": lab["t_exit"][te_idx], "fwd": lab["fwd"][te_idx],
                                "g_long": lab["g_long"][te_idx], "g_short": lab["g_short"][te_idx],
                                "cost_taker": cst["taker"][te_idx], "cost_mixed": cst["mixed"][te_idx],
                                "cost_maker": cst["maker"], "sigma1": sig[te_idx],
                                "prior_up": y_dir.mean(), "prior_hit": y_hit.mean()})
            for g, cols in groups.items():
                X = f[cols].values
                for mdl, fitter in (("gb", fit_bin), ("lr", fit_lr)):
                    pdir = fitter(X[tr_idx], y_dir)
                    phit = fitter(X[tr_idx], y_hit)
                    rec[f"p_up_{g}_{mdl}"] = pdir(X[te_idx])
                    rec[f"p_hit_{g}_{mdl}"] = phit(X[te_idx])
                    if g == gl and mdl == "gb":
                        last_model = (pdir, X[te_idx], y_dir_all[te_idx], cols)
            X = f[groups[gl]].values
            rec["vol_hgb"] = fit_reg(X[tr_idx], y_vol[tr_idx]).predict(X[te_idx])
            Xh = f[har_cols].values
            rec["vol_har"] = har_fit_predict(Xh[tr_idx], y_vol[tr_idx], Xh[te_idx])
            rec["y_vol"], rec["train_mean_yvol"] = y_vol[te_idx], y_vol[tr_idx].mean()
            oos.append(rec)
            log(f"H={H} fold {fi}: train {len(tr_idx):,} test {len(te_idx):,} [{t0f:%m-%d}..{t1f:%m-%d}) "
                f"P(up)={y_dir.mean():.3f} P(hit)={y_hit.mean():.3f} ({time.time()-t0:.0f}s)")
        if not oos:
            log(f"H={H}: not enough data for walk-forward folds")
            continue
        O = pd.concat(oos, ignore_index=True)
        O["day"] = O["ts"].dt.floor("D").astype(str)
        y_dir_te = (O["fwd"].values > 0).astype(float)
        y_hit_te = (O["label"].values != 0).astype(float)
        ll0_dir, ll0_hit = binll(O["prior_up"].values, y_dir_te), binll(O["prior_hit"].values, y_hit_te)
        keys = [f"{g}_{m}" for g in groups for m in ("gb", "lr")]
        for key in keys:
            g, mdl = key.rsplit("_", 1)
            p_up, p_hit = O[f"p_up_{key}"].values, O[f"p_hit_{key}"].values
            score = p_up - O["prior_up"].values
            O[f"score_{key}"] = score
            O[f"ev_{key}"] = 2 * np.abs(score) * p_hit * O["bw"].values
            summary.append({"H_min": H, "group": g, "model": mdl, "n_oos": len(O), "n_folds": int(O["fold"].nunique()),
                            "dir_auc": safe_auc(y_dir_te, p_up),
                            "dir_skill_pct": (ll0_dir - binll(p_up, y_dir_te)) / ll0_dir * 100,
                            "ic_score_fwd": float(spearmanr(score, O["fwd"].values, nan_policy="omit")[0]),
                            "hit_auc": safe_auc(y_hit_te, p_hit),
                            "hit_skill_pct": (ll0_hit - binll(p_hit, y_hit_te)) / ll0_hit * 100,
                            "p_up_oos_mean": float(p_up.mean()), "prior_up_train_mean": float(O["prior_up"].mean()),
                            "realised_up_oos": float(y_dir_te.mean())})
            for fi, gidx in O.groupby("fold").indices.items():
                by_fold.append({"H_min": H, "fold": int(fi), "key": key, "n": len(gidx),
                                "dir_auc": safe_auc(y_dir_te[gidx], p_up[gidx])})
            pnl += pnl_rows(O, H, key, "score") + pnl_rows(O, H, key, "ev")
        med = O.groupby("fold")["vol_hgb"].transform("median").values
        for key in (f"{gl}_gb", f"{gl}_lr"):
            pnl += pnl_rows(O, H, key, "ev", gate=O["vol_hgb"].values >= med, gate_name="pred_vol_high")
            pnl += pnl_rows(O, H, key, "ev", gate=O["vol_hgb"].values < med, gate_name="pred_vol_low")
        for name in ("vol_hgb", "vol_har"):
            sse = ((O["y_vol"] - O[name]) ** 2).sum()
            sst = ((O["y_vol"] - O["train_mean_yvol"]) ** 2).sum()
            volm.append({"H_min": H, "model": name, "r2_oos": 1 - sse / sst,
                         "spearman": float(spearmanr(O[name], O["y_vol"])[0]),
                         "sigma_pred_med_bps": float(np.median(np.sqrt(np.exp(O[name])) * 1e4 * math.sqrt(H)))})
        curves = {}
        for key in [f"{g}_{m}" for g in sim_keys for m in ("gb", "lr")][:4]:
            srows, _, daily = simulate(O, H, key, 0.10)
            sims += srows
            curves[key] = daily
        plot_equity(H, curves, 0.10)
        if last_model is not None:
            pdir, Xte, yte_last, cols = last_model
            imp = perm_importance(pdir, Xte, yte_last, cols)
            imp.insert(0, "H_min", H)
            imps.append(imp.head(25))
        O.to_parquet(out_path(f"oos_H{H}.parquet"))
        log(f"H={H} done in {time.time()-t0:.0f}s")

    S, BF, Pn, V, Sm = pd.DataFrame(summary), pd.DataFrame(by_fold), pd.DataFrame(pnl), pd.DataFrame(volm), pd.DataFrame(sims)
    save_csv(S, "model_skill.csv")
    save_csv(BF, "model_skill_by_fold.csv")
    save_csv(Pn, "model_pnl.csv")
    save_csv(V, "vol_model.csv")
    if not Sm.empty:
        save_csv(Sm, "sim_summary.csv")
    if imps:
        save_csv(pd.concat(imps), "feature_importance.csv")
    if "PL2" in groups:
        CR = criterion_rows(S, BF, Pn)
        save_csv(CR, "criterion.csv")
        prim = CR[CR.H_min == C.PRIMARY_H]
        info_ok = bool(len(prim) == 2 and prim["info_pass"].all()) if C.CRIT_BOTH_LEARNERS else bool(prim["info_pass"].any())
        trade_ok = bool(prim["trade_pass"].any()) if len(prim) else False
        verdict = ("MATERIAL: L2 features pass the pre-registered information criterion at the primary horizon"
                   if info_ok else "NOT MATERIAL: L2 features fail the pre-registered information criterion at the primary horizon")
        verdict += "; trading criterion " + ("PASSED" if trade_ok else "FAILED")
        with open(out_path("VERDICT.txt"), "w") as fh:
            fh.write(verdict + "\n")
        print("\n" + md_table(CR, "{:.4f}"))
        print("\nVERDICT:", verdict)
    print()
    print(md_table(S, "{:.4f}"))


# ====================================================================== stage: report
def stage_report():
    cfg = {"V2_RAW_GLOB": getattr(C, "V2_RAW_GLOB", None), "FEES": C.FEES, "HORIZONS": C.HORIZONS,
           "MODEL_HORIZONS": C.MODEL_HORIZONS, "BARRIER_K": C.BARRIER_K, "K_LIST": C.K_LIST,
           "SIGMA_WINDOW_MIN": C.SIGMA_WINDOW_MIN, "TRAIN_MIN_DAYS": C.TRAIN_MIN_DAYS, "TEST_DAYS": C.TEST_DAYS,
           "TRAIN_STRIDE": C.TRAIN_STRIDE, "GB_PARAMS": gb_params(), "LR_C": getattr(C, "LR_C", 0.1),
           "TOP_FRACTIONS": C.TOP_FRACTIONS,
           "CRITERION": {k: getattr(C, k) for k in ("PRIMARY_H", "CRIT_BASE", "CRIT_TEST", "CRIT_DELTA_AUC",
                                                     "CRIT_FOLDS_IMPROVED", "CRIT_BOTH_LEARNERS", "CRIT_TOP_FRAC",
                                                     "CRIT_COST", "CRIT_T_MIN") if hasattr(C, k)}}
    fence = "`" * 3
    parts = ["# Research screen report (v2)", "",
             f"generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}", "",
             "## Config", "", fence] + [f"{k} = {json.dumps(v, default=str)}" for k, v in cfg.items()] + [fence, ""]
    vp = out_path("VERDICT.txt")
    if os.path.exists(vp):
        parts += ["## Verdict", "", open(vp).read().strip(), ""]
    sections = [
        ("Pre-registered criterion", "criterion.csv", "{:.4f}",
         "info_pass = delta_auc >= CRIT_DELTA_AUC and folds_improved >= CRIT_FOLDS_IMPROVED; "
         "trade_pass = net > 0 at the chosen cost in the top fraction by expected value, positive in a majority of folds, "
         "daily-mean t >= CRIT_T_MIN. Horizons below 30 minutes are diagnostic only."),
        ("Data quality by day", "data_quality_by_day.csv", "{:.2f}",
         "missing_sec_pct = seconds without a row; max_abs_r = largest 1-second mid move in bps."),
        ("Cost assumptions", "costs.csv", "{:.3f}", "round-trip costs in bps; taker crosses the spread twice."),
        ("Move size vs cost per horizon", "horizon_room.csv", "{:.2f}",
         "absmove = |close-to-close return| over H minutes; frac_gt_x = share of minutes where the move exceeds that cost."),
        ("Triple-barrier base rates", "barrier_base_rates.csv", "{:.3f}",
         "take-profit = stop-loss = k*sigma_H. breakeven_hit = hit rate needed on barrier-ending trades to cover cost."),
        ("Signal decay: Spearman IC (rows = signal, columns = horizon in minutes)", "ic_decay_matrix.csv", "{:+.3f}",
         "positive = higher signal -> higher forward return. Non-overlapping subsamples; t-stats in the next table."),
        ("Signal decay t-stats", "ic_decay.csv", "{:+.2f}",
         "ic_t uses the number of non-overlapping observations; share_weeks_pos = share of 7-day blocks with IC > 0."),
        ("Big 15-minute moves (>2 sigma): follow-through", "bigmove_followthrough.csv", "{:.2f}",
         "mean_follow > 0 means continuation in the direction of the move, < 0 means reversal."),
        ("Hour of day (UTC)", "hour_of_day.csv", "{:.2f}", ""),
        ("Funding window", "funding_window.csv", "{:.2f}", ""),
        ("Passive (maker) entry: fill probability and adverse selection", "maker_fill.csv", "{:.3f}",
         "ret60_if_filled = 60-min return measured from the bid when a passive buy at the bid is filled within wait_min."),
        ("Walk-forward model skill", "model_skill.csv", "{:.4f}",
         "Two binary models per fold: direction (sign of the H-minute return) and hit (either barrier reached). "
         "dir_auc = 0.5 means no directional information; *_skill_pct = log-loss improvement over the training base rate."),
        ("Direction AUC by fold", "model_skill_by_fold.csv", "{:.4f}", ""),
        ("Walk-forward P&L after cost by confidence", "model_pnl.csv", "{:.2f}",
         "score = P(up) minus the training base rate. top_frac = fraction of out-of-sample minutes traded, ranked by |score| "
         "or by expected value (2*|score|*P(hit)*barrier). t_* = t-stat of daily mean net P&L. folds_pos = folds with positive mean net."),
        ("Volatility model", "vol_model.csv", "{:.3f}", "target = log mean per-minute realized variance over the next H minutes."),
        ("Sequential simulation (one position at a time)", "sim_summary.csv", "{:.2f}", ""),
        ("Feature importance (permutation, last fold, direction model, largest feature set)", "feature_importance.csv", "{:.4f}", ""),
    ]
    for title, fname, fmt, note in sections:
        p = out_path(fname)
        if not os.path.exists(p):
            continue
        df = pd.read_csv(p)
        if fname == "model_skill_by_fold.csv":
            df = df.pivot_table(index=["H_min", "key"], columns="fold", values="dir_auc").reset_index()
        parts += [f"## {title}", ""]
        if note:
            parts += [note, ""]
        parts += [md_table(df, fmt, max_rows=500), ""]
    p = out_path("REPORT.md")
    with open(p, "w") as fh:
        fh.write("\n".join(parts))
    log(f"wrote {p}  ({os.path.getsize(p)/1024:.0f} KB)")


# ====================================================================== main
STAGES = {"sanity": stage_sanity, "bars": stage_bars, "bars2": stage_bars2, "horizon": stage_horizon,
          "events": stage_events, "model": stage_model, "report": stage_report}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in list(STAGES) + ["all"]:
        sys.exit(__doc__)
    if sys.argv[1] == "all":
        for name in ("horizon", "events", "model", "report"):
            log(f"===== {name}")
            STAGES[name]()
    else:
        STAGES[sys.argv[1]]()
