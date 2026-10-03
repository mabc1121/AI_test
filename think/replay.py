"""Recorded-market replay and trade measurement for Think.

The trade worker records every clean event released to the strategy (after the
protected integrity gate). Replay feeds those events to ScientificCore variants,
each with its own protected paper broker, in one pass. Decisions are throttled
(decide_every_ms), so replay is a fast *screen*; live shadow testing confirms.
"""
from __future__ import annotations
import dataclasses, gzip, json, math, random, re, time, zlib
from pathlib import Path
from typing import Any, Iterator

# ---------------- event encoding (shared with trade/worker.py) ----------------

def encode_event(e, decided: bool) -> str:
    return json.dumps({"k":e.kind,"t":int(e.recv_time_ms),"n":int(e.recv_time_ns),"x":e.exchange_time_ms,"p":e.payload,"c":e.protected_context,
                       "b":e.best_bid,"a":e.best_ask,"m":e.mid,"s":e.spread_bps,"q":e.sequence,"d":1 if decided else 0},default=str,separators=(",",":"))

def decode_event(row: dict, mod, symbol: str = "tBTCUSD"):
    integ=mod.MarketIntegrity(True,True,True,True,True,False,row.get("q"),0,0,0,None,row["t"],0,None)
    return mod.CleanMarketEvent(row["k"],"bitfinex_public_ws",symbol,"book" if row["k"].startswith("l3") else "trades",None,row.get("q"),
                                row["n"],row["t"],row.get("x"),row.get("p"),None,row.get("c") or {},row.get("b"),row.get("a"),row.get("m"),row.get("s"),integ)

def recording_files(directory: Path, start_ms: int = 0, end_ms: int | None = None) -> list[Path]:
    lo=time.strftime("%Y%m%d-%H",time.gmtime(start_ms/1000)); hi=time.strftime("%Y%m%d-%H",time.gmtime((end_ms or time.time()*1000)/1000))
    return sorted(p for p in directory.glob("*.jsonl.gz") if lo<=p.name[:11]<=hi) if directory.is_dir() else []

def iter_rows(directory, start_ms: int = 0, end_ms: int | None = None) -> Iterator[dict]:
    if isinstance(directory,Recordings): yield from directory.rows(start_ms,end_ms); return
    end=end_ms or int(time.time()*1000)
    for path in recording_files(directory,start_ms,end):
        try:
            with gzip.open(path,"rt",encoding="utf-8") as f:
                for line in f:
                    try: row=json.loads(line)
                    except ValueError: continue
                    if start_ms<=row["t"]<=end: yield row
        except (EOFError,OSError,zlib.error):  # file still being written, truncated tail, or a damaged block: keep what was read
            continue

TTI_KINDS={"book_snapshot":"l3_snapshot","book_update":"l3_update","checksum":"l3_checksum","trade_snapshot":"public_trade_snapshot","trade":"public_trade"}

