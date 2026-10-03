from __future__ import annotations
import asyncio, dataclasses, gzip, hashlib, json, os, sys, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from core.common import Health, Store, import_module, load_config, resolve
from trade.state import RecoveryError, checkpoint, identity, restore
from think.replay import encode_event

def _hour_key(ms:int)->str: return time.strftime('%Y%m%d-%H',time.gmtime(ms/1000))
def _day_key(ms:int)->str: return time.strftime('%Y-%m-%d',time.gmtime(ms/1000))

class Recorder:
    """Hourly gzip files of the clean events released to the strategy; Think replays them."""
    def __init__(self,directory:Path,keep_days:int=7):
        self.dir=directory; self.keep_days=keep_days; self.hour=None; self.fh=None; self.n=0; self.failed=None
    def write(self,event,decided:bool):
        if self.failed: return
        try:
            hour=_hour_key(int(event.recv_time_ms))
            if hour!=self.hour: self._rotate(hour)
            self.fh.write(encode_event(event,decided)+'\n'); self.n+=1
            if self.n%2000==0: self.fh.flush()
        except Exception as exc: self.failed=repr(exc)
    def _rotate(self,hour:str):
        if self.fh: self.fh.close()
        self.dir.mkdir(parents=True,exist_ok=True); self.hour=hour; self.fh=gzip.open(self.dir/f"{hour}.jsonl.gz","at",encoding="utf-8")
        cutoff=_hour_key(int((time.time()-self.keep_days*86400)*1000))
        for p in self.dir.glob("*.jsonl.gz"):
            if p.name[:11]<cutoff: p.unlink(missing_ok=True)
    def close(self):
        if self.fh: self.fh.close(); self.fh=None

class Shadow:
    """A candidate strategy fed the same live events as the champion, with its own protected paper broker."""
    def __init__(self,spec:dict,mod,store:Store):
        self.id=spec["id"]; self.spec=spec; self.mod=mod; self.store=store; self.error=None
        fields={f.name for f in dataclasses.fields(mod.TradeConfig)}
        self.core=mod.ScientificCore(mod.TradeConfig(**{k:v for k,v in (spec.get("overrides") or {}).items() if k in fields}))
        self.broker=mod._ProtectedPaperBroker(10_000.0); self.reasons:dict[str,dict[str,int]]={}; self.last_sig=None; self.last_save=0.0; self.last_event=None
        self._restore()
    def _restore(self):
        s=self.store.get("shadow",self.id)
        if not s or s.get("source_sha")!=self.spec.get("source_sha"): return
        b=s["broker"]; m=self.mod
        self.broker.positions={k:m.PositionState(**v) for k,v in b["positions"].items()}; self.broker.entry_fees=b["entry_fees"]
        self.broker.executions=[m.ExecutionRecord(**x) for x in b["executions"]]; self.broker.trade_pnls=b["trade_pnls"]
        self.broker.realized_pnl_usd=b["realized_pnl_usd"]; self.broker.peak_equity=b["peak_equity"]; self.broker.max_drawdown_pct=b["max_drawdown_pct"]
        self.core.cooldown_until_ms=s["core"]["cooldown_until_ms"]; self.core.position_state=s["core"]["position_state"]; self.reasons=s.get("reasons",{})
    def feed(self,event,decided:bool):
        if self.error: return
        try:
            if not decided: self.core._apply_event(event); return
            self.last_event=event
            self.core.config.assumed_equity_usd=max(0.0,self.broker.equity(event))
            d=self.core.decide(event,list(self.broker.positions.values())); self.broker.execute(d,event)
            h=self.reasons.setdefault(_hour_key(int(event.recv_time_ms)),{}); h[d.reason]=h.get(d.reason,0)+1
            sig=(len(self.broker.executions),tuple(self.broker.positions),self.core.cooldown_until_ms)
            if sig!=self.last_sig or time.monotonic()-self.last_save>=5: self.save(); self.last_sig=sig
        except Exception as exc:
            self.error=f"{type(exc).__name__}: {exc}"; self.save()
    def save(self):
        self.last_save=time.monotonic(); keep=sorted(self.reasons)[-7*24:]; self.reasons={k:self.reasons[k] for k in keep}
        b=self.broker
        self.store.put("shadow",self.id,{"id":self.id,"source_sha":self.spec.get("source_sha"),"error":self.error,"updated_at":time.time(),
            "broker":{"positions":{k:dataclasses.asdict(v) for k,v in b.positions.items()},"entry_fees":b.entry_fees,"executions":[dataclasses.asdict(x) for x in b.executions],
                      "trade_pnls":b.trade_pnls,"realized_pnl_usd":b.realized_pnl_usd,"peak_equity":b.peak_equity,"max_drawdown_pct":b.max_drawdown_pct},
            "core":{"cooldown_until_ms":self.core.cooldown_until_ms,"position_state":self.core.position_state},
            "metrics":b.metrics(self.last_event).as_dict(),"reasons":self.reasons})

