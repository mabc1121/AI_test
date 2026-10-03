# Research screen for BTCUSDT perpetual, 1-second L2 data

Everything you need is in this one file: four code files to copy out, and the instructions to run them.
The code was tested end to end on synthetic data (including a pure-noise run to confirm no look-ahead leakage),
under pandas 2.2 and pandas 3.0. It has not seen your real files, so the first run is a schema check.

## 1. What the tests answer

The research question we settled on: *on BTCUSDT perp, using only streamable data, can a model decide every minute
whether price will travel k·σ in one direction before k·σ in the other within 15 to 240 minutes, with enough skill
above the cost-adjusted breakeven that trading only its most confident calls is profitable after cost?*

| Stage | Question it answers | Output |
|---|---|---|
| `sanity` | Are the columns mapped and the timestamps parsed correctly? | console |
| `bars` | Builds 1-minute bars from the 1-second rows; per-day data quality (gaps, duplicates, crossed books, spread) | `bars_1m.parquet`, `data_quality_by_day.csv` |
| `horizon` | At each horizon, how big is the typical move relative to round-trip cost? Triple-barrier base rates and the hit rate you would need to break even | `horizon_room.csv`, `barrier_base_rates.csv`, `costs.csv`, `horizon_room.png` |
| `events` | Which raw signals predict forward returns, and how fast does that fade (daily Spearman IC by horizon)? Do big moves continue or reverse? Hour-of-day and funding effects. Can you get passive (maker) fills, and what adverse selection do you pay? | `ic_decay*.csv`, `bigmove_followthrough.csv`, `hour_of_day.csv`, `funding_window.csv`, `maker_fill.csv`, `ic_decay.png` |
| `model` | Walk-forward (expanding window, purged) models on four feature groups (price-only, flow-only, book-only, all) with two learners each (gradient boosting and L2 logistic regression). Direction skill, barrier-hit skill, P&L after cost when trading only the top x% most confident minutes, a volatility model, vol-gated P&L, and a one-position-at-a-time simulation | `model_skill.csv`, `model_pnl.csv`, `vol_model.csv`, `sim_summary.csv`, `feature_importance.csv`, `equity_H*.png`, `oos_H*.parquet` |
| `report` | Collects all tables into one Markdown file | `REPORT.md` |

Design choices worth knowing before you read results:

- **Decision at the close of each minute**, using only data up to that minute. Labels use minutes t+1..t+H.
- **Triple barrier**: take-profit = stop-loss = k·σ_H where σ_H = trailing 240-minute realised vol (from 1-second returns) scaled by √H. Label is which barrier is touched first (+1 / −1), 0 if neither inside H minutes. A minute that touches both barriers counts as a loss.
- **Costs**, round trip, in bps: *taker* = 2×taker fee + 2×slippage + full spread; *mixed* = taker + maker fee + slippage + half spread (passive entry, market exit); *maker* = 2×maker fee (both legs passive, assumes you were filled). Fees default to Bybit non-VIP derivatives. Edit `FEES` if your tier differs.
- **Walk-forward**: first train window 28 days, then test in 7-day blocks with an expanding training window. Training rows whose label window overlaps the test block are purged.
- **Score is centred**: the direction model's P(up) minus the training-period base rate, so a drift in the training period does not get counted as skill.
- **Two learners per feature group** because on weak signals a regularised logistic regression often beats boosting; if they disagree, that is itself information.
- 92 days is one regime. Treat anything that is not consistent across folds as noise.

## 2. Setup

Python 3.10 or newer. In an empty folder:

