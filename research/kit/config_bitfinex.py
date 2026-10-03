"""
config.py — v2. Pre-filled for the Bybit BTCUSDT 1-second files described in SCHEMA.md.
You should only need to check the three paths in the "v2 data location" block.
"""

# ---------------------------------------------------------------- v2 data location (native files, no copy needed)
V2_RAW_GLOB = "../data_bitfinex/bitfinex_1s/*.parquet"
V2_LVL_PATTERN = "../data_bitfinex/bitfinex_lvl/{date}.npy"
V1_BARS_FOR_CHECK = None
OUT_DIR = "out"
MAX_FILES = None                                 # e.g. 3 for a smoke test
L2_WEIGHT_BPS = 0.2                              # distance scale (bps) for the distance-weighted top-10 imbalance

# ---------------------------------------------------------------- v1 data location (only used by `bars`, kept for reference)
DATA_GLOB = "kit_input/*.parquet"
CHUNK_ROWS = 1_000_000
TS_UNIT = "s"

# ---------------------------------------------------------------- column mapping: expected name -> YOUR column name
COLS = {
    "ts": "ts",                      # bars2 creates 'ts' from the index 'sec'
    "bid_px": "bid1", "ask_px": "ask1", "bid_sz": "bsz1", "ask_sz": "asz1",
    "buy_vol": "buy_vol", "sell_vol": "sell_vol", "n_trades": "n_trades",
    "n_large": "large_trades",       # = big_buy + big_sell (volume), created by bars2
    "trade_high": "px_max", "trade_low": "px_min", "trade_last": "px_last",
    "ofi": "ofi",
    "added_2bps": "added_2bps", "removed_2bps": "removed_2bps",   # = add_b + add_a, rem_b + rem_a, created by bars2
    "depth_bid_05": "bd0.5", "depth_ask_05": "ad0.5",
    "depth_bid_1": "bd1", "depth_ask_1": "ad1",
    "depth_bid_3": "bd3", "depth_ask_3": "ad3",
    # v2 additions (per-side and book-state columns used only by the L2 features)
    "add_b": "add_b", "rem_b": "rem_b", "add_a": "add_a", "rem_a": "rem_a",
    "big_buy": "big_buy", "big_sell": "big_sell",
    "mid_hi": "mid_hi", "mid_lo": "mid_lo",
    "depth_bid_2": "bd2", "depth_ask_2": "ad2",
    "depth_bid_tot": "bdtot", "depth_ask_tot": "adtot",
    "reach_bid": "breach", "reach_ask": "areach",
}
L10 = None                                        # levels come from V2_LVL_PATTERN, not from columns

# ---------------------------------------------------------------- costs (fractions, per side)
FEES = {"taker": 0.00055, "maker": 0.00020, "slippage": 0.00005}

# ---------------------------------------------------------------- research design (unchanged from v1 where it matters)
HORIZONS = [5, 15, 30, 60, 120, 240]
MODEL_HORIZONS = [5, 15, 60]
BARRIER_K = 1.0
K_LIST = [0.75, 1.0, 1.5]
SIGMA_WINDOW_MIN = 240
FUNDING_HOURS_UTC = [0, 8, 16]
TRAIN_MIN_DAYS = 4
TEST_DAYS = 1
TRAIN_STRIDE = {5: 1, 15: 1, 30: 1, 60: 2, 120: 3, 240: 4}
GB_PARAMS = {"max_iter": 150, "learning_rate": 0.03, "max_leaf_nodes": 8, "min_samples_leaf": 500, "l2_regularization": 1.0}
LR_C = 0.1
TOP_FRACTIONS = [0.02, 0.05, 0.10, 0.20, 0.50, 1.00]
MIN_ROWS_PER_MIN = 30
RANDOM_SEED = 7
BARS_FILE = "bars_1m_v2.parquet"                  # what horizon/events/model read; falls back to bars_1m.parquet

# ---------------------------------------------------------------- PRE-REGISTERED CRITERION (do not change after the run)
PRIMARY_H = 60                     # primary horizon; 30/120/240 secondary; 5/15 diagnostic only
CRIT_BASE, CRIT_TEST = "P", "PL2"  # price-only baseline vs price + full L2 feature set, same folds and rows
CRIT_DELTA_AUC = 0.02              # minimum out-of-sample direction AUC gain
CRIT_FOLDS_IMPROVED = 3
CRIT_BOTH_LEARNERS = True          # gb and lr must both pass
CRIT_TOP_FRAC = 0.10               # trading criterion: top 10% of minutes by expected value ...
CRIT_COST = "mixed"                # ... at the mixed cost (passive entry, market exit, 8 bps) ...
CRIT_T_MIN = 2.0                   # ... daily-mean t-stat >= 2, net > 0, positive in a majority of folds