class TradeEngine:
    def __init__(self,root:Path=ROOT,record:bool=False):
        self.root=root; self.cfg=load_config(root); self.store=Store(resolve(root,self.cfg["runtime"]["database"])); self.runtime=None
        self.recorder=Recorder(resolve(root,"runtime/recordings"),int(self.cfg.get("think",{}).get("recording_keep_days",7))) if record else None
        self.shadows:dict[str,Shadow]={}; self.pool_sig=None; self.funnel:dict[str,dict[str,int]]={}
        self.reconnects:dict[str,int]=dict(self.store.get("trade","reconnects",{}) or {}); self.reload()
    def reload(self):
        source=resolve(self.root,self.cfg["trade"]["file"])
        mod=import_module(source,"active_trade"); self.mod=mod; self.source=source
        self.runtime=mod.create_trade_system(symbol=self.cfg["market"]["symbol"],starting_equity_usd=float(self.cfg["trade"]["starting_equity_usd"]),market_config=self.cfg["market"])
        expected=identity(source,mod.TRADE_CONTRACT_VERSION)
        restore(self.store,self.runtime,expected,mod)
        self.runtime.persist_callback=self._persister(expected)
        self._tap_core()
        self.runtime.initialize({"project":self.cfg["project"]["name"]})
        return self.runtime
    def _tap_core(self):
        """Mirror every event the champion core sees to the recorder and the shadow pool."""
        core=self.runtime.core; apply0=core._apply_event; decide0=core.decide; inside={"d":False}
        def apply(event):
            apply0(event)
            if not inside["d"]: self._tap(event,False)
        def decide(event,positions):
            inside["d"]=True
            try: d=decide0(event,positions)
            finally: inside["d"]=False
            self._tap(event,True); h=self.funnel.setdefault(_hour_key(int(event.recv_time_ms)),{}); h[d.reason]=h.get(d.reason,0)+1
            if str(d.action).startswith("ENTER"): self._save_entry(event,d)
            return d
        core._apply_event=apply; core.decide=decide
    ENTRY_KEYS=("long_score","short_score","obi_near","persistent_obi","microprice_bias","book_pressure","tfi_fast","tfi_prior","ret_15m_bps","regime",
                "cost_bps","min_move_bps","reference_high","reference_low","high_10m","low_10m",
                # AI_test multi-horizon strategy
                "history_minutes","quality","sigma1_bps","spread_end_bps","h30_p_hit","h30_score","h60_p_hit","h60_score",
                "h120_p_hit","h120_score","h240_p_hit","h240_score")
    def _save_entry(self,event,d):
        """Why a trade was entered (features at decision time), keyed by execution timestamp for the Trades tab."""
        try:
            f=self.runtime.core.last_features or {}
            entries=self.store.get("trade","entries",{}) or {}
            entries[str(int(event.recv_time_ms))]={"action":d.action,"reason":d.reason,"stop":d.stop_price,"target":d.target_price,"size":d.requested_size,
                "mid":event.mid,"spread_bps":event.spread_bps,"features":{k:f.get(k) for k in self.ENTRY_KEYS}}
            self.store.put("trade","entries",{k:entries[k] for k in sorted(entries,key=int)[-500:]})
        except Exception: pass
    def screen_state(self)->dict:
        """Live values behind each entry check, for the Screening card."""
        core=self.runtime.core; f=getattr(core,"last_features",None) or {}; ev=self.runtime.last_event; cfg=getattr(core,"config",None); now=int(time.time()*1000)
        keys=("max_spread_bps","warmup_seconds","trend_15m_bps","min_confirmations","near_obi_threshold","persistent_obi_threshold","microprice_bias_threshold",
              "book_pressure_threshold","fast_tfi_threshold","min_expected_move_bps","cost_multiple")
        first=getattr(core,"first_event_ms",None)
        return {"ts":time.time(),"f":{k:f.get(k) for k in self.ENTRY_KEYS},"mid":getattr(ev,"mid",None),"spread":getattr(ev,"spread_bps",None),
                "warm_elapsed_s":(now-first)/1000 if first else 0,"cooldown_s":max(0,(getattr(core,"cooldown_until_ms",0)-now)/1000),
                "pending_breakout":getattr(core,"pending_breakout",None),"reason":self.runtime.signals[-1]["decision"]["reason"] if self.runtime.signals else None,
                "cfg":{k:getattr(cfg,k,None) for k in keys}}
    def _tap(self,event,decided):
        if event.kind=="l3_snapshot": self.reconnects[_day_key(int(event.recv_time_ms))]=self.reconnects.get(_day_key(int(event.recv_time_ms)),0)+1
        if self.recorder: self.recorder.write(event,decided)
        for s in list(self.shadows.values()): s.feed(event,decided)
    def sync_pool(self):
        """Start/stop shadows to match Think's pool (kv think/pool)."""
        pool=[p for p in (self.store.get("think","pool",[]) or []) if p.get("status")=="SHADOW"]
        sig=json.dumps(pool,sort_keys=True)
        if sig==self.pool_sig: return
        self.pool_sig=sig; want={p["id"]:p for p in pool}
        for sid in list(self.shadows):
            if sid not in want: self.shadows.pop(sid).save()
        for sid,spec in want.items():
            if sid in self.shadows: continue
            try:
                mod=self.mod if spec.get("kind")=="params" else import_module(Path(spec["source_path"]),f"shadow_{sid}")
                if spec.get("kind")=="params": spec={**spec,"source_sha":spec.get("source_sha") or hashlib.sha256(self.source.read_bytes()).hexdigest()}
                self.shadows[sid]=Shadow(spec,mod,self.store)
            except Exception as exc:
                self.store.put("shadow",sid,{"id":sid,"error":f"load failed: {exc}","updated_at":time.time()})
    def flush_funnel(self):
        keep=sorted(self.funnel)[-7*24:]; self.funnel={k:self.funnel[k] for k in keep}
        saved=self.store.get("trade","funnel",{}) or {}; saved.update(self.funnel); self.store.put("trade","funnel",{k:saved[k] for k in sorted(saved)[-7*24:]})
    def _persister(self,expected,interval_s=5.0):
        """Checkpoint on account changes (execution, position, cooldown) and at most every
        interval_s while a position is open, instead of on every market message."""
        last={"sig":None,"t":0.0}
        def persist(runtime):
            b=runtime.broker; sig=(len(b.executions),tuple(sorted(b.positions)),runtime.core.cooldown_until_ms); now=time.monotonic()
            if sig!=last["sig"] or (b.positions and now-last["t"]>=interval_s):
                checkpoint(self.store,runtime,expected); last.update(sig=sig,t=now)
        return persist
    def snapshot(self): return self.runtime.get_ui_snapshot()