def tti_rows(code_dir: str, data_dir: str, feed: str, start_ms: int, end_ms: int) -> Iterator[dict]:
    """tt_input's re-verified events in the replay row format, with the order lifecycle and top of book the
    protected layer attaches to each event (so the strategy sees exactly what it sees live)."""
    import sys
    if code_dir not in sys.path: sys.path.insert(0,code_dir)
    from tt_input.storage import read_events
    orders:dict[int,tuple[float,float]]={}; best={"b":None,"a":None}
    def top(side):
        vals=[p for p,a in orders.values() if (a>0)==(side=="b")]
        return (max(vals) if side=="b" else min(vals)) if vals else None
    def row(ev,kind,payload,ctx):
        b,a=best["b"],best["a"]; mid=(b+a)/2 if b is not None and a is not None else None
        return {"k":kind,"t":ev["t_recv"],"n":ev["t_recv"]*1_000_000,"x":ev.get("t_exch"),"p":payload,"c":ctx,"b":b,"a":a,"m":mid,
                "s":(a-b)/mid*1e4 if mid else None,"q":ev.get("seq"),"d":1 if ev["type"]=="checksum" else 0}
    for ev in read_events(Path(data_dir),feed,start_ms,end_ms):
        t=ev["type"]; d=ev.get("data") or {}; kind=TTI_KINDS.get(t)
        if kind is None:  # gap: state is rebuilt from the next snapshot
            orders.clear(); best.update(b=None,a=None); continue
        if t=="book_snapshot":
            orders.clear(); orders.update({int(i):(float(p),float(a)) for i,p,a in d["orders"]})
            best.update(b=top("b"),a=top("a")); yield row(ev,kind,d["orders"],{"order_count":len(orders)}); continue
        if t=="book_update":
            oid,price,amount,now=int(d["id"]),float(d["price"]),float(d["amount"]),ev["t_recv"]; old=orders.get(oid); life=[]
            if price==0:
                if old: orders.pop(oid); life.append({"kind":"remove","order_id":oid,"side":"bid" if old[1]>0 else "ask","price":old[0],"qty":abs(old[1]),"ts_ms":now})
            else:
                side="bid" if amount>0 else "ask"
                if old is None: life.append({"kind":"add","order_id":oid,"side":side,"price":price,"qty":abs(amount),"ts_ms":now})
                elif old[0]!=price or (old[1]>0)!=(amount>0):
                    life+=[{"kind":"remove","order_id":oid,"side":"bid" if old[1]>0 else "ask","price":old[0],"qty":abs(old[1]),"ts_ms":now},
                           {"kind":"add","order_id":oid,"side":side,"price":price,"qty":abs(amount),"ts_ms":now}]
                else:
                    delta=abs(amount)-abs(old[1])
                    if abs(delta)>1e-12: life.append({"kind":"resize_up" if delta>0 else "resize_down","order_id":oid,"side":side,"price":price,"qty":abs(delta),"new_size":abs(amount),"ts_ms":now})
                orders[oid]=(price,amount)
            if old and (old[0] in (best["b"],best["a"])) or price and ((amount>0 and (best["b"] is None or price>best["b"])) or (amount<0 and (best["a"] is None or price<best["a"]))):
                best.update(b=top("b"),a=top("a"))
            yield row(ev,kind,[oid,d["price"],d["amount"]],{"lifecycle":life}); continue
        if t=="trade": yield row(ev,kind,[d["id"],d["mts"],d["amount"],d["price"]],{}); continue
        if t=="trade_snapshot": yield row(ev,kind,d["trades"],{"trade_count":len(d["trades"])}); continue
        yield row(ev,kind,{"checksum":d["checksum"]},{})

def tti_first_ms(data_dir: str, feed: str) -> int | None:
    files=sorted(Path(data_dir,feed).glob("*/*/*/*.jsonl.gz"))
    for p in files:
        try:
            with gzip.open(p,"rt",encoding="utf-8") as f: return int(json.loads(f.readline())["r"])
        except (EOFError,OSError,zlib.error,ValueError,KeyError): continue
    return None

class Recordings:
    """Market history for Think: tt_input (re-verified originals) wherever it has data, the old recorder before that."""
    def __init__(self, old_dir: Path, tti: dict | None = None):
        self.old_dir=Path(old_dir); self.tti=tti if tti and Path(tti["data_dir"],tti["feed"]).is_dir() and Path(tti["code_dir"],"tt_input").is_dir() else None
    def cut(self) -> int | None:
        return tti_first_ms(self.tti["data_dir"],self.tti["feed"]) if self.tti else None
    def rows(self, start_ms: int, end_ms: int | None = None) -> Iterator[dict]:
        end=end_ms or int(time.time()*1000); cut=self.cut()
        if cut is None or start_ms<cut: yield from iter_rows(self.old_dir,start_ms,min(end,cut-1) if cut else end)
        if cut is not None and end>=cut: yield from tti_rows(self.tti["code_dir"],self.tti["data_dir"],self.tti["feed"],max(start_ms,cut),end)

def recorded_span(directory) -> dict:
    if isinstance(directory,Recordings):
        old=recorded_span(directory.old_dir); cut=directory.cut()
        tti_h=round((time.time()*1000-cut)/3.6e6,1) if cut else 0
        return {**old,"hours":max(old["hours"],int(tti_h)),"tt_input_hours":tti_h,"source":"tt_input + recorder" if cut else "recorder"}
    files=recording_files(directory)
    return {"files":len(files),"hours":len(files),"first":files[0].name[:11] if files else None,"last":files[-1].name[:11] if files else None,
            "bytes":sum(p.stat().st_size for p in files)}

# ---------------- trade measurement ----------------

