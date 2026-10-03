#!/usr/bin/env python3
from __future__ import annotations
import argparse, os, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def run(cmd): print('+',' '.join(map(str,cmd))); subprocess.run(cmd,check=True)
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('command',choices=['check','check-live','start','print-systemd']); a=ap.parse_args()
    if a.command=='check': run([sys.executable,'-m','core.doctor','--full']); return
    if a.command=='check-live': run([sys.executable,'-m','core.doctor','--full','--live-market']); return
    if a.command=='start': run([sys.executable,'-m','core.launcher']); return
    user=os.environ.get('USER','ubuntu'); py=ROOT/'.venv'/'bin'/'python'
    print(f'''[Unit]\nDescription=Reusable Trading Research Template\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nUser={user}\nWorkingDirectory={ROOT}\nExecStart={py} -m core.launcher\nRestart=on-failure\nRestartSec=5\nEnvironment=PYTHONUNBUFFERED=1\nEnvironmentFile=-{ROOT}/.env\nNoNewPrivileges=true\nPrivateTmp=true\n\n[Install]\nWantedBy=multi-user.target''')
if __name__=='__main__': main()
