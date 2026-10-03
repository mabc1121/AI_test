"""dayaudit.py — per-day audit of the transferred direction skill (no fitting): is the higher Bitfinex AUC broad or a few trend days?"""
import os, sys, pickle, numpy as np, pandas as pd
from sklearn.metrics import roc_auc_score
HERE=os.path.dirname(os.path.abspath(__file__)); V2=os.path.abspath(os.path.join(HERE,"..","real","v2run"))
sys.path.insert(0,V2); sys.argv=["x"]; os.chdir(V2)
import research as R, config as C
class GB:
    def predict(self,X): return self.m.predict_proba(X)[:,self.pos] if self.pos is not None else np.zeros(len(X))
class LR:
    def _prep(self,X): return np.clip(np.nan_to_num((X-self.mu)/self.sd,nan=0.0,posinf=0.0,neginf=0.0),-10,10)
    def predict(self,X): return np.full(len(X),self.const) if self.const is not None else self.m.predict_proba(self._prep(X))[:,self.pos]
import __main__; __main__.GB=GB; __main__.LR=LR
by=R.load_bars(); fby=R.build_features(by)
os.chdir(HERE)
bf=pd.read_parquet("kit/out/bars_1m_v2.parquet"); fbf=R.build_features(bf)
P=[c for c in fby.columns if c.startswith(("ret_","lrv_","rvr_","rpos_","hl15","tod_","dow","mins_to"))]
L2=[c for c in fby.columns if c.startswith("l2_")]
def prep(b,f):
    sig=f["sigma1"].values; q=(b["n_rows"].values>=C.MIN_ROWS_PER_MIN)&np.isfinite(f["lrv_1440" if "lrv_1440" in f else "lrv_240"].values)&np.isfinite(sig); return sig,q
sig_bf,q_bf=prep(bf,fbf); sig_by,q_by=prep(by,fby)
day_bf=bf.index.floor("D").astype(str).to_numpy(); day_by=by.index.floor("D").astype(str).to_numpy()
rows=[]; mom=[]
for H in (30,60,120,240):
    m=pickle.load(open(os.path.join(os.environ.get("MODELS_DIR","models_bybit"),f"H{H}.pkl"),"rb"))
    lab=R.triple_barrier(bf["close"].values,bf["high"].values,bf["low"].values,sig_bf,H,C.BARRIER_K)
    y=(lab["fwd"]>0).astype(float); ok=q_bf&np.isfinite(lab["fwd"])
    G=os.environ.get("GROUP","P"); cols={"P":P,"PL2":P+L2,"ALL":P+[c for c in fby.columns if c.startswith(("timb_","volz_","nlarge_","ofi_","netadd_"))]+[c for c in fby.columns if c.startswith(("spread_","bimb_","micro_","dimb","ldtot","dtot1_chg","l10_"))]+L2}[G]; s=m["dir"][f"{G}_gb"].predict(fbf[cols].values)-m["prior_up"]
    for d in sorted(set(day_bf[ok])):
        i=ok&(day_bf==d)
        if y[i].min()==y[i].max() or i.sum()<60: continue
        rows.append(dict(H=H,day=d,n=int(i.sum()),up_share=float(y[i].mean()),auc_P_gb=roc_auc_score(y[i],s[i]),
                         day_ret_bps=float(np.log(bf["close"][i].iloc[-1]/bf["close"][i].iloc[0])*1e4)))
    # raw momentum features: AUC of each ret_* feature against the same label, Bitfinex vs Bybit
    labb=R.triple_barrier(by["close"].values,by["high"].values,by["low"].values,sig_by,H,C.BARRIER_K)
    yb=(labb["fwd"]>0).astype(float); okb=q_by&np.isfinite(labb["fwd"])
    for c in [c for c in P if c.startswith("ret_")]:
        xf=fbf[c].values; xb=fby[c].values; jf=ok&np.isfinite(xf); jb=okb&np.isfinite(xb)
        mom.append(dict(H=H,feature=c,auc_bitfinex=roc_auc_score(y[jf],xf[jf]),auc_bybit=roc_auc_score(yb[jb],xb[jb])))
D=pd.DataFrame(rows); M=pd.DataFrame(mom)
pd.set_option("display.width",250); pd.set_option("display.max_rows",200)
print(D.round(3).to_string(index=False))
print("\nper-H: days with AUC>0.5, median AUC, pooled AUC (from transfer_skill), sign agreement of day_ret and up_share")
for H,g in D.groupby("H"): print(H, f"{int((g.auc_P_gb>0.5).sum())}/{len(g)} days>0.5  median {g.auc_P_gb.median():.3f}  min {g.auc_P_gb.min():.3f} max {g.auc_P_gb.max():.3f}  up_share range {g.up_share.min():.2f}-{g.up_share.max():.2f}")
print("\nraw momentum feature AUC (no model):"); print(M.round(3).to_string(index=False))
OUT=os.environ.get("OUT_DIR","."); D.to_csv(os.path.join(OUT,f"dayaudit_days_{os.environ.get('GROUP','P')}.csv"),index=False); M.to_csv(os.path.join(OUT,"dayaudit_momentum.csv"),index=False)
