"""Think: the autonomous trade scientist.

Cycle (every interval_hours, or on demand):
  1. Detect champion changes (promotions/manual edits) and run the rollback guard.
  2. Build a deterministic analysis pack (stats, block funnel, excursions) - numbers, not guesses.
  3. Evaluate live shadow candidates against the champion over the same window (tiered criteria).
  4. Ask the AI for 1-3 experiments grounded in the pack and the lab notebook.
  5. Screen them on recorded market data (replay), then start passing ones as live shadows.
PASS candidates become a promotion proposal in Manage > Actions; nothing goes live without approval.
"""
from __future__ import annotations
import dataclasses, hashlib, itertools, json, math, os, re, sys, time
from pathlib import Path
from typing import Any
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from core.common import AgentProvider, Health, Store, default_agent_settings, import_module, load_config, resolve
from think.replay import Recordings, excursions, iter_rows, pair_trades, prob_beats, recorded_span, replay, stats

TT_INPUT_DEFAULTS={"data_dir":"/srv/tt-input","code_dir":"/opt/tt-input","feed":"bitfinex.tBTCUSD"}
MARKET_TOOLS=[
  {"type":"function","name":"market_candles","description":"Price bars from the verified market recording for any past window (UTC). start: ISO time like 2026-09-24T10:00 or relative like -6h. Returns at most 360 bars with open/high/low/close, volume, buy/sell volume, trade count and average spread (bps).",
   "parameters":{"type":"object","properties":{"start":{"type":"string"},"minutes":{"type":"integer"},"bar_seconds":{"type":"integer","description":"60, 300, 900 or 3600"}},"required":["start","minutes","bar_seconds"],"additionalProperties":False}},
  {"type":"function","name":"find_moves","description":"Search the recording for every price move of at least min_move_bps within window_minutes (breakouts, flushes, squeezes). Returns start time, direction, size and duration, largest first (max 30).",
   "parameters":{"type":"object","properties":{"hours_back":{"type":"integer","description":"max 24"},"min_move_bps":{"type":"number"},"window_minutes":{"type":"integer"}},"required":["hours_back","min_move_bps","window_minutes"],"additionalProperties":False}}]

def _parse_start(s: str) -> int:
    import calendar
    s=str(s).strip(); now=int(time.time()*1000)
    if s.startswith("-"): v=float(s[1:-1]) if s[-1] in "smhd" else float(s[1:]); return now-int(v*{"s":1,"m":60,"h":3600,"d":86400}.get(s[-1],1)*1000)
    return int(calendar.timegm(time.strptime(s[:16].replace(" ","T"),"%Y-%m-%dT%H:%M"))*1000)

LOCKED_PARAMS={"assumed_equity_usd","paper_fee_bps_each_side","paper_slippage_bps_each_side"}
EDITABLE_BEGIN="# EDITABLE:SCIENTIFIC_WORKSPACE BEGIN"; EDITABLE_END="# EDITABLE:SCIENTIFIC_WORKSPACE END"
CONTROL_DEFAULTS={"paused":False,"interval_hours":4.0,"pool_slots":5,"max_llm_calls_per_day":8,"replay_budget_minutes":45,"shadow_max_days":7,"run_now":False}
SYSTEM=("You are Think, the research scientist for a paper-only BTCUSD strategy (trade/trade.py). Your goal is a genuine, "
        "robust improvement in net edge per trade after fees and slippage - not more activity. Work like a scientist: "
        "read the analysis pack and the lab notebook, identify ONE concrete weakness backed by a number in the pack, state a testable "
        "hypothesis, and propose the smallest change that tests it. Never repeat an experiment the notebook already rejected unless you "
        "explain what is different. Prefer parameter experiments (code searches the values on recorded data); use a code edit only when "
        "the logic itself is the problem, and only inside the EDITABLE:SCIENTIFIC_WORKSPACE section. Fees, slippage, accounting and "
        "protected blocks are off limits. You may read trade/trade.py with read_file/search_files, and study the verified market recording with "
        "market_candles (any past window) and find_moves (locate every move of a given size) to check a hypothesis against what the market actually did. Finish by calling submit_experiments "
        "exactly once with 1-3 experiments.")
SUBMIT_TOOL={"type":"function","name":"submit_experiments","strict":False,
  "description":"Submit 1-3 experiments. kind=params: 'params' maps up to 2 TradeConfig names to 2-4 candidate values each. kind=code: exact 'old_text' (must occur once, inside the editable scientific workspace) and 'new_text'.",
  "parameters":{"type":"object","properties":{"experiments":{"type":"array","maxItems":3,"items":{"type":"object","properties":{
      "hypothesis":{"type":"string"},"evidence":{"type":"string","description":"The analysis-pack numbers that motivate it"},
      "expected_effect":{"type":"string","description":"Which metric should move, and in which direction"},
      "kind":{"type":"string","enum":["params","code"]},"params":{"type":"object"},"old_text":{"type":"string"},"new_text":{"type":"string"}},
      "required":["hypothesis","evidence","expected_effect","kind"]}}},"required":["experiments"]}}

def _sha(b: bytes) -> str: return hashlib.sha256(b).hexdigest()
def _day(ts: float) -> str: return time.strftime("%Y-%m-%d",time.gmtime(ts))