```
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Create the four files below (`requirements.txt`, `config.py`, `research.py`, `make_synthetic.py`) in that folder by copying each code block verbatim.

## 3. Optional but recommended: smoke test on synthetic data (5 minutes)

```
python make_synthetic.py --days 12 --out data_synth --signal 0.02
```

Then in `config.py` set `DATA_GLOB = "data_synth/*.parquet"`, `TRAIN_MIN_DAYS = 6`, `TEST_DAYS = 2`, and run

```
python research.py sanity
python research.py all
```

Expected on the synthetic data: the `events` IC table shows `ofi_1`, `timb_1`, `dimb*_end` around +0.10 at 1 to 5 minutes fading toward zero by 60 minutes; in `model_skill.csv` the flow-only logistic regression (`F`, `lr`) reaches a direction AUC around 0.53 at H=15 while the price-only group sits at 0.50. If you see that, the pipeline works on your machine. Then restore the three config values for the real data.

## 4. Run on the real data

1. Put the 1-second files in a folder (parquet or csv, one file per day is ideal; one big file also works, it is read in chunks). File names sorted alphabetically must be sorted in time.
2. In `config.py` set `DATA_GLOB`, and map **your** column names in `COLS` (right-hand side). Set a key to `None` if you do not have that column. If the top-10 levels are in the same rows, set the four patterns in `L10`; if they are in a separate file, set `L10 = None` for this round.
3. `python research.py sanity` and read the mapping report. Fix `COLS` / `TS_UNIT` until every column you have is mapped and the timestamps read as 2026-07-01 onwards with a median step of 1.000 s.
4. `python research.py bars` (one pass over all data; about 1 to 3 minutes per 10 days on a laptop; memory is bounded by `CHUNK_ROWS`).
5. `python research.py horizon`, then `python research.py events` (each a minute or two).
6. `python research.py model` (the slow one: 20 to 60 minutes for three horizons on a 4-core laptop; it prints progress per fold). To shorten, set `MODEL_HORIZONS = [60]` first.
7. `python research.py report`.

Or, after `sanity` passes, simply `python research.py all`.

## 5. What to send back

- `out/REPORT.md` (this is the main thing; it contains every table)
- `out/horizon_room.png`, `out/ic_decay.png`, `out/equity_H15.png`, `out/equity_H60.png`, `out/equity_H240.png`
- the console output of `python research.py sanity`

Do not send `bars_1m.parquet` or the `oos_H*.parquet` files; keep them, we will use them in the next round.

## 6. How to read the key numbers while you wait

- `horizon_room.csv`: `absmove_med_bps` versus `cost_*_bps`. A horizon is only worth modelling if the typical move is several times the cost. `frac_gt_2x_taker` is the share of minutes where even a perfect call would double the cost.
- `barrier_base_rates.csv`: `breakeven_hit_taker/mixed/maker` is the hit rate you need on barrier-ending trades. Compare with `hit_rate` in `model_pnl.csv`.
- `ic_decay_matrix.csv`: an IC of 0.02 with |t| > 3 is real but tiny; 0.05 or more at the 60-minute horizon would be remarkable. Watch how fast the flow signals (`ofi_*`, `timb_*`) fade.
- `maker_fill.csv`: `fill_prob` is how often a passive bid is traded through within the wait; `adverse_selection_bps` is what being filled costs you in subsequent drift. This decides whether the maker cost scenario is realistic.
- `model_skill.csv`: `dir_auc` of 0.50 is nothing; 0.52 is a weak real edge; `hit_auc` should be well above 0.5 (volatility is predictable). Compare `p_up_oos_mean`, `prior_up_train_mean`, `realised_up_oos` to see drift.
- `model_pnl.csv`: look at `net_*_bps` with `t_*` and `folds_pos_*` for `select = ev`, `top_frac` 0.05 to 0.20. Positive net with |t| > 2 and most folds positive is the bar. Anything else is not yet evidence.
- `sim_summary.csv`: the honest version of the same thing with one position at a time.

## 7. Troubleshooting

- *"REQUIRED canonical columns not mapped"*: fix the right-hand side of `COLS`.
- Timestamps come out in 1970 or 2200: set `TS_UNIT` explicitly (`"ms"`, `"us"`, `"ns"`, `"s"`, or `"iso"`).
- Memory: lower `CHUNK_ROWS` to 300_000.
- Too slow: set `MODEL_HORIZONS = [60]`, or raise `TRAIN_STRIDE` values, or lower `GB_PARAMS["max_iter"]` to 100.
- Depth columns are totals rather than per side: map only what you have; the imbalance features need both sides and are skipped otherwise.

---

## 8. The code

### `requirements.txt`

Python dependencies.

```text
numpy>=1.24
pandas>=2.0
pyarrow>=14
scikit-learn>=1.3
scipy>=1.10
matplotlib>=3.7
```

### `config.py`

The only file you edit: data location, column mapping, fees, research design.

```python
"""
config.py — the only file you should need to edit.

1. Point DATA_GLOB at your 1-second files (parquet or csv, one or many files, sorted by name = sorted by time).
2. Map your column names in COLS (left = name the code expects, right = name in YOUR file).
   Set a value to None if you do not have that column. Only ts, bid_px, ask_px are strictly required;
   everything else is optional and the tests that need it are skipped or reduced.
3. Run:  python research.py sanity   and check the mapping report before running anything else.
"""

# ---------------------------------------------------------------- data location
DATA_GLOB = "data/*.parquet"        # e.g. "data/btcusdt_2026-*.parquet" or "data/*.csv" or "data/*.csv.gz"
OUT_DIR = "out"
MAX_FILES = None                    # set e.g. 3 for a quick smoke test on the first 3 files
CHUNK_ROWS = 1_000_000              # rows per chunk when reading (memory control)

# timestamp unit of the ts column: "auto" (detect from magnitude), "s", "ms", "us", "ns", or "iso" (strings)
TS_UNIT = "auto"

# ---------------------------------------------------------------- column mapping
COLS = {
    # required
    "ts": "timestamp",
    "bid_px": "bid_price",
    "ask_px": "ask_price",
    # best-level sizes (book imbalance, microprice)
    "bid_sz": "bid_size",
    "ask_sz": "ask_size",
    # trades (strongly recommended)
    "buy_vol": "buy_volume",        # aggressive buy volume in the second (BTC)
    "sell_vol": "sell_volume",      # aggressive sell volume in the second (BTC)
    "n_trades": "trade_count",
    "n_large": "large_trades",      # count (or volume) of trades >= 1 BTC
    "trade_high": "high",           # high / low / last TRADE price in the second (NaN if no trade)
    "trade_low": "low",
    "trade_last": "last",
    # order flow
    "ofi": "ofi",                   # order-flow imbalance at the best levels (signed, BTC)
    "added_2bps": "size_added_2bps",
    "removed_2bps": "size_removed_2bps",
    # cumulative depth within X bps of mid, per side (BTC)
    "depth_bid_05": "bid_depth_0.5bps",
    "depth_ask_05": "ask_depth_0.5bps",
    "depth_bid_1": "bid_depth_1bps",
    "depth_ask_1": "ask_depth_1bps",
    "depth_bid_3": "bid_depth_3bps",
    "depth_ask_3": "ask_depth_3bps",
}

# top-10 levels per side (optional). Patterns with {i} = 1..n. Set L10 = None if these are in a separate file.
L10 = {
    "bid_dist": "bid_dist_{i}",     # distance from mid in bps (sign does not matter)
    "bid_size": "bid_size_{i}",
    "ask_dist": "ask_dist_{i}",
    "ask_size": "ask_size_{i}",
    "n": 10,
}

# ---------------------------------------------------------------- costs (fractions, per side)
FEES = {
    "taker": 0.00055,     # Bybit non-VIP derivatives taker
    "maker": 0.00020,     # Bybit non-VIP derivatives maker
    "slippage": 0.00005,  # per taker leg, on top of the spread (small size on BTC)
}