def pair_trades(executions: list[dict]) -> list[dict]:
    """Turn ENTER/EXIT execution records into round-trip trades with net $ and bps."""
    out=[]; opened={}
    for x in executions:
        key=x.get("position_id") or x.get("symbol") or "_"   # contract 2.3: several positions open at once, keyed by id
        if str(x["action"]).startswith("ENTER"): opened[key]=x; continue
        open_=opened.pop(key,None) if x["action"]=="EXIT" else None
        if open_:
            notional=float(open_["fill_price"])*float(open_["size"]); side="long" if open_["action"]=="ENTER_LONG" else "short"
            reason=str(open_["reason"]); slot=re.match(r"(H\d+)_",reason)
            setup=slot.group(1) if slot else ("reversal" if "reversal" in reason else ("continuation" if "continuation" in reason else "other"))
            net=float(x["realized_pnl"]); fees=float(open_["fee_usd"])+float(x["fee_usd"])
            out.append({"entry_ms":int(open_["timestamp_ms"]),"exit_ms":int(x["timestamp_ms"]),"side":side,"setup":setup,"size":float(open_["size"]),
                        "entry":float(open_["fill_price"]),"exit":float(x["fill_price"]),"exit_reason":str(x["reason"]),"fees":fees,"net":net,
                        "notional":notional,"net_bps":net/notional*1e4 if notional else 0.0,"hold_s":(int(x["timestamp_ms"])-int(open_["timestamp_ms"]))/1000,
                        "slippage_bps":float(x.get("slippage_bps") or 0.0),"position_id":open_.get("position_id")})
    return out

def _group(trades, key):
    g={}
    for t in trades: g.setdefault(t[key],[]).append(t)
    return {k:{"n":len(v),"net":round(sum(x["net"] for x in v),2),"avg_bps":round(sum(x["net_bps"] for x in v)/len(v),2),
               "win":round(sum(x["net"]>0 for x in v)/len(v)*100,1)} for k,v in g.items()}

