"""compare.py — Bybit (walk-forward out-of-sample) vs Bitfinex (external, no fitting) side by side."""
import pandas as pd
import numpy as np

pd.set_option("display.width", 280)
S = pd.read_csv("transfer_skill.csv")
Pn = pd.read_csv("transfer_pnl.csv")
V = pd.read_csv("transfer_vol.csv")
Sm = pd.read_csv("transfer_sims.csv")
St = pd.read_csv("transfer_strategies.csv")
bs = pd.read_csv("bybit_ref_skill.csv")
bv = pd.read_csv("bybit_ref_vol.csv")
bp = pd.read_csv("bybit_ref_pnl.csv")

# 1) skill: direction AUC, hit AUC
sk = S.merge(bs, on=["H_min", "group", "model"], how="left")
sk["d_dir_auc"] = sk["dir_auc"] - sk["bybit_dir_auc"]
sk["d_hit_auc"] = sk["hit_auc"] - sk["bybit_hit_auc"]
cols = ["H_min", "group", "model", "shifted_feature_share", "bybit_dir_auc", "dir_auc", "d_dir_auc", "bybit_hit_auc", "hit_auc", "d_hit_auc", "bybit_ic", "ic_score_fwd"]
sk = sk[cols].rename(columns={"dir_auc": "bitfinex_dir_auc", "hit_auc": "bitfinex_hit_auc", "ic_score_fwd": "bitfinex_ic"})
sk.to_csv("compare_skill.csv", index=False)

# 2) vol R2
vv = V.merge(bv, on=["H_min", "model"], how="left")[["H_min", "model", "bybit_r2", "r2_external", "bybit_spearman", "spearman"]]
vv.to_csv("compare_vol.csv", index=False)

# 3) P&L: fixed-hold, all signals, top fractions, by ev; Bybit gross from the zero-fee sensitivity file
pf = Pn[(Pn.exit == "fixed_hold") & (Pn.gate == "all") & (Pn.select == "ev")].merge(bp, on=["H_min", "group", "model", "top_frac"], how="left")
pf = pf[["H_min", "group", "model", "top_frac", "bybit_n", "n_trades", "bybit_gross_bps", "gross_bps", "spread_rt_bps", "net_bps", "win_rate",
         "bybit_t_gross", "t_daily", "bybit_folds_pos", "days_pos"]].rename(columns={"n_trades": "bitfinex_n", "gross_bps": "bitfinex_gross_bps",
                                                                                      "net_bps": "bitfinex_net_bps", "t_daily": "bitfinex_t_net", "days_pos": "bitfinex_days_pos"})
pf.to_csv("compare_pnl.csv", index=False)

print("=== DIRECTION / HIT AUC: Bybit out-of-sample vs Bitfinex external (no fitting) ===")
print(sk.round(4).to_string(index=False))
print("\n=== VOLATILITY R2 ===")
print(vv.round(3).to_string(index=False))
print("\n=== P&L, fixed hold, top 10% by expected value, all signals (gross at mid; Bitfinex net = gross - actual spread - 1 bp slippage, zero fees) ===")
print(pf[pf.top_frac == 0.10].round(3).to_string(index=False))
print("\n=== P&L, top 5% ===")
print(pf[pf.top_frac == 0.05].round(3).to_string(index=False))
print("\n=== vol-gated (ALL_gb, ev, pred_vol_high) on Bitfinex ===")
print(Pn[(Pn.gate == "pred_vol_high") & (Pn.exit == "fixed_hold")].round(3).to_string(index=False))
print("\n=== one-position-at-a-time simulations on Bitfinex ===")
print(Sm.round(3).to_string(index=False))
print("\n=== breakout / fade on Bitfinex with the Bybit vol model's forecast ===")
print(St.round(3).to_string(index=False))
# summary verdict lines
best = sk.loc[sk.groupby("H_min")["bitfinex_dir_auc"].idxmax()]
print("\nbest Bitfinex direction AUC per horizon:")
print(best[["H_min", "group", "model", "bybit_dir_auc", "bitfinex_dir_auc"]].round(4).to_string(index=False))
print("\nshare of model cells with Bitfinex dir AUC > 0.5:", round(float((S.dir_auc > 0.5).mean()), 3),
      "| > 0.52:", round(float((S.dir_auc > 0.52).mean()), 3), "| cells:", len(S))
print("share of Bitfinex net-positive P&L cells (fixed hold, ev):", round(float((pf.bitfinex_net_bps > 0).mean()), 3), "of", len(pf))