async def worker_async(root:str):
    try:
        eng=TradeEngine(Path(root),record=True)
    except RecoveryError as exc:
        store=Store(resolve(Path(root),load_config(Path(root))["runtime"]["database"]))
        while True:
            store.set_health(Health("trade","PAUSED",f"recovery blocked: {exc}",os.getpid()))
            await asyncio.sleep(5)
    async def status_loop():
        tick=0
        while True:
            try:
                h=eng.runtime.health(); state="HEALTHY" if h.get("ok") else ("ERROR" if h.get("state") in {"integrity_error","disconnected"} else "DEGRADED"); eng.store.set_health(Health("trade",state,h.get("message","ok"),os.getpid())); eng.store.put("trade","snapshot",eng.snapshot())
                if tick%2==0: eng.store.put("trade","screen",eng.screen_state())
                if tick%10==0:
                    eng.sync_pool(); eng.store.put("trade","reconnects",{k:eng.reconnects[k] for k in sorted(eng.reconnects)[-14:]})
                if tick%60==0:
                    eng.flush_funnel()
                    for s in eng.shadows.values(): s.save()
                    eng.store.put("trade","recorder",{"hour":eng.recorder.hour,"events":eng.recorder.n,"error":eng.recorder.failed,"updated_at":time.time()})
            except Exception as exc: eng.store.set_health(Health("trade","ERROR",repr(exc),os.getpid()))
            tick+=1; await asyncio.sleep(1)
    try: await asyncio.gather(eng.runtime.run_live_market_data(),status_loop())
    finally:
        eng.recorder.close()
        for s in eng.shadows.values(): s.save()

def worker(root:str): asyncio.run(worker_async(root))
if __name__=="__main__": print(json.dumps(TradeEngine().snapshot(),indent=2,default=str))