def stats(trades: list[dict], equity: float = 10_000.0) -> dict:
    """Trader-grade statistics over round trips (net of fees and slippage)."""
    n=len(trades)
    if not n: return {"n":0}
    r=[t["net_bps"] for t in trades]; nets=[t["net"] for t in trades]
    mean=sum(r)/n; sd=math.sqrt(sum((x-mean)**2 for x in r)/(n-1)) if n>1 else 0.0; se=sd/math.sqrt(n) if n>1 else float("inf")
    wins=[x for x in nets if x>0]; losses=[x for x in nets if x<0]
    gp=sum(wins); gl=-sum(losses)
    eq=peak=equity; dd=0.0; streak=maxstreak=0
    for x in nets:
        eq+=x; peak=max(peak,eq); dd=max(dd,(peak-eq)/peak)
        streak=streak+1 if x<0 else 0; maxstreak=max(maxstreak,streak)
    cur_dd=(peak-eq)/peak
    stress=[t["net"]-0.5*(t["fees"]+2e-4*t["notional"]) for t in trades]  # +50% fees and slippage
    days={}
    for t in trades: days.setdefault(t["exit_ms"]//86_400_000,0.0); days[t["exit_ms"]//86_400_000]+=t["net"]
    top2=sorted(nets,reverse=True)[:2]
    wr=len(wins)/n; aw=gp/len(wins) if wins else 0.0; al=gl/len(losses) if losses else 0.0
    return {"n":n,"net":round(sum(nets),2),"return_pct":round(sum(nets)/equity*100,3),"expectancy_bps":round(mean,2),"t_stat":round(mean/se,2) if se not in (0,float("inf")) else None,
            "conservative_bps":round(mean-se,2) if n>1 else None,"win_rate":round(wr*100,1),"payoff":round(aw/al,2) if al else None,
            "breakeven_win_rate":round(al/(aw+al)*100,1) if aw+al else None,"profit_factor":round(gp/gl,2) if gl else None,
            "fees":round(sum(t["fees"] for t in trades),2),"fees_share_of_gross":round(sum(t["fees"] for t in trades)/(gp+sum(t["fees"] for t in trades))*100,1) if gp else None,
            "max_dd_pct":round(dd*100,3),"dd_now_pct":round(cur_dd*100,3),"recovery":round(sum(nets)/(dd*equity),2) if dd else None,
            "max_losing_streak":maxstreak,"losing_streak_now":streak,"worst_trade":round(min(nets),2),"worst_vs_avg_loss":round(-min(nets)/al,2) if al and min(nets)<0 else None,
            "stress_net":round(sum(stress),2),"net_without_best2":round(sum(nets)-sum(x for x in top2 if x>0),2),
            "profitable_days_pct":round(sum(v>0 for v in days.values())/len(days)*100,1),"days":len(days),
            "avg_hold_min":round(sum(t["hold_s"] for t in trades)/n/60,1),
            "by_setup":_group(trades,"setup"),"by_side":_group(trades,"side"),"by_exit":_group(trades,"exit_reason")}

def prob_beats(a: list[dict], b: list[dict], start_ms: int, resamples: int = 2000, seed: int = 7) -> float | None:
    """Bootstrap P(candidate beats champion) on paired hourly net P&L over the same window."""
    hours={}
    for arm,trades in ((0,a),(1,b)):
        for t in trades:
            if t["exit_ms"]>=start_ms: hours.setdefault(t["exit_ms"]//3_600_000,[0.0,0.0])[arm]+=t["net"]
    diffs=[x[0]-x[1] for x in hours.values()]
    if len(diffs)<3: return None
    rnd=random.Random(seed); n=len(diffs)
    return round(sum(sum(rnd.choice(diffs) for _ in range(n))>0 for _ in range(resamples))/resamples,3)

def excursions(directory: Path, trades: list[dict]) -> list[dict]:
    """Best/worst open P&L (bps) during each trade, from recorded mids."""
    out=[]
    for t in trades:
        best=worst=0.0
        for row in iter_rows(directory,t["entry_ms"],t["exit_ms"]):
            m=row.get("m")
            if not m: continue
            move=(float(m)-t["entry"])/t["entry"]*1e4*(1 if t["side"]=="long" else -1)
            best=max(best,move); worst=min(worst,move)
        gross=(t["exit"]-t["entry"])/t["entry"]*1e4*(1 if t["side"]=="long" else -1)
        out.append({"entry_ms":t["entry_ms"],"mfe_bps":round(best,2),"mae_bps":round(worst,2),"captured_pct":round(gross/best*100,1) if best>0 else None})
    return out

# ---------------- multi-variant replay ----------------

def replay(mod_variants: list[tuple[str, Any, dict]], directory: Path, start_ms: int, end_ms: int,
           decide_every_ms: int = 1000, budget_s: float = 2700, base_mod=None) -> dict:
    """One pass over recorded events; each variant = (name, module, TradeConfig overrides)."""
    arms=[]
    for name,mod,overrides in mod_variants:
        fields={f.name for f in dataclasses.fields(mod.TradeConfig)}
        cfg=mod.TradeConfig(**{k:v for k,v in overrides.items() if k in fields})
        arms.append({"name":name,"core":mod.ScientificCore(cfg),"broker":mod._ProtectedPaperBroker(10_000.0),"last":0,"reasons":{},"error":None})
    base=base_mod or mod_variants[0][1]; started=time.monotonic(); events=0; complete=True; last_t=None
    for row in iter_rows(directory,start_ms,end_ms):
        if time.monotonic()-started>budget_s: complete=False; break
        ev=decode_event(row,base); events+=1; last_t=row["t"]
        for a in arms:
            if a["error"]: continue
            try:
                if row.get("d") and (row["t"]-a["last"]>=decide_every_ms or a["broker"].positions):
                    a["last"]=row["t"]; a["core"].config.assumed_equity_usd=max(0.0,a["broker"].equity(ev))
                    d=a["core"].decide(ev,list(a["broker"].positions.values())); a["broker"].execute(d,ev); a["reasons"][d.reason]=a["reasons"].get(d.reason,0)+1
                else: a["core"]._apply_event(ev)
            except Exception as exc: a["error"]=f"{type(exc).__name__}: {exc}"
    res={"events":events,"complete":complete,"seconds":round(time.monotonic()-started,1),"start_ms":start_ms,"end_ms":last_t or start_ms,"arms":{}}
    for a in arms:
        trades=pair_trades([dataclasses.asdict(x) for x in a["broker"].executions])
        res["arms"][a["name"]]={"error":a["error"],"trades":trades,"stats":stats(trades),"reasons":a["reasons"]}
    return res