def set_config_defaults(source: str, overrides: dict) -> str:
    """Rewrite TradeConfig default values in trade.py source (used to promote a params candidate)."""
    start=source.index("class TradeConfig"); end=source.index("\nclass ",start+1); block=source[start:end]
    for key,val in overrides.items():
        pat=re.compile(rf"^(\s+{re.escape(key)}\s*:\s*([A-Za-z]+)\s*=\s*)([^\n#]+?)(\s*(?:#.*)?)$",re.M)
        m=pat.search(block)
        if not m: raise ValueError(f"TradeConfig has no field {key}")
        typ=m.group(2); v=int(round(float(val))) if typ=="int" else (float(val) if typ=="float" else val)
        block=block[:m.start(3)]+repr(v)+block[m.end(3):]
    return source[:start]+block+source[end:]

class ThinkEngine:
    def __init__(self, root: Path=ROOT):
        self.root=root; self.cfg=load_config(root); self.store=Store(resolve(root,self.cfg["runtime"]["database"]))
        self.think_dir=resolve(root,"runtime/think"); self.trade_file=resolve(root,self.cfg["trade"]["file"])
        self.rec_dir=Recordings(resolve(root,"runtime/recordings"),{**TT_INPUT_DEFAULTS,**(self.cfg.get("input") or {})})
        if self.store.get("settings","think_agent") is None: self.store.put("settings","think_agent",default_agent_settings(self.cfg,"think"))

    # ---------------- state / control ----------------
    def control(self) -> dict:
        c=dict(CONTROL_DEFAULTS); c.update({k:v for k,v in self.cfg.get("think",{}).items() if k in c})
        c.update(self.store.get("think","control",{}) or {}); return c
    def set_control(self, values: dict) -> dict:
        c=self.store.get("think","control",{}) or {}
        for k,v in values.items():
            if k not in CONTROL_DEFAULTS: raise ValueError(f"unknown control {k}")
            if isinstance(CONTROL_DEFAULTS[k],bool): v=v in (True,"true","1","on",1)
            else:
                v=type(CONTROL_DEFAULTS[k])(v)
                if v<0: raise ValueError(f"{k} must be >= 0")
            c[k]=v
        self.store.put("think","control",c); self.store.audit("think","control",c); return self.control()
    def state(self) -> dict: return self.store.get("think","state",{}) or {}
    def _set_state(self, **kw):
        s=self.state(); s.update(kw); self.store.put("think","state",s)
    def pool(self) -> list[dict]: return self.store.get("think","pool",[]) or []
    def _save_pool(self, pool: list[dict]): self.store.put("think","pool",pool)
    def llm_calls_today(self) -> int: return int((self.store.get("think","usage",{}) or {}).get(_day(time.time()),0))
    def _count_llm(self):
        u=self.store.get("think","usage",{}) or {}; d=_day(time.time()); u[d]=int(u.get(d,0))+1; self.store.put("think","usage",{k:u[k] for k in sorted(u)[-30:]})
    def status(self) -> dict:
        s=self.state(); c=self.control(); settings=self.store.get("settings","think_agent",{}) or {}
        state="PAUSED" if c["paused"] else ("ERROR" if s.get("last_error") else ("RUNNING" if s.get("running") else "IDLE"))
        return {"state":state,"last_cycle":s.get("last_cycle"),"next_cycle":s.get("next_cycle"),"last_error":s.get("last_error"),
                "llm_calls_today":self.llm_calls_today(),"llm_cap":c["max_llm_calls_per_day"],"provider":settings.get("provider","mock"),
                "pool":[p for p in self.pool() if p.get("status") in ("SHADOW","PASS_PENDING")],"champion":self.store.get("think","champion",{}),
                "recordings":recorded_span(self.rec_dir),"control":c}

    # ---------------- notebook ----------------
    def note(self, event: str, **data):
        self.think_dir.mkdir(parents=True,exist_ok=True)
        with open(self.think_dir/"notebook.jsonl","a",encoding="utf-8") as f: f.write(json.dumps({"ts":time.time(),"type":event,**data},default=str)+"\n")
    def notebook(self, n: int=50) -> list[dict]:
        p=self.think_dir/"notebook.jsonl"
        if not p.is_file(): return []
        rows=[]
        for line in p.read_text(encoding="utf-8").splitlines()[-n:]:
            try: rows.append(json.loads(line))
            except ValueError: pass
        return rows

    # ---------------- measurement ----------------
    def champion_trades(self) -> list[dict]:
        cp=self.store.get("trade_state","checkpoint") or {}
        return pair_trades(cp.get("broker",{}).get("executions",[]))
    def shadow_trades(self, sid: str) -> list[dict]:
        rec=self.store.get("shadow",sid) or {}
        return pair_trades(rec.get("broker",{}).get("executions",[]))
    def funnel(self, hours: int) -> dict:
        f=self.store.get("trade","funnel",{}) or {}; out={}
        for k in sorted(f)[-hours:]:
            for r,n in f[k].items(): out[r]=out.get(r,0)+n
        total=sum(out.values()) or 1
        return {r:{"n":n,"pct":round(n/total*100,1)} for r,n in sorted(out.items(),key=lambda x:-x[1])}
    def champion_module(self):
        return import_module(self.trade_file,"think_champion")
    def tunables(self) -> dict:
        mod=self.champion_module(); cfg=mod.TradeConfig()
        return {f.name:getattr(cfg,f.name) for f in dataclasses.fields(cfg) if f.name not in LOCKED_PARAMS}
    def analysis_pack(self) -> dict:
        trades=self.champion_trades(); snap=self.store.get("trade","snapshot",{}) or {}
        exc=excursions(self.rec_dir,trades[-10:]) if trades else []
        return {"generated_at":time.time(),"champion":{"stats":stats(trades),"recent_trades":trades[-10:],"excursions":exc},
                "block_funnel_24h":self.funnel(24),"block_funnel_7d":self.funnel(168),
                "market_now":snap.get("custom_metrics",{}),"recordings":recorded_span(self.rec_dir),"config":self.tunables()}

    # ---------------- market search (for the AI) ----------------
    def _hour_bars(self, h: int, end: int) -> list[dict]:
        bars:dict[int,dict]={}; spreads:dict[int,list]={}
        for r in iter_rows(self.rec_dir,h,end-1):
            if r["k"]=="public_trade" and isinstance(r.get("p"),list) and len(r["p"])>=4:
                ts=int(r["p"][1]) or r["t"]; px=float(r["p"][3]); amt=float(r["p"][2]); b=ts//60000*60000
                if not h<=b<end: continue
                x=bars.setdefault(b,{"t":b,"o":px,"h":px,"l":px,"c":px,"v":0.0,"buy":0.0,"sell":0.0,"n":0})
                x["h"]=max(x["h"],px); x["l"]=min(x["l"],px); x["c"]=px; x["v"]+=abs(amt); x["n"]+=1; x["buy" if amt>0 else "sell"]+=abs(amt)
            elif r.get("d") and r.get("s") is not None: spreads.setdefault(r["t"]//60000*60000,[]).append(float(r["s"]))
        for b,x in bars.items(): sp=spreads.get(b); x["spread"]=round(sum(sp)/len(sp),3) if sp else None
        return [bars[k] for k in sorted(bars)]
    def bars(self, start_ms: int, end_ms: int) -> list[dict]:
        """1-minute bars from the recordings; complete hours are cached so searches get fast."""
        out=[]; h=start_ms//3_600_000*3_600_000; now=time.time()*1000
        while h<end_ms:
            he=h+3_600_000; cache=self.think_dir/"bars"/f"{h}.json"
            if cache.is_file(): hb=json.loads(cache.read_text())
            else:
                hb=self._hour_bars(h,min(he,end_ms))
                if he<now-120_000 and he<=end_ms: cache.parent.mkdir(parents=True,exist_ok=True); cache.write_text(json.dumps(hb))
            out+=[b for b in hb if start_ms<=b["t"]<end_ms]; h=he
        return out
    def market_candles(self, start: str, minutes: int, bar_seconds: int=60) -> list[dict]:
        s=_parse_start(start); e=s+min(int(minutes),24*60)*60_000; step=max(60,int(bar_seconds))//60*60_000; out:dict[int,dict]={}
        for b in self.bars(s,e):
            k=b["t"]//step*step; x=out.get(k)
            if x is None: out[k]={**b,"t":k,"time":time.strftime("%Y-%m-%d %H:%M",time.gmtime(k/1000))}; continue
            x["h"]=max(x["h"],b["h"]); x["l"]=min(x["l"],b["l"]); x["c"]=b["c"]; x["v"]+=b["v"]; x["buy"]+=b["buy"]; x["sell"]+=b["sell"]; x["n"]+=b["n"]
        return [{k:(round(v,6) if isinstance(v,float) else v) for k,v in out[t].items()} for t in sorted(out)][:360]
    def find_moves(self, hours_back: int, min_move_bps: float, window_minutes: int) -> list[dict]:
        now=int(time.time()*1000); bars=self.bars(now-min(int(hours_back),24)*3_600_000,now); hits=[]; i=0; w=max(1,int(window_minutes))
        while i<len(bars):
            o=bars[i]["o"]; best=None
            for j in range(i,min(len(bars),i+w)):
                for px,d in ((bars[j]["h"],"up"),(bars[j]["l"],"down")):
                    mv=(px-o)/o*1e4
                    if abs(mv)>=min_move_bps and (best is None or abs(mv)>abs(best[0])): best=(mv,d,j)
            if best:
                hits.append({"start":time.strftime("%Y-%m-%d %H:%M",time.gmtime(bars[i]["t"]/1000)),"direction":best[1],"move_bps":round(best[0],1),
                             "minutes":best[2]-i+1,"from":o,"to":bars[best[2]]["h" if best[1]=="up" else "l"]}); i=best[2]+1
            else: i+=1
        return sorted(hits,key=lambda x:-abs(x["move_bps"]))[:30]

    # ---------------- evaluation ----------------
    def evaluate(self, entry: dict) -> dict:
        """Tiered, trader-grade criteria for one shadow candidate vs the champion over the same window."""
        rec=self.store.get("shadow",entry["id"]) or {}; start=int(entry.get("started_ms") or 0)
        cand=self.shadow_trades(entry["id"]); champ=[t for t in self.champion_trades() if t["entry_ms"]>=start]
        s=stats(cand); c=stats(champ); age_h=(time.time()*1000-start)/3.6e6 if start else 0.0
        tested=int(self.store.get("think","tested",0) or 0); t_req=round(min(2.0,1.0+0.25*math.log2(1+tested)),2)
        p=prob_beats(cand,champ,start)
        def chk(name,ok,value,need): return {"name":name,"ok":bool(ok),"value":value,"need":need}
        n=s.get("n",0)
        checks=[chk("No worker error",not rec.get("error"),rec.get("error") or "none","none"),
                chk("Running ≥ 24 h",age_h>=24,f"{age_h:.1f} h","24 h"),
                chk("Candidate trades ≥ 30",n>=30,n,30),
                chk("Net edge ≥ +2 bps/trade",n and s["expectancy_bps"]>=2,s.get("expectancy_bps"),"+2.0"),
                chk(f"t-stat ≥ {t_req} ({tested} tested)",n>1 and (s.get("t_stat") or 0)>=t_req,s.get("t_stat"),t_req),
                chk("P(beats champion) ≥ 75%",p is not None and p>=0.75,None if p is None else f"{p*100:.0f}%","75%"),
                chk("Profit factor ≥ 1.2",(s.get("profit_factor") or 0)>=1.2,s.get("profit_factor"),1.2),
                chk("Profitable at 1.5× costs",n and s["stress_net"]>0,s.get("stress_net"),"> 0"),
                chk("Drawdown ≤ 1.25× champion",n and s["max_dd_pct"]<=max(1.25*(c.get("max_dd_pct") or 0),0.25),s.get("max_dd_pct"),round(max(1.25*(c.get("max_dd_pct") or 0),0.25),3)),
                chk("Positive without best 2 trades",n and s["net_without_best2"]>0,s.get("net_without_best2"),"> 0"),
                chk("Losing streak ≤ 8",n and s["max_losing_streak"]<=8,s.get("max_losing_streak"),8)]
        goal=[chk("Trades ≥ 200 over ≥ 14 days",n>=200 and s.get("days",0)>=14,f"{n} / {s.get('days',0)}d","200 / 14d"),
              chk("t-stat ≥ 2",(s.get("t_stat") or 0)>=2,s.get("t_stat"),2),chk("Profit factor ≥ 1.4",(s.get("profit_factor") or 0)>=1.4,s.get("profit_factor"),1.4),
              chk("Profitable days ≥ 60%",(s.get("profitable_days_pct") or 0)>=60,s.get("profitable_days_pct"),60),
              chk("Max drawdown ≤ 2%",n and s["max_dd_pct"]<=2,s.get("max_dd_pct"),2),chk("Return ÷ drawdown ≥ 3",(s.get("recovery") or 0)>=3,s.get("recovery"),3)]
        passed=all(x["ok"] for x in checks); failing=[x["name"] for x in checks if not x["ok"]]
        # clearly losing once it has its pre-registered trade count (30 if none): stop early instead of holding a slot for days
        min_n=int((entry.get("preregistration") or {}).get("min_trades") or 30)
        # ...or sooner when the evidence is already overwhelming (15+ trades, t <= -3)
        losing=(s.get("expectancy_bps") or 0)<=-5 and ((n>=min_n and (s.get("t_stat") or 0)<=-2) or (n>=15 and (s.get("t_stat") or 0)<=-3))
        verdict="PASS" if passed else ("FAIL" if rec.get("error") or losing else "WAITING")
        why="all PASS rules met" if passed else (f"clearly losing after {n} trades: {s.get('expectancy_bps')} bps/trade, t {s.get('t_stat')}" if losing and not rec.get("error") else "failing: "+", ".join(failing))
        return {"verdict":verdict,"why":why,"checks":checks,"goal":goal,
                "candidate":s,"champion_same_window":c,"p_beats":p,"age_h":round(age_h,1)}

    # ---------------- champion lifecycle ----------------
    def _track_champion(self):
        text=self.trade_file.read_bytes(); sha=_sha(text); ch=self.store.get("think","champion",{}) or {}
        if ch.get("sha")==sha: return ch
        self.think_dir.mkdir(parents=True,exist_ok=True); (self.think_dir/"champions").mkdir(exist_ok=True)
        (self.think_dir/"champions"/f"{sha[:12]}.py").write_bytes(text)
        pool=self.pool(); promoted=next((p for p in pool if p.get("status")=="PASS_PENDING" and p.get("promote_sha")==sha),None)
        new={"sha":sha,"since_ms":int(time.time()*1000),"id":promoted["id"] if promoted else ("baseline" if not ch else f"manual-{sha[:8]}"),
             "previous_sha":ch.get("sha"),"guard_dd_pct":(promoted or {}).get("shadow_dd_pct"),"rolled_back":False}
        for p in pool:
            if p.get("status") in ("SHADOW","PASS_PENDING"):
                p["status"]="PROMOTED" if p is promoted else "RETIRED"; p["verdict"]=p.get("verdict") if p is promoted else "champion changed; comparison no longer valid"
        self._save_pool(pool); self.store.put("think","champion",new)
        if ch: self.note("champion_changed",champion=new["id"],sha=sha[:12],previous=(ch.get("sha") or "")[:12])
        return new
    def _rollback_guard(self, ch: dict):
        if not ch.get("previous_sha") or ch.get("rolled_back") or not ch.get("since_ms"): return
        trades=[t for t in self.champion_trades() if t["entry_ms"]>=ch["since_ms"]]
        if len(trades)<20: return
        s=stats(trades); dd_limit=max(1.5*float(ch.get("guard_dd_pct") or 0),0.5)
        bad=(s["expectancy_bps"]<-2 and (s.get("t_stat") or 0)<-1) or s["max_dd_pct"]>dd_limit
        if not bad: return
        prev=self.think_dir/"champions"/f"{ch['previous_sha'][:12]}.py"
        if not prev.is_file(): return
        from manage.manage import ManageEngine
        m=ManageEngine(self.root); cur=self.trade_file.read_text(encoding="utf-8")
        aid=m.propose_file_edit("trade/trade.py",cur,prev.read_text(encoding="utf-8"),f"AUTO-ROLLBACK {ch['id']}: edge {s['expectancy_bps']} bps, drawdown {s['max_dd_pct']}% (limit {dd_limit}%)")
        result=m.approve_action(aid); ch["rolled_back"]=bool(result.get("ok")); self.store.put("think","champion",ch)
        self.note("rollback",champion=ch["id"],ok=result.get("ok"),error=result.get("error"),stats={k:s[k] for k in ("n","expectancy_bps","max_dd_pct")})

    def _propose_promotion(self, entry: dict, ev: dict):
        from manage.manage import ManageEngine
        cur=self.trade_file.read_text(encoding="utf-8")
        if entry["kind"]=="params": new=set_config_defaults(cur,entry.get("overrides") or {})
        else:
            if entry.get("base_sha")!=_sha(cur.encode()): entry["status"]="RETIRED"; entry["verdict"]="champion changed since candidate was built"; return
            new=Path(entry["source_path"]).read_text(encoding="utf-8")
        m=ManageEngine(self.root)
        aid=m.propose_file_edit("trade/trade.py",cur,new,f"PROMOTE {entry['id']}: {entry['hypothesis'][:300]} | edge {ev['candidate'].get('expectancy_bps')} bps, t {ev['candidate'].get('t_stat')}, P(beats) {ev.get('p_beats')}")
        entry.update(status="PASS_PENDING",action_id=aid,promote_sha=_sha(new.encode()),shadow_dd_pct=ev["candidate"].get("max_dd_pct"))
        self.note("promotion_proposed",candidate=entry["id"],action_id=aid,verdict=ev["why"])

    # ---------------- experiments ----------------
    def _ask(self, pack: dict) -> list[dict]:
        settings=self.store.get("settings","think_agent",default_agent_settings(self.cfg,"think"))
        provider=AgentProvider(settings); captured=[]
        from manage.manage import ManageEngine
        m=ManageEngine(self.root)
        def handler(name,args):
            if name=="read_file": return m.read_file(args["path"],args.get("start_line",1),args.get("max_lines",300))
            if name=="search_files": return m.search_files(args["pattern"],args.get("path","trade"))
            if name=="market_candles": return self.market_candles(args["start"],args.get("minutes",60),args.get("bar_seconds",60))
            if name=="find_moves": return self.find_moves(args.get("hours_back",24),float(args["min_move_bps"]),args.get("window_minutes",10))
            if name=="submit_experiments": captured.extend(args.get("experiments") or []); return {"ok":True,"received":len(captured)}
            raise ValueError(name)
        from manage.manage import TOOLS as MT
        tools=[t for t in MT if t["name"] in ("read_file","search_files")]+MARKET_TOOLS+[SUBMIT_TOOL]
        notebook=[{k:v for k,v in r.items() if k in ("type","candidate","hypothesis","verdict","why","lesson","params","kind")} for r in self.notebook(40) if r["type"] in ("experiment","verdict","screen")]
        prompt=json.dumps({"analysis_pack":pack,"lab_notebook":notebook,"tunable_params":pack["config"],
                           "free_shadow_slots":self._free_slots()},default=str)[:60000]
        self._count_llm()
        provider.run(prompt,system=SYSTEM,allow_web=False,tools=tools,handler=handler)
        if provider.fallback_error: self.note("fallback",why=f"answered by {provider.used}; primary failed: {provider.fallback_error}")
        return captured[:3]
    def _free_slots(self) -> int:
        return max(0,int(self.control()["pool_slots"])-sum(p.get("status") in ("SHADOW","PASS_PENDING") for p in self.pool()))
    def apply_retire_requests(self) -> int:
        """Retirements approved in Manage > Actions are queued (one row per action) and applied here, in the Think
        worker, so the shadow pool is only ever changed by one process."""
        with self.store.connect() as db: keys=[r[0] for r in db.execute("SELECT key FROM kv WHERE scope='think_retire'").fetchall()]
        if not keys: return 0
        pool=self.pool(); done=0
        for k in keys:
            req=self.store.get("think_retire",k) or {}
            e=next((p for p in pool if p["id"]==req.get("candidate") and p.get("status") in ("SHADOW","PASS_PENDING")),None)
            if e is not None:
                e.update(status="RETIRED",verdict="RETIRED_BY_USER",why=req.get("reason","")); done+=1
                self.note("verdict",candidate=e["id"],hypothesis=e.get("hypothesis"),verdict="RETIRED_BY_USER",why=f"approved action #{k}: {req.get('reason','')}")
        self._save_pool(pool)
        for k in keys: self.store.delete("think_retire",k)
        return done
    def _build_variants(self, experiments: list[dict], champ_mod, tun: dict):
        variants=[("baseline",champ_mod,{})]; built=[]; cur=self.trade_file.read_text(encoding="utf-8")
        for i,e in enumerate(experiments):
            eid=f"x{int(time.time())%10**6:06d}{i}"; e["id"]=eid
            try:
                if e["kind"]=="params":
                    def differs(x,k):  # a value equal to the champion's setting would test nothing
                        try: return float(x)!=float(tun[k])
                        except (TypeError,ValueError): return x!=tun[k]
                    grid={k:[x for x in v if differs(x,k)][:4] for k,v in (e.get("params") or {}).items() if k in tun and isinstance(v,list)}
                    grid=dict(list((k,v) for k,v in grid.items() if v)[:2])
                    if not grid: raise ValueError("no proposed value differs from the champion's current settings")
                    for j,combo in enumerate(itertools.product(*grid.values())):
                        if len(variants)>=13: break
                        variants.append((f"{eid}.{j}",champ_mod,{**dict(zip(grid,combo))}))
                else:
                    old,new=e.get("old_text") or "",e.get("new_text") or ""
                    lo,hi=cur.index(EDITABLE_BEGIN),cur.index(EDITABLE_END); pos=cur.find(old)
                    if not old or cur.count(old)!=1 or not lo<pos<hi or not lo<pos+len(old)<hi: raise ValueError("old_text must occur once inside the editable scientific workspace")
                    src=cur.replace(old,new,1)
                    if '"paper_only": True' not in src: raise ValueError("must stay paper-only")
                    d=self.think_dir/"candidates"/eid; d.mkdir(parents=True,exist_ok=True); path=d/"trade.py"; path.write_text(src,encoding="utf-8")
                    import subprocess
                    proc=subprocess.run([sys.executable,str(self.root/"trade"/"validate_trade_contract.py"),str(path),"--canonical",str(self.trade_file),"--run-self-test"],capture_output=True,text=True,timeout=120)
                    out=json.loads(proc.stdout or "{}")
                    if proc.returncode or not out.get("ok") or not out.get("self_test",{}).get("ok"): raise ValueError(f"contract validation failed: {out.get('errors') or proc.stderr[-500:]}")
                    e.update(source_path=str(path),source_sha=_sha(src.encode()),base_sha=_sha(cur.encode()))
                    variants.append((eid,import_module(path,f"cand_{eid}"),{}))
                built.append(e)
            except Exception as exc:
                self.note("experiment",candidate=eid,hypothesis=e.get("hypothesis"),kind=e.get("kind"),verdict="REJECTED",why=f"{type(exc).__name__}: {exc}")
        return variants,built
    def _screen(self, experiments: list[dict]) -> list[dict]:
        champ_mod=self.champion_module(); tun=self.tunables(); variants,built=self._build_variants(experiments,champ_mod,tun)
        if not built: return []
        span=recorded_span(self.rec_dir); now=int(time.time()*1000)
        if span["hours"]<6:
            for e in built: e["screen"]={"status":"UNSCREENED","why":f"only {span['hours']} h of recordings"}; e["overrides"]={k:v[0] for k,v in (e.get("params") or {}).items() if k in tun and isinstance(v,list) and v}
            return built
        days=min(7,span["hours"]/24)
        res=replay(variants,self.rec_dir,now-int(days*86400_000),now,budget_s=float(self.control()["replay_budget_minutes"])*60,base_mod=champ_mod)
        base=res["arms"]["baseline"]["stats"]; bcons=base.get("conservative_bps") if base.get("n",0)>1 else None
        for e in built:
            arms={k:v for k,v in res["arms"].items() if k==e["id"] or k.startswith(e["id"]+".")}
            best_name,best=max(arms.items(),key=lambda kv:(kv[1]["stats"].get("conservative_bps") or -1e9,kv[1]["stats"].get("net",-1e9)))
            s=best["stats"]; n=s.get("n",0)
            if best["error"]: status,why="FAIL",best["error"]
            elif n<3: status,why="INCONCLUSIVE",f"{n} replay trades"
            elif s["net"]>0 and (bcons is None or (s.get("conservative_bps") or -1e9)>=bcons): status,why="PASS",f"cons. edge {s.get('conservative_bps')} vs baseline {bcons}"
            else: status,why="FAIL",f"cons. edge {s.get('conservative_bps')} vs baseline {bcons}, net {s['net']}"
            if e["kind"]=="params": e["overrides"]=next(ov for name,_,ov in variants if name==best_name)
            e["screen"]={"status":status,"why":why,"best":best_name,"stats":{k:s.get(k) for k in ("n","net","expectancy_bps","conservative_bps","win_rate","profit_factor","max_dd_pct")},
                         "baseline":{k:base.get(k) for k in ("n","net","expectancy_bps","conservative_bps")},"days":round(days,1),"events":res["events"],"complete":res["complete"],"seconds":res["seconds"]}
            self.note("screen",candidate=e["id"],hypothesis=e["hypothesis"],kind=e["kind"],params=e.get("overrides"),verdict=status,why=why)
        return built

    # ---------------- the cycle ----------------
    def run_cycle(self, force: bool=False) -> dict:
        c=self.control()
        if c["paused"] and not force: return {"ran":False,"reason":"paused"}
        self._set_state(running=True)
        try:
            ch=self._track_champion(); self._rollback_guard(ch)
            pack=self.analysis_pack(); self.store.put("think","analysis",pack)
            pool=self.pool()
            for entry in pool:
                if entry.get("status")!="SHADOW": continue
                ev=self.evaluate(entry); entry["evaluation"]=ev
                if entry.get("study_id"): continue  # Lab candidates are judged by the Evaluators
                if ev["verdict"]=="PASS": self._propose_promotion(entry,ev)
                elif ev["verdict"]=="FAIL":  # no time limit: a slow candidate stays until a rule or the user stops it
                    entry.update(status="RETIRED",verdict="FAIL",why=ev["why"])
                    self.note("verdict",candidate=entry["id"],hypothesis=entry["hypothesis"],verdict=entry["verdict"],why=ev["why"],
                              lesson=f"edge {ev['candidate'].get('expectancy_bps')} bps over {ev['candidate'].get('n',0)} trades vs champion {ev['champion_same_window'].get('expectancy_bps')}")
            self._save_pool(pool)
            proposed=0; settings=self.store.get("settings","think_agent",{}) or {}
            from think.loop import Lab
            lab_auto=bool(Lab(self).cfg["auto"])  # the Lab loop replaces the single-agent experiments below
            if not lab_auto and self._free_slots()>0 and settings.get("provider","mock")!="mock" and self.llm_calls_today()<int(c["max_llm_calls_per_day"]):
                exps=self._ask(pack)
                for e in exps: self.note("experiment",candidate=None,hypothesis=e.get("hypothesis"),kind=e.get("kind"),why=e.get("evidence"),params=e.get("params"))
                screened=self._screen(exps); pool=self.pool(); champ_sha=_sha(self.trade_file.read_bytes())
                for e in sorted(screened,key=lambda e:{"PASS":0,"UNSCREENED":1,"INCONCLUSIVE":2}.get(e["screen"]["status"],9)):
                    if e["screen"]["status"] not in ("PASS","UNSCREENED","INCONCLUSIVE") or self._free_slots()<=0: continue
                    if e["screen"]["status"]=="INCONCLUSIVE" and proposed: continue
                    entry={"id":e["id"],"kind":e["kind"],"hypothesis":e["hypothesis"],"evidence":e.get("evidence"),"expected_effect":e.get("expected_effect"),
                           "overrides":e.get("overrides") or {},"source_path":e.get("source_path"),"source_sha":e.get("source_sha") or champ_sha,"base_sha":e.get("base_sha") or champ_sha,
                           "screen":e["screen"],"created_at":time.time(),"started_ms":int(time.time()*1000),"status":"SHADOW"}
                    pool.append(entry); self._save_pool(pool); proposed+=1
                    self.store.put("think","tested",int(self.store.get("think","tested",0) or 0)+1)
                    self.note("shadow_started",candidate=e["id"],hypothesis=e["hypothesis"],kind=e["kind"],params=entry["overrides"],verdict=e["screen"]["status"])
            self._weekly_report(settings,c)
            now=time.time(); self._set_state(running=False,last_cycle=now,next_cycle=now+float(c["interval_hours"])*3600,last_error=None,backoff_s=0)
            self.note("cycle",proposed=proposed,trades=pack["champion"]["stats"].get("n",0))
            return {"ran":True,"proposed":proposed}
        except Exception as exc:
            s=self.state(); back=min(4*3600,max(900,int(s.get("backoff_s") or 450)*2))
            self._set_state(running=False,last_error=f"{type(exc).__name__}: {str(exc)[:300]}",backoff_s=back,next_cycle=time.time()+back)
            self.note("error",why=f"{type(exc).__name__}: {str(exc)[:300]}"); raise
    def _weekly_report(self, settings: dict, c: dict):
        s=self.state()
        if time.time()-float(s.get("last_report") or 0)<7*86400 or settings.get("provider","mock")=="mock" or self.llm_calls_today()>=int(c["max_llm_calls_per_day"]): return
        self._count_llm(); provider=AgentProvider(settings)
        text=provider.chat(json.dumps({"notebook":self.notebook(200),"champion":self.analysis_pack()["champion"]["stats"]},default=str)[:60000],
                           system="Write a concise weekly research report for the trader: what was tested, what worked, what failed and why, current champion edge, and the 3 most promising next hypotheses. Markdown, under 400 words.")
        self.note("report",text=text); self._set_state(last_report=time.time())

    # ---------------- daily check (standard card) ----------------
    def daily_check(self) -> list[dict]:
        """The user's standard 7-line daily card for the champion and every live candidate."""
        snap=self.store.get("trade","snapshot",{}) or {}; th=(self.store.health() or {}).get("trade",{})
        cols=[("Champion",self.champion_trades(),th.get("state")=="HEALTHY",th.get("reason",""),snap.get("positions") or [])]
        for p in self.pool():
            if p.get("status") in ("SHADOW","PASS_PENDING"):
                rec=self.store.get("shadow",p["id"]) or {}
                pos=[{"side":v["side"],"size":v["size"],"entry_time_ms":v["entry_time_ms"]} for v in (rec.get("broker",{}).get("positions") or {}).values()]
                cols.append((p["id"],self.shadow_trades(p["id"]),not rec.get("error"),rec.get("error") or "running",pos))
        today=int(time.time()//86400); out=[]
        for name,trades,ok,why,pos in cols:
            s=stats(trades); last30=stats(trades[-30:]); per_day={}
            for t in trades: per_day[t["exit_ms"]//86_400_000]=per_day.get(t["exit_ms"]//86_400_000,0)+1
            prev=sorted(v for d,v in per_day.items() if d<today); usual=prev[len(prev)//2] if prev else None; n_today=per_day.get(today,0)
            net_today=sum(t["net"] for t in trades if t["exit_ms"]//86_400_000==today)
            lvl=lambda g,a: "g" if g else ("a" if a else "r")
            rows={"health":(("All OK" if ok else "Problem"),why[:40],lvl(ok,False)),
                  "pnl":(f"{net_today:+.2f}",f"{s.get('net',0):+.2f} total",lvl(net_today>=0,net_today>-100)),
                  "trades":(str(n_today),f"usual {usual}" if usual is not None else "no history",lvl(usual is None or (n_today>0 and n_today<=3*max(usual,1)),n_today==0 and (usual or 0)==0)),
                  "edge":(f"{last30.get('expectancy_bps',0):+.1f} bps" if trades else "—",f"PF {last30.get('profit_factor')}" if trades else "no trades",lvl(not trades or last30.get("expectancy_bps",0)>0,len(trades)<10)),
                  "dd":(f"-{s.get('dd_now_pct',0):.2f}%",f"max -{s.get('max_dd_pct',0):.2f}%",lvl((s.get("dd_now_pct") or 0)<1.5,(s.get("dd_now_pct") or 0)<2)),
                  "streak":(str(s.get("losing_streak_now",0)),f"max {s.get('max_losing_streak',0)}",lvl((s.get("losing_streak_now") or 0)<=4,(s.get("losing_streak_now") or 0)<=6)),
                  "position":((f"{pos[0]['side'].title()} {pos[0]['size']:.3f}" if pos else "None"),(f"{(time.time()*1000-pos[0]['entry_time_ms'])/60000:.0f} min" if pos else ""),"g")}
            out.append({"name":name,"rows":rows})
        return out

def worker(root: str):
    eng=ThinkEngine(Path(root))
    try: os.nice(10)  # research must never starve the live trade process
    except OSError: pass
    import threading
    eng._set_state(running=False)
    if not eng.state().get("next_cycle"): eng._set_state(next_cycle=time.time()+600)  # first cycle 10 min after start
    def cycle():
        try: eng.run_cycle(force=True)
        except Exception: pass  # recorded in state + notebook; retried with backoff
    lab_next=[time.time()+600]
    def lab_step():
        """The Lab loop (Supervisor-run): advance studies, then the daily review. Same thread slot as the cycle,
        so the two never change the shadow pool at the same time."""
        from think.loop import Lab, lab_tools
        try:
            lab=Lab(eng); tools,handler=lab_tools(eng); lab_next[0]=time.time()+60*float(lab.cfg["step_minutes"])
            for _ in range(4):
                r=lab.advance(tools,handler); prev=eng.state().get("lab_last") or {}
                since=prev.get("since") if (prev.get("step"),prev.get("why"))==(r.get("step"),r.get("why")) else None
                eng._set_state(lab_last={**r,"at":time.time(),"since":since or time.time()})   # since: how long it has said the same
                if r["step"] in ("idle","error","failed"): break
            from manage.manage import ManageEngine
            ManageEngine(eng.root).supervisor.daily_review()
        except Exception as exc: eng.note("error",why=f"lab step: {type(exc).__name__}: {str(exc)[:300]}")
    job=None; kind=""
    while True:
        c=eng.control(); s=eng.state(); busy=job is not None and job.is_alive()
        if c["paused"]: eng.store.set_health(Health("think","PAUSED","paused by user",os.getpid()))
        else:
            reason=("running a research cycle" if kind=="cycle" else "running the Lab loop") if busy else (s.get("last_error") or "scientist idle")
            eng.store.set_health(Health("think","DEGRADED" if s.get("last_error") and not busy else "HEALTHY",reason[:200],os.getpid()))
            if not busy:
                try: eng.apply_retire_requests()
                except Exception as exc: eng.note("error",why=f"retire requests: {type(exc).__name__}: {exc}")
            if not busy and (c.get("run_now") or time.time()>=float(s.get("next_cycle") or 0)):
                if c.get("run_now"): eng.set_control({"run_now":False})
                job=threading.Thread(target=cycle,daemon=True); kind="cycle"; job.start()
            elif not busy and time.time()>=lab_next[0]:
                lab_next[0]=time.time()+900; job=threading.Thread(target=lab_step,daemon=True); kind="lab"; job.start()
        time.sleep(5)

if __name__=="__main__": print(json.dumps(ThinkEngine().status(),indent=2,default=str))
