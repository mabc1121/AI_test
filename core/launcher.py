#!/usr/bin/env python3
from __future__ import annotations
import argparse, multiprocessing as mp, os, signal, sys, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from core.common import Health, Store, load_config, load_root_env, resolve
from manage.manage import worker as manage_worker
from think.think import worker as think_worker
from trade.worker import worker as trade_worker
from ui.ui import serve

WORKERS=("manage","think","trade","ui")
LOG_MAX_BYTES=5_000_000
RESTART_ALL_EXIT=75  # non-zero so systemd (Restart=on-failure) starts a fresh launcher

class _Tee:
    """Mirror a std stream into the shared app log (read by Manage), prefixing each line."""
    def __init__(self,stream,log,name): self.stream=stream; self.log=log; self.name=name; self.bol=True
    def write(self,s):
        self.stream.write(s)
        for line in s.splitlines(True):
            if self.bol: self.log.write(time.strftime('%Y-%m-%d %H:%M:%S ')+f'[{self.name}] ')
            self.log.write(line); self.bol=line.endswith('\n')
        self.log.flush(); return len(s)
    def flush(self): self.stream.flush(); self.log.flush()
    def __getattr__(self,a): return getattr(self.stream,a)

def _child(name:str):
    cfg=load_config(ROOT); path=resolve(ROOT,'runtime/logs/app.log'); path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_file() and path.stat().st_size>LOG_MAX_BYTES: os.replace(path,path.with_suffix('.log.1'))
    log=open(path,'a',encoding='utf-8',errors='replace'); sys.stdout=_Tee(sys.stdout,log,name); sys.stderr=_Tee(sys.stderr,log,name)
    if name=='ui': serve(ROOT,cfg['ui']['host'],int(cfg['ui']['port']))
    else: {'manage':manage_worker,'think':think_worker,'trade':trade_worker}[name](str(ROOT))

def run():
    load_root_env(ROOT)
    cfg=load_config(ROOT); store=Store(resolve(ROOT,cfg['runtime']['database']))
    ctx=mp.get_context('spawn')  # fresh interpreter per worker, so a worker restart loads the current code from disk
    def spawn(name):
        p=ctx.Process(target=_child,args=(name,),name=name); p.start(); return p
    jobs={n:spawn(n) for n in WORKERS}
    handled=float((store.get('control','restart') or {}).get('requested_at',0))  # ignore requests from before this start
    def stop(*_, exit_code=0):
        for p in jobs.values():
            if p.is_alive(): p.terminate()
        for p in jobs.values():p.join(3)
        for p in jobs.values():
            if p.is_alive(): p.kill(); p.join(3)
        raise SystemExit(exit_code)
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    while True:
        req=store.get('control','restart') or {}
        if float(req.get('requested_at',0))>handled:
            handled=float(req['requested_at']); names=[n for n in req.get('workers',[]) if n in WORKERS or n=='all']
            store.audit('launcher','restart',{'workers':names})
            if 'all' in names: stop(exit_code=RESTART_ALL_EXIT)
            for n in names:
                p=jobs[n]; p.terminate(); p.join(5)
                if p.is_alive(): p.kill(); p.join(3)
                jobs[n]=spawn(n)
        dead=[p for p in jobs.values() if not p.is_alive()]
        if dead:
            for p in dead: store.set_health(Health(p.name,'ERROR',f'worker exited code={p.exitcode}',p.pid))
            stop(exit_code=1)
        time.sleep(1)
if __name__=='__main__': run()