# ---------------------------------------------------------------- research design
HORIZONS = [5, 15, 30, 60, 120, 240]      # minutes, for the move-vs-cost and base-rate tests
MODEL_HORIZONS = [15, 60, 240]            # minutes, for the walk-forward model (each costs minutes of runtime)
BARRIER_K = 1.0                           # take-profit = stop-loss = K * sigma_H
K_LIST = [0.75, 1.0, 1.5]                 # barrier sizes to tabulate base rates for
SIGMA_WINDOW_MIN = 240                    # trailing window for the vol estimate used in barriers
FUNDING_HOURS_UTC = [0, 8, 16]            # Bybit funding timestamps

TRAIN_MIN_DAYS = 28                       # first training window (days), expanding afterwards
TEST_DAYS = 7                             # length of each walk-forward test block
TRAIN_STRIDE = {15: 1, 60: 2, 240: 4}     # train on every n-th minute for long horizons (labels overlap anyway)
GB_PARAMS = {                             # gradient boosting (sklearn HistGradientBoosting*), deliberately conservative
    "max_iter": 150, "learning_rate": 0.03, "max_leaf_nodes": 8, "min_samples_leaf": 500, "l2_regularization": 1.0,
}
LR_C = 0.1                                # inverse L2 strength of the logistic-regression baseline (smaller = stronger)
TOP_FRACTIONS = [0.02, 0.05, 0.10, 0.20, 0.50, 1.00]  # trade only the most confident x% of minutes
MIN_ROWS_PER_MIN = 30                     # minutes with fewer 1-second rows than this are treated as bad data
RANDOM_SEED = 7
```

### `research.py`

All stages. Do not edit unless you know why.

```python
#!/usr/bin/env python3
"""
research.py — horizon / cost / predictability screen for BTCUSDT perpetual 1-second L2 data.

Stages (run in this order; each writes CSV/PNG files into OUT_DIR):

  python research.py sanity    # schema + column-mapping check on the first file (seconds)
  python research.py bars      # 1-second rows -> 1-minute bars + per-day data-quality table (one full pass)
  python research.py horizon   # size of moves vs. cost per horizon, triple-barrier base rates, breakeven hit rates
  python research.py events    # signal decay (daily Spearman IC), big-move follow-through, time of day,
                               # funding window, passive (maker) fill probability and adverse selection
  python research.py model     # walk-forward gradient boosting (3 feature groups), P&L after cost by confidence,
                               # volatility model, vol-gated P&L, one-position-at-a-time simulation
  python research.py report    # assemble OUT_DIR/REPORT.md from everything above
  python research.py all       # bars -> horizon -> events -> model -> report

Only config.py is meant to be edited.
"""
import glob
import json
import math
import os
import sys
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

try:
    import config as C
except ImportError:
    sys.exit("config.py not found next to research.py")

REQUIRED = ["ts", "bid_px", "ask_px"]

# per-minute aggregation of the 1-second derived frame: (output column, input column, function)
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

# plot palette (validated categorical slots + chrome)
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
        "taker": (2 * f["taker"] + 2 * f["slippage"]) * 1e4 + spread_bps,          # cross the spread twice
        "mixed": (f["taker"] + f["maker"] + f["slippage"]) * 1e4 + spread_bps / 2,  # passive entry, market exit
        "maker": (2 * f["maker"]) * 1e4,                                            # passive both legs (if filled)
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


# ====================================================================== loading
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


# ====================================================================== 1-second -> 1-minute
def derive_seconds(raw):
    """Canonical 1-second chunk -> derived per-second frame (DatetimeIndex UTC) + per-day quality frame."""
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
    return d, qd


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
    n_l10 = sum(1 for v in rn.values() if v.startswith(("bid_size_", "ask_size_")))
    print(f"top-10-level columns found: {n_l10}")
    raw = next(iter_chunks(files[0]))
    raw = raw.head(200_000)
    ts = parse_ts(raw["ts"])
    print(f"\nfirst chunk: {len(raw):,} rows, ts {ts[0]} .. {ts[-1]}  (unit={C.TS_UNIT})")
    dt = np.diff(ts.values.astype("datetime64[ns]").astype("int64")) / 1e9
    print(f"median step between rows: {np.median(dt):.3f} s ; rows with step > 1.5 s: {(dt > 1.5).mean()*100:.2f}%")
    d, qd = derive_seconds(raw)
    print(f"mid: {d['mid'].min():.1f} .. {d['mid'].max():.1f} ; spread bps median {d['spread_bps'].median():.3f}, "
          f"p99 {d['spread_bps'].quantile(0.99):.3f} ; crossed book rows {int(d['crossed'].sum())}")
    print("\nper-second derived columns available:", ", ".join(d.columns))
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
            d, qd = derive_seconds(raw)
            minute = d.index.floor("min")
            spec = {o: (i, f) for o, i, f in AGG if i in d.columns}
            part = d.groupby(minute, sort=True).agg(**spec)
            parts.append(part)
            quals.append(qd)
            log(f"{os.path.basename(path)} chunk {ci}: {len(d):,} s-rows -> {len(part):,} minutes "
                f"[{d.index[0]:%Y-%m-%d %H:%M} .. {d.index[-1]:%Y-%m-%d %H:%M}]")
    allp = pd.concat(parts).sort_index(kind="mergesort")  # stable: keeps chunk order inside a minute
    merge_spec = {o: MERGE[f] for o, _, f in AGG if o in allp.columns}
    bars = allp.groupby(level=0).agg(merge_spec)
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
    bars.to_parquet(out_path("bars_1m.parquet"))
    log(f"bars: {len(bars):,} minutes {bars.index[0]} .. {bars.index[-1]} ; empty minutes filled: {n_gap} ; "
        f"{time.time()-t0:.0f}s")

    q = pd.concat(quals).groupby(level=0).agg(rows=("rows", "sum"), crossed=("crossed", "sum"),
                                              spread_sum=("spread_sum", "sum"), zero_trade=("zero_trade", "sum"),
                                              max_abs_r=("max_abs_r", "max"), dups=("dups", "sum"))
    q["missing_sec_pct"] = (1 - q["rows"] / 86400).clip(lower=0) * 100
    q["spread_mean_bps"] = q["spread_sum"] / q["rows"]
    q["zero_trade_pct"] = q["zero_trade"] / q["rows"] * 100
    q.index.name = "day"
    q = q.reset_index()
    q["day"] = pd.to_datetime(q["day"]).dt.strftime("%Y-%m-%d")
    q = q[["day", "rows", "missing_sec_pct", "dups", "crossed", "spread_mean_bps", "zero_trade_pct", "max_abs_r"]]
    save_csv(q, "data_quality_by_day.csv")
    print(md_table(q.describe().T.reset_index().rename(columns={"index": "stat"}), "{:.2f}"))


