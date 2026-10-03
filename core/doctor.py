#!/usr/bin/env python3
from __future__ import annotations
import argparse, importlib.util, json, os, socket, subprocess, sys, tempfile, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from core.common import AgentProvider, Store, default_agent_settings, import_module, load_config, load_root_env, resolve
from think.think import ThinkEngine

def check(name,fn):
    try:
        detail=fn()
        if detail is False: raise AssertionError(f"{name} returned false")
        return {"name":name,"ok":True,"detail":detail}
    except Exception as e:return {"name":name,"ok":False,"detail":repr(e)}

def run(full=False,provider=False,live_market=False):
    env_status=load_root_env(ROOT)
    old_runtime=os.environ.get('APP_RUNTIME_DIR')
    probe=tempfile.TemporaryDirectory(prefix='tt-doctor-')
    os.environ['APP_RUNTIME_DIR']=probe.name
    cfg=load_config(ROOT); store=Store(resolve(ROOT,cfg['runtime']['database']))
    checks=[]
    checks.append(check('project_config',lambda:{"name":cfg['project']['name'],"paper_only":cfg['mode']['paper_only']}))
    checks.append(check('env_contract',lambda:{"root_env_present":env_status['present'],"example_present":(ROOT/'.env.example').is_file(),"secrets_not_required_for_offline":True}))
    def writable():
        directory=resolve(ROOT,cfg['runtime']['directory']); directory.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory) as f: f.write(b'ok'); f.flush()
        return True
    checks.append(check('runtime_writable',writable))
    def trade_contract():
        p=resolve(ROOT,cfg['trade']['file'])
        proc=subprocess.run([sys.executable,str(ROOT/'trade'/'validate_trade_contract.py'),str(p),'--run-self-test'],capture_output=True,text=True,timeout=20)
        result=json.loads(proc.stdout)
        if proc.returncode or not result.get('ok') or not result.get('self_test',{}).get('ok'):
            raise AssertionError(result)
        return result
    checks.append(check('trade_contract_self_test',trade_contract))
    def strategy_interface():
        """What the platform needs from the EDITABLE strategy beyond the written contract (see START_HERE.md)."""
        import dataclasses
        mod=import_module(resolve(ROOT,cfg['trade']['file']),'doctor_iface')
        assert dataclasses.is_dataclass(mod.TradeConfig), "TradeConfig must be a @dataclass (Think tunes its fields)"
        core=mod.ScientificCore(mod.TradeConfig())
        for name in ('config','event_history','position_state','cooldown_until_ms','last_features'):
            assert hasattr(core,name), f"ScientificCore needs attribute {name}"
        assert callable(getattr(core,'decide',None)) and callable(getattr(core,'_apply_event',None)), "ScientificCore needs decide() and _apply_event()"
        assert isinstance(core.position_state,dict) and isinstance(core.cooldown_until_ms,int), "position_state must be a dict, cooldown_until_ms an int"
        json.dumps({"position_state":core.position_state,"cooldown_until_ms":core.cooldown_until_ms})  # checkpointed as JSON
        assert hasattr(core.config,'assumed_equity_usd'), "TradeConfig needs assumed_equity_usd (set by the runtime)"
        return {"strategy":mod.TRADE_META.get("name"),"tunable_fields":len(dataclasses.fields(mod.TradeConfig))}
    checks.append(check('strategy_interface',strategy_interface))
    checks.append(check('market_dependency',lambda:__import__('websockets').__version__))
    checks.append(check('sqlite',lambda:store.get('doctor','probe',{'ok':True})))
    t=ThinkEngine(ROOT); checks.append(check('think_status',lambda:t.status()['state'] in {'IDLE','RUNNING','PAUSED','ERROR'}))
    checks.append(check('mock_manage_agent',lambda:AgentProvider({"provider":"mock"}).chat('ping',system='test')))
    if full:
        def sequence_gap():
            mod=import_module(resolve(ROOT,cfg['trade']['file']),'doctor_gap'); r=mod.create_trade_system(); r.initialize({}); r.input.mark_connected(); r.input.mark_subscribed('book'); r.input.mark_subscribed('trades'); r.on_l3_snapshot([[1,'100','1'],[2,'101','-1']],1,time.time_ns())
            try:r.on_l3_update([1,'100','1'],3,time.time_ns())
            except mod.L3IntegrityError:return 'rejected'
            raise AssertionError('sequence gap accepted')
        checks.append(check('l3_sequence_gap_fail_closed',sequence_gap))
        def checksum():
            mod=import_module(resolve(ROOT,cfg['trade']['file']),'doctor_cs'); r=mod.create_trade_system(); r.initialize({}); r.input.mark_connected(); r.input.mark_subscribed('book'); r.input.mark_subscribed('trades'); r.on_l3_snapshot([[1,'100','1'],[2,'101','-1']],1,time.time_ns())
            try:r.on_l3_checksum(123456,2,time.time_ns())
            except mod.L3IntegrityError:return 'rejected'
            raise AssertionError('bad checksum accepted')
        checks.append(check('l3_checksum_fail_closed',checksum))
        def recovery():
            store.put('doctor','recovery_probe',{'x':1}); snap=resolve(ROOT,'runtime/backups/doctor.sqlite3'); store.snapshot(snap); return {"exists":snap.exists()}
        checks.append(check('snapshot_recovery_ready',recovery))
        def ui_routes():
            from ui.ui import App
            app=App(ROOT)
            for p in ['/manage/overview','/think/overview','/trade/overview','/trade/market']:
                text=app.render(p); assert '<html>' in text and 'MANAGE' in text and 'THINK' in text and 'TRADE' in text
            return '4 routes rendered'
        checks.append(check('ui_routes',ui_routes))
    if live_market:
        def live_market_probe():
            import asyncio
            mod=import_module(resolve(ROOT,cfg['trade']['file']),'doctor_live_market')
            r=mod.create_trade_system(symbol=cfg['market']['symbol'],market_config=cfg['market']); r.initialize({'doctor':True})
            async def probe():
                task=asyncio.create_task(r.run_live_market_data())
                try:
                    deadline=time.monotonic()+25
                    while time.monotonic()<deadline:
                        h=r.health()
                        if h.get('ok'):
                            kinds={x.get('event_kind') for x in r.get_recent_signals(200)}
                            return {'health':h,'event_kinds':sorted(x for x in kinds if x)}
                        await asyncio.sleep(.25)
                    raise RuntimeError(f"live market did not become verified/healthy: {r.health()}")
                finally:
                    task.cancel()
                    try: await task
                    except BaseException: pass
            return asyncio.run(probe())
        checks.append(check('bitfinex_live_market',live_market_probe))
    if provider:
        def live_provider():
            s=store.get('settings','manage_agent',default_agent_settings(cfg,'manage')).copy(); s['provider']='openai'; return AgentProvider(s).chat('Reply with exactly OK',system='Connectivity test. Reply exactly OK.',allow_web=False)
        checks.append(check('openai_connectivity',live_provider))
    result={"ok":all(x['ok'] for x in checks),"mode":"FULL" if full else "QUICK","checks":checks}
    if old_runtime is None: os.environ.pop('APP_RUNTIME_DIR',None)
    else: os.environ['APP_RUNTIME_DIR']=old_runtime
    probe.cleanup()
    return result
if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--full',action='store_true'); ap.add_argument('--provider',action='store_true'); ap.add_argument('--live-market',action='store_true'); a=ap.parse_args(); out=run(a.full,a.provider,a.live_market); print(json.dumps(out,indent=2,default=str)); raise SystemExit(0 if out['ok'] else 1)
