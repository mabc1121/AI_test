"""compare_clean.py — previous overlapping test (trained through 09-30) vs clean test (frozen after 09-23), same Bitfinex period."""
import sys
import pandas as pd
import numpy as np

pd.set_option("display.width", 320); pd.set_option("display.max_rows", 500)
CL = sys.argv[1] if len(sys.argv) > 1 else "clean_before20260924"
bs = pd.read_csv("bybit_ref_skill.csv"); bv = pd.read_csv("bybit_ref_vol.csv")

# skill
a = pd.read_csv("transfer_skill.csv"); b = pd.read_csv(f"{CL}/transfer_skill.csv")
k = ["H_min", "group", "model"]
sk = a[k + ["dir_auc", "hit_auc", "ic_score_fwd"]].merge(b[k + ["dir_auc", "hit_auc", "ic_score_fwd"]], on=k, suffixes=("_prev", "_clean")).merge(bs, on=k, how="left")
sk["d_dir"] = sk.dir_auc_clean - sk.dir_auc_prev
sk = sk[k + ["bybit_dir_auc", "dir_auc_prev", "dir_auc_clean", "d_dir", "bybit_hit_auc", "hit_auc_prev", "hit_auc_clean", "ic_score_fwd_prev", "ic_score_fwd_clean"]]
sk.to_csv(f"{CL}/compare_prev_vs_clean_skill.csv", index=False)

# vol
va = pd.read_csv("transfer_vol.csv"); vb = pd.read_csv(f"{CL}/transfer_vol.csv")
vv = va[["H_min", "model", "r2_external", "spearman"]].merge(vb[["H_min", "model", "r2_external", "spearman"]], on=["H_min", "model"], suffixes=("_prev", "_clean")).merge(bv, on=["H_min", "model"], how="left")
vv = vv[["H_min", "model", "bybit_r2", "r2_external_prev", "r2_external_clean", "spearman_prev", "spearman_clean"]]
vv.to_csv(f"{CL}/compare_prev_vs_clean_vol.csv", index=False)

# pnl: fixed hold, all gate, every select/top_frac
pa = pd.read_csv("transfer_pnl.csv"); pb = pd.read_csv(f"{CL}/transfer_pnl.csv")
kk = ["H_min", "group", "model", "select", "gate", "top_frac", "exit"]
vals = ["n_trades", "win_rate", "gross_bps", "spread_rt_bps", "net_bps", "days_pos", "t_daily"]
pp = pa[kk + vals].merge(pb[kk + vals], on=kk, suffixes=("_prev", "_clean"))
pp.to_csv(f"{CL}/compare_prev_vs_clean_pnl.csv", index=False)

sa = pd.read_csv("transfer_sims.csv"); sb = pd.read_csv(f"{CL}/transfer_sims.csv")
ks = ["H_min", "group", "model", "sim"]
sv = [v for v in vals if v in sa.columns]
ss = sa[ks + sv].merge(sb[ks + sv], on=ks, suffixes=("_prev", "_clean"))
ss.to_csv(f"{CL}/compare_prev_vs_clean_sims.csv", index=False)
ta = pd.read_csv("transfer_strategies.csv"); tb = pd.read_csv(f"{CL}/transfer_strategies.csv")
tt = ta.merge(tb, on="strategy", suffixes=("_prev", "_clean"))
tt.to_csv(f"{CL}/compare_prev_vs_clean_strategies.csv", index=False)

print("=== DIRECTION AUC, gb: Bybit OOS | prev overlapping | clean ===")
g = sk[sk.model == "gb"].pivot(index="H_min", columns="group", values=["dir_auc_prev", "dir_auc_clean"])
print(g.round(3).to_string())
print("\n=== 30/60 min, all models ===")
print(sk[sk.H_min.isin([30, 60])].round(3).to_string(index=False))
print("\n=== logistic direction AUC clean, range:", sk[sk.model == "lr"].dir_auc_clean.min().round(3), sk[sk.model == "lr"].dir_auc_clean.max().round(3))
print("\n=== HIT AUC gb ===")
print(sk[sk.model == "gb"].pivot(index="H_min", columns="group", values=["hit_auc_prev", "hit_auc_clean"]).round(3).to_string())
print("\n=== VOL R2 ===")
print(vv.round(3).to_string(index=False))
print("\n=== P&L fixed hold, P_gb / PL2_gb / ALL_gb, all signals and top10% (ev and score) ===")
x = pp[(pp.exit == "fixed_hold") & (pp.gate == "all") & (pp.model == "gb") & (pp.group.isin(["P", "PL2", "ALL"])) & (pp.top_frac.isin([0.10, 1.00]))]
print(x.drop(columns=["gate", "exit", "model"]).round(2).to_string(index=False))
print("\n=== P&L fixed hold ev top10% all groups/models (clean) ===")
y = pp[(pp.exit == "fixed_hold") & (pp.gate == "all") & (pp.select == "ev") & (pp.top_frac == 0.10)]
print(y.drop(columns=["gate", "exit", "select", "top_frac"]).round(2).to_string(index=False))
fh = pp[(pp.exit == "fixed_hold") & (pp.gate == "all") & (pp.select == "ev")]
print("\nnet-positive cells (fixed hold, ev): prev", int((fh.net_bps_prev > 0).sum()), "clean", int((fh.net_bps_clean > 0).sum()), "of", len(fh),
      "| clean cells with t>=2:", int((fh.t_daily_clean >= 2).sum()), "| max clean t:", fh.t_daily_clean.max().round(2))
print("best clean net cells:"); print(fh.sort_values("net_bps_clean", ascending=False).head(8).drop(columns=["gate", "exit"]).round(2).to_string(index=False))
print("\n=== sequential sims, net>0 in either run ===")
print(ss[(ss.net_bps_prev > 0) | (ss.net_bps_clean > 0)].round(2).to_string(index=False))
print("\n=== strategies ===")
print(tt.round(2).to_string(index=False))
print("\nshare dir AUC>0.52 prev:", round(float((sk.dir_auc_prev > 0.52).mean()), 3), "clean:", round(float((sk.dir_auc_clean > 0.52).mean()), 3))