def load_bars():
    p = out_path("bars_1m.parquet")
    if not os.path.exists(p):
        sys.exit("run `python research.py bars` first")
    b = pd.read_parquet(p)
    if b.index.tz is None:
        b.index = b.index.tz_localize("UTC")
    return b


# ====================================================================== features
def build_features(b):
    f = pd.DataFrame(index=b.index)
    idx = b.index
    c = b["close"]
    lc = np.log(c)
    for n in (1, 5, 15, 60, 240):
        f[f"ret_{n}"] = (lc - lc.shift(n)) * 1e4
    rv = b["rv"].fillna(0.0)
    for n in (5, 15, 60, 240, 1440):
        f[f"lrv_{n}"] = np.log(rv.rolling(n, min_periods=max(2, n // 2)).sum() / n + 1e-12)
    f["rvr_5_60"] = f["lrv_5"] - f["lrv_60"]
    f["rvr_60_1440"] = f["lrv_60"] - f["lrv_1440"]
    sig = np.sqrt(rv.rolling(C.SIGMA_WINDOW_MIN, min_periods=60).mean()) * 1e4
    sig_alt = f["ret_1"].rolling(C.SIGMA_WINDOW_MIN, min_periods=60).std()
    sig = sig.where(sig > 0, sig_alt).clip(lower=0.5)
    f["sigma1"] = sig                                             # bps per sqrt(minute); helper, not a feature
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
        vma = vol.rolling(1440, min_periods=240).mean()
        for n in (1, 5, 60):
            f[f"volz_{n}"] = np.log((vol.rolling(n).mean() + 1e-9) / (vma + 1e-9))
    if "n_large" in b:
        nl = b["n_large"].fillna(0.0)
        nlma = nl.rolling(1440, min_periods=240).mean()
        f["nlarge_5"] = np.log((nl.rolling(5).mean() + 1e-3) / (nlma + 1e-3))
        f["nlarge_60"] = np.log((nl.rolling(60).mean() + 1e-3) / (nlma + 1e-3))
    if "ofi" in b:
        ofi = b["ofi"].fillna(0.0)
        sd = ofi.rolling(1440, min_periods=240).std().replace(0, np.nan)
        for n in (1, 5, 15):
            f[f"ofi_{n}"] = ofi.rolling(n).sum() / (sd * math.sqrt(n))
        if "ofi_tail" in b:
            f["ofi_tail"] = b["ofi_tail"] / sd
    if "added" in b and "removed" in b:
        net = b["added"].fillna(0.0) - b["removed"].fillna(0.0)
        sd = net.rolling(1440, min_periods=240).std().replace(0, np.nan)
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
    return f


def feature_groups(f):
    P = [c for c in f.columns if c.startswith(("ret_", "lrv_", "rvr_", "rpos_", "hl15", "tod_", "dow", "mins_to"))]
    F = [c for c in f.columns if c.startswith(("timb_", "volz_", "nlarge_", "ofi_", "netadd_"))]
    B = [c for c in f.columns if c.startswith(("spread_", "bimb_", "micro_", "dimb", "ldtot", "dtot1_chg", "l10_"))]
    groups = {"P": P}
    if F:
        groups["F"] = F
    if B:
        groups["B"] = B
    if F or B:
        groups["PFB"] = P + F + B
    return groups


# ====================================================================== labels
def triple_barrier(close, high, low, sigma1, H, k):
    """Entry at close[t]; take-profit = stop-loss = k*sigma1*sqrt(H) bps; path = minutes t+1..t+H.
    Returns dict of arrays: label (+1 up first / -1 down first / 0 neither), bw (barrier bps), tie,
    t_exit (minutes), fwd (close-to-close return bps), g_long / g_short (gross realized bps for a long / short)."""
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
    """min(x[t+1..t+H])"""
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
    room = pd.DataFrame(rows)
    base = pd.DataFrame(brows)
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
    """Spearman IC of signal x against the H-minute forward return y.
    Overlapping forward windows make the usual per-day correlation biased (negative, ~ -H/1440 per day) and its
    t-stat too optimistic. So: the IC is the average Spearman over non-overlapping subsamples (every H-th minute,
    several offsets), and the t-stat uses the number of non-overlapping observations. share_weeks_pos is the share
    of 7-day blocks whose own IC is positive (stability only; small-sample bias applies inside each block)."""
    ok = np.isfinite(x) & np.isfinite(y)
    xs, ys, ws = x[ok], y[ok], week[ok]
    n = len(xs)
    if n < 10 * H + 100:
        return np.nan, np.nan, n, np.nan
    offsets = range(0, H, max(1, H // 8))
    ics = []
    for o in offsets:
        xi, yi = xs[o::H], ys[o::H]
        if len(xi) > 50 and np.std(xi) > 0 and np.std(yi) > 0:
            ics.append(pd.Series(xi).corr(pd.Series(yi), method="spearman"))
    if not ics:
        return np.nan, np.nan, n, np.nan
    ic = float(np.nanmean(ics))
    n_eff = n / H
    t = ic * math.sqrt(n_eff)
    wk = []
    for w in np.unique(ws):
        m = ws == w
        if m.sum() >= 5 * H + 50:
            wk.append(pd.Series(xs[m]).corr(pd.Series(ys[m]), method="spearman"))
    wk = [v for v in wk if np.isfinite(v)]
    share_pos = float(np.mean(np.array(wk) > 0)) if wk else np.nan
    return ic, t, int(n_eff), share_pos


def stage_events():
    b = load_bars()
    f = build_features(b)
    idx = b.index
    day = idx.floor("D")
    close, high, low = b["close"].values, b["high"].values, b["low"].values
    sig = f["sigma1"].values
    good = (b["n_rows"].values >= C.MIN_ROWS_PER_MIN) & np.isfinite(f["lrv_1440"].values)
    hz = [1] + [h for h in C.HORIZONS if h != 1]
    fwd = {H: fwd_return(close, H) for H in hz}

    # 1) signal decay: Spearman IC of each candidate signal vs forward return at each horizon
    cands = ["ret_1", "ret_5", "ret_15", "ret_60", "ret_240", "timb_1", "timb_5", "timb_15", "timb_60", "timb_tail",
             "ofi_1", "ofi_5", "ofi_15", "ofi_tail", "netadd_1", "netadd_5", "bimb_end", "bimb_m5", "micro_end",
             "dimb05_end", "dimb1_end", "dimb3_end", "dimb1_m5", "l10_imb_end", "rpos_60", "rpos_240"]
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
        fig, ax = plt.subplots(figsize=(7, 0.32 * len(piv) + 1.5))
        im = ax.imshow(piv.values, cmap=cmap, vmin=-v, vmax=v, aspect="auto")
        ax.set_xticks(range(len(piv.columns)))
        ax.set_xticklabels([f"{h}m" for h in piv.columns])
        ax.set_yticks(range(len(piv)))
        ax.set_yticklabels(piv.index)
        ax.grid(False)
        for i in range(piv.shape[0]):
            for j in range(piv.shape[1]):
                val = piv.values[i, j]
                if np.isfinite(val):
                    ax.text(j, i, f"{val:+.3f}", ha="center", va="center", fontsize=7, color=INK)
        fig.colorbar(im, ax=ax, fraction=0.03, label="mean daily Spearman IC")
        ax.set_title("Signal -> forward return: information coefficient by horizon")
        fig.tight_layout()
        fig.savefig(out_path("ic_decay.png"), dpi=130)
        plt.close(fig)
    except Exception as e:  # plotting is optional
        log(f"ic plot skipped: {e}")

    # 2) big 15-minute moves: continuation or reversal? (non-overlapping events, 240 min apart)
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

    # 3) time of day (UTC): how big is the next hour's move, what does the spread look like, how much volume
    vol = (b["buy_vol"] + b["sell_vol"]).values if "buy_vol" in b else np.full(len(b), np.nan)
    hod = pd.DataFrame({"hour_utc": idx.hour, "abs_ret60_bps": np.abs(fwd[60]), "ret60_bps": fwd[60],
                        "spread_bps": b["spread_end"].values, "vol_per_min": vol, "sigma1_bps": sig})
    hod = hod[good].groupby("hour_utc").agg(abs_ret60_bps=("abs_ret60_bps", "mean"), ret60_bps=("ret60_bps", "mean"),
                                            spread_bps=("spread_bps", "mean"), vol_per_min=("vol_per_min", "mean"),
                                            sigma1_bps=("sigma1_bps", "mean"), n=("ret60_bps", "size")).reset_index()
    save_csv(hod, "hour_of_day.csv")

    # 4) funding window: return in the 60 min before / after each funding timestamp
    fmask = np.isin(idx.hour, C.FUNDING_HOURS_UTC) & (idx.minute == 0) & good
    before, after = f["ret_60"].values[fmask], fwd[60][fmask]
    fund = pd.DataFrame([{"window": "60min_before_funding", "n": int(np.isfinite(before).sum()),
                          "mean_ret_bps": float(np.nanmean(before)), "t": tstat(before)},
                         {"window": "60min_after_funding", "n": int(np.isfinite(after).sum()),
                          "mean_ret_bps": float(np.nanmean(after)), "t": tstat(after)}])
    save_csv(fund, "funding_window.csv")

    # 5) passive (maker) entry: join the bid at minute end; filled if price trades strictly through it within X min
    lowsrc = b["t_low"].values if "t_low" in b and np.isfinite(b["t_low"].values).mean() > 0.5 else low
    bid_end = close * (1 - b["spread_end"].values / 2e4)
    mrows = []
    for X in (1, 5, 15):
        fmin = fwd_rolling_min(lowsrc, X)
        filled = fmin < bid_end
        ok = good & np.isfinite(fmin) & np.isfinite(fwd[60])
        gain_from_bid = fwd[60] + b["spread_end"].values / 2  # return measured from the bid, not the mid
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
    print()
    print(md_table(pd.DataFrame(brows), "{:.2f}"))
    print()
    print(md_table(pd.DataFrame(mrows), "{:.3f}"))


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
    """binary gradient boosting; returns a callable X -> P(y=1)"""
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
    """L2 logistic regression on standardised features (NaN -> 0); returns a callable X -> P(y=1)"""
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
    """permutation importance of a binary model: log-loss increase when one feature is shuffled"""
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


def pnl_rows(O, H, key, select, gate=None, gate_name="all"):
    """Trade the top fraction of minutes ranked by |score| (select='score') or by expected value (select='ev').
    key = '<feature group>_<model>' e.g. 'PFB_gb'."""
    g, mdl = key.rsplit("_", 1)
    score = O[f"score_{key}"].values
    rank = np.abs(score) if select == "score" else O[f"ev_{key}"].values
    days = O["day"].values
    folds = O["fold"].values
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
            dm = pd.Series(net).groupby(dsel).mean()
            r[f"t_{name}"] = tstat(dm.values)
            fm = pd.Series(net).groupby(fsel).mean()
            r[f"folds_pos_{name}"] = f"{int((fm > 0).sum())}/{len(fm)}"
        rows.append(r)
    return rows


def simulate(O, H, key, q):
    """One position at a time: enter when the expected value is in the top q of out-of-sample minutes and we are flat,
    hold until barrier or time exit, then look for the next entry. Fixed notional, P&L in bps of notional.
    Returns (summary rows, trades, daily net P&L under the 'mixed' cost)."""
    g, mdl = key.rsplit("_", 1)
    O = O.sort_values("ts").reset_index(drop=True)
    rank = O[f"ev_{key}"].values
    score = O[f"score_{key}"].values
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
        j = np.searchsorted(ts, exit_ts)       # first minute at/after the exit
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
        dd = (eq - eq.cummax()).min()
        rows.append({"H_min": H, "group": g, "model": mdl, "cost": name, "n_trades": len(T),
                     "trades_per_day": len(T) / n_days, "hit_rate": float((T["gross_bps"] > 0).mean()),
                     "mean_hold_min": float(T["hold_min"].mean()), "mean_net_bps_per_trade": float(T[col].mean()),
                     "total_net_bps": float(T[col].sum()), "daily_mean_bps": float(daily.mean()),
                     "daily_std_bps": float(daily.std(ddof=1)),
                     "sharpe_ann": float(daily.mean() / daily.std(ddof=1) * math.sqrt(365)) if daily.std(ddof=1) > 0 else np.nan,
                     "max_drawdown_bps": float(dd), "pos_days_frac": float((daily > 0).mean())})
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


def stage_model():
    from sklearn.metrics import roc_auc_score
    from scipy.stats import spearmanr
    b = load_bars()
    f = build_features(b)
    groups = feature_groups(f)
    log("feature groups: " + ", ".join(f"{k}={len(v)}" for k, v in groups.items()))
    idx = f.index
    sig = f["sigma1"].values
    close, high, low = b["close"].values, b["high"].values, b["low"].values
    quality = (b["n_rows"].values >= C.MIN_ROWS_PER_MIN) & np.isfinite(f["lrv_1440"].values) & np.isfinite(sig)
    spread = b["spread_end"].ffill().fillna(0.0).values
    cst = cost_bps(spread)
    rv = b["rv"].fillna(0.0)
    folds = make_folds(idx)
    log(f"{len(folds)} walk-forward folds; first test block starts {folds[0][0]:%Y-%m-%d}")

    summary, pnl, volm, sims, imps = [], [], [], [], []
    gl = list(groups)[-1]                                            # the full feature set
    for H in C.MODEL_HORIZONS:
        t0 = time.time()
        lab = triple_barrier(close, high, low, sig, H, C.BARRIER_K)
        y_vol = np.log(rv.rolling(H).sum().shift(-H).values / H + 1e-12)
        valid = quality & np.isfinite(lab["label"]) & np.isfinite(y_vol)
        y_dir_all = (lab["fwd"] > 0).astype(float)                   # direction: close-to-close sign over H
        y_hit_all = (lab["label"] != 0).astype(float)                # magnitude: did either barrier get hit
        har_cols = [c for c in ("lrv_5", "lrv_15", "lrv_60", "lrv_240", "lrv_1440", "tod_sin", "tod_cos") if c in f]
        stride = C.TRAIN_STRIDE.get(H, 1)
        oos, last_model = [], None
        for fi, (t0f, t1f) in enumerate(folds):
            tr = valid & (idx < t0f - pd.Timedelta(minutes=H))       # purge: label window must end before test
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
            score = p_up - O["prior_up"].values          # centred on the training base rate: removes pure drift
            O[f"score_{key}"] = score
            O[f"ev_{key}"] = 2 * np.abs(score) * p_hit * O["bw"].values   # expected gross bps of the better side
            try:
                dir_auc, hit_auc = roc_auc_score(y_dir_te, p_up), roc_auc_score(y_hit_te, p_hit)
            except ValueError:
                dir_auc, hit_auc = np.nan, np.nan
            summary.append({"H_min": H, "group": g, "model": mdl, "n_oos": len(O), "n_folds": int(O["fold"].nunique()),
                            "dir_auc": dir_auc, "dir_skill_pct": (ll0_dir - binll(p_up, y_dir_te)) / ll0_dir * 100,
                            "ic_score_fwd": float(spearmanr(score, O["fwd"].values, nan_policy="omit")[0]),
                            "hit_auc": hit_auc, "hit_skill_pct": (ll0_hit - binll(p_hit, y_hit_te)) / ll0_hit * 100,
                            "p_up_oos_mean": float(p_up.mean()), "prior_up_train_mean": float(O["prior_up"].mean()),
                            "realised_up_oos": float(y_dir_te.mean())})
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
        for key in [k for k in (f"{gl}_gb", f"{gl}_lr", "F_gb", "F_lr") if k in keys]:
            srows, _, daily = simulate(O, H, key, 0.10)
            sims += srows
            curves[key] = daily
        plot_equity(H, curves, 0.10)
        if last_model is not None:
            pdir, Xte, yte_last, cols = last_model
            imp = perm_importance(pdir, Xte, yte_last, cols)
            imp.insert(0, "H_min", H)
            imps.append(imp.head(20))
        O.to_parquet(out_path(f"oos_H{H}.parquet"))
        log(f"H={H} done in {time.time()-t0:.0f}s")

    S, Pn, V, Sm = pd.DataFrame(summary), pd.DataFrame(pnl), pd.DataFrame(volm), pd.DataFrame(sims)
    save_csv(S, "model_skill.csv")
    save_csv(Pn, "model_pnl.csv")
    save_csv(V, "vol_model.csv")
    if not Sm.empty:
        save_csv(Sm, "sim_summary.csv")
    if imps:
        save_csv(pd.concat(imps), "feature_importance.csv")
    print(md_table(S, "{:.4f}"))
    print()
    keep = ["H_min", "group", "model", "select", "gate", "top_frac", "n_trades", "hit_rate", "barrier_mean_bps",
            "gross_bps", "net_taker_bps", "t_taker", "net_mixed_bps", "net_maker_bps", "t_maker", "folds_pos_taker",
            "folds_pos_maker"]
    if not Pn.empty:
        print(md_table(Pn[keep], "{:.2f}", max_rows=200))
    print()
    print(md_table(V, "{:.3f}"))
    if not Sm.empty:
        print()
        print(md_table(Sm, "{:.2f}"))


# ====================================================================== stage: report
def stage_report():
    cfg = {"DATA_GLOB": C.DATA_GLOB, "FEES": C.FEES, "HORIZONS": C.HORIZONS, "MODEL_HORIZONS": C.MODEL_HORIZONS,
           "BARRIER_K": C.BARRIER_K, "K_LIST": C.K_LIST, "SIGMA_WINDOW_MIN": C.SIGMA_WINDOW_MIN,
           "TRAIN_MIN_DAYS": C.TRAIN_MIN_DAYS, "TEST_DAYS": C.TEST_DAYS, "TRAIN_STRIDE": C.TRAIN_STRIDE,
           "GB_PARAMS": gb_params(), "LR_C": getattr(C, "LR_C", 0.1), "TOP_FRACTIONS": C.TOP_FRACTIONS}
    fence = "`" * 3
    parts = ["# Research screen report", "",
             f"generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}", "",
             "## Config", "", fence] + [f"{k} = {json.dumps(v, default=str)}" for k, v in cfg.items()] + [fence, ""]
    sections = [
        ("Data quality by day", "data_quality_by_day.csv", "{:.2f}",
         "missing_sec_pct = seconds without a row; max_abs_r = largest 1-second mid move in bps (data errors show up here)."),
        ("Cost assumptions", "costs.csv", "{:.3f}", "round-trip costs in bps; taker crosses the spread twice."),
        ("Move size vs cost per horizon", "horizon_room.csv", "{:.2f}",
         "absmove = |close-to-close return| over H minutes; frac_gt_x = share of minutes where the move exceeds that cost."),
        ("Triple-barrier base rates", "barrier_base_rates.csv", "{:.3f}",
         "take-profit = stop-loss = k*sigma_H. breakeven_hit = hit rate needed on barrier-ending trades to cover cost."),
        ("Signal decay: Spearman IC (rows = signal, columns = horizon in minutes)", "ic_decay_matrix.csv", "{:+.3f}",
         "positive = higher signal -> higher forward return. Computed on non-overlapping subsamples; see the next table "
         "for t-stats and week-by-week stability."),
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
         "dir_auc = 0.5 means no directional information; *_skill_pct = log-loss improvement over the training base rate "
         "(negative = worse than guessing the base rate). p_up_oos_mean vs realised_up_oos shows drift between periods."),
        ("Walk-forward P&L after cost by confidence", "model_pnl.csv", "{:.2f}",
         "score = P(up) minus the training base rate (drift removed). top_frac = fraction of out-of-sample minutes traded, "
         "ranked by |score| or by expected value (2*|score|*P(hit)*barrier). t_* = t-stat of daily mean net P&L. "
         "folds_pos = folds with positive mean net."),
        ("Volatility model", "vol_model.csv", "{:.3f}", "target = log mean per-minute realized variance over the next H minutes."),
        ("Sequential simulation (one position at a time)", "sim_summary.csv", "{:.2f}", ""),
        ("Feature importance (permutation, last fold, full feature set)", "feature_importance.csv", "{:.4f}", ""),
    ]
    for title, fname, fmt, note in sections:
        p = out_path(fname)
        if not os.path.exists(p):
            continue
        df = pd.read_csv(p)
        parts += [f"## {title}", ""]
        if note:
            parts += [note, ""]
        parts += [md_table(df, fmt, max_rows=400), ""]
    p = out_path("REPORT.md")
    with open(p, "w") as fh:
        fh.write("\n".join(parts))
    log(f"wrote {p}  ({os.path.getsize(p)/1024:.0f} KB) -> send this file back")


# ====================================================================== main
STAGES = {"sanity": stage_sanity, "bars": stage_bars, "horizon": stage_horizon, "events": stage_events,
          "model": stage_model, "report": stage_report}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in list(STAGES) + ["all"]:
        sys.exit(__doc__)
    if sys.argv[1] == "all":
        for name in ("bars", "horizon", "events", "model", "report"):
            log(f"===== {name}")
            STAGES[name]()
    else:
        STAGES[sys.argv[1]]()
```

### `make_synthetic.py`

Optional: fake data with the default column names, for the smoke test.

```python
#!/usr/bin/env python3
"""
make_synthetic.py — fake 1-second rows with the default column names in config.py, for a smoke test only.

  python make_synthetic.py --days 6 --out data_synth --signal 0.02     # weak, decaying order-flow signal
  python make_synthetic.py --days 6 --out data_synth --signal 0.0      # pure noise (leakage check: skill ~ 0)

Then set DATA_GLOB = "data_synth/*.parquet" in config.py (and TRAIN_MIN_DAYS / TEST_DAYS small enough for --days).
Nothing here resembles real market dynamics beyond vol clustering and a persistent latent flow.
"""
import argparse
import os

import numpy as np
import pandas as pd


def ar1(n, phi, rng, x0):
    """stationary AR(1) with unit variance, continued from x0"""
    e = rng.standard_normal(n) * np.sqrt(1 - phi ** 2)
    x = np.empty(n)
    prev = x0
    for i in range(n):
        prev = phi * prev + e[i]
        x[i] = prev
    return x


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=6)
    ap.add_argument("--out", default="data_synth")
    ap.add_argument("--start", default="2026-07-01")
    ap.add_argument("--signal", type=float, default=0.02, help="strength of the predictable flow component")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-l10", action="store_true", help="omit the top-10-level columns")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rng = np.random.default_rng(a.seed)
    S = 86400
    tick = 0.1
    mid_last = 100_000.0
    flow_last, vol_last = 0.0, 0.0
    for d in range(a.days):
        t0 = pd.Timestamp(a.start, tz="UTC") + pd.Timedelta(days=d)
        ts_ms = t0.value // 10 ** 6 + np.arange(S) * 1000
        flow = ar1(S, 0.995, rng, flow_last)    # latent order flow, memory ~ 3 minutes
        vst = ar1(S, 0.9995, rng, vol_last)     # slow volatility regime
        flow_last, vol_last = flow[-1], vst[-1]
        hour = np.arange(S) / 3600
        intraday = 1 + 0.5 * np.cos(2 * np.pi * (hour - 15) / 24)
        sigma = 8e-5 * np.exp(0.5 * vst) * intraday
        r = sigma * (rng.standard_normal(S) + a.signal * flow)
        mid = np.exp(np.log(mid_last) + np.cumsum(r))
        mid_last = mid[-1]
        half = tick * (0.5 + rng.poisson(0.3, S))
        bid = np.floor((mid - half) / tick) * tick
        ask = np.ceil((mid + half) / tick) * tick
        tanh = np.tanh(flow)
        bid_sz = np.exp(rng.normal(0.5, 0.8, S)) * (1 + 0.3 * tanh)
        ask_sz = np.exp(rng.normal(0.5, 0.8, S)) * (1 - 0.3 * tanh)
        base = np.exp(rng.normal(3.0, 0.5, S))
        n_trades = rng.poisson(3 * intraday, S)
        vol = n_trades * np.exp(rng.normal(-2, 1, S))
        buy_share = 1 / (1 + np.exp(-(0.8 * flow + rng.normal(0, 0.5, S))))
        buy_vol = vol * buy_share
        sell_vol = vol - buy_vol
        last = np.round((mid + rng.normal(0, 0.5 * tick, S)) / tick) * tick
        high = np.maximum(last, mid) + np.abs(rng.normal(0, tick, S))
        low = np.minimum(last, mid) - np.abs(rng.normal(0, tick, S))
        notrade = n_trades == 0
        last[notrade], high[notrade], low[notrade] = np.nan, np.nan, np.nan
        df = pd.DataFrame({
            "timestamp": ts_ms, "bid_price": bid, "ask_price": ask, "bid_size": bid_sz, "ask_size": ask_sz,
            "spread": ask - bid,
            "bid_depth_0.5bps": base * 0.3 * (1 + 0.2 * tanh), "ask_depth_0.5bps": base * 0.3 * (1 - 0.2 * tanh),
            "bid_depth_1bps": base * 0.6 * (1 + 0.15 * tanh), "ask_depth_1bps": base * 0.6 * (1 - 0.15 * tanh),
            "bid_depth_2bps": base * 1.2 * (1 + 0.1 * tanh), "ask_depth_2bps": base * 1.2 * (1 - 0.1 * tanh),
            "bid_depth_3bps": base * 1.8 * (1 + 0.05 * tanh), "ask_depth_3bps": base * 1.8 * (1 - 0.05 * tanh),
            "ofi": 2 * flow + rng.normal(0, 2, S),
            "size_added_2bps": np.exp(rng.normal(0, 1, S)), "size_removed_2bps": np.exp(rng.normal(0, 1, S)),
            "buy_volume": buy_vol, "sell_volume": sell_vol, "trade_count": n_trades,
            "large_trades": rng.poisson(0.05 * (1 + np.abs(flow)), S),
            "high": high, "low": low, "last": last,
        })
        if not a.no_l10:
            for i in range(1, 11):
                df[f"bid_dist_{i}"] = -(i * 0.4 + rng.uniform(0, 0.2, S))
                df[f"bid_size_{i}"] = np.exp(rng.normal(0, 1, S)) * (1 + 0.1 * tanh)
                df[f"ask_dist_{i}"] = i * 0.4 + rng.uniform(0, 0.2, S)
                df[f"ask_size_{i}"] = np.exp(rng.normal(0, 1, S)) * (1 - 0.1 * tanh)
        path = os.path.join(a.out, f"btcusdt_{t0:%Y-%m-%d}.parquet")
        df.to_parquet(path, index=False)
        print(f"wrote {path}  rows={len(df):,}  mid {mid[0]:.0f}->{mid[-1]:.0f}")


if __name__ == "__main__":
    main()
```
