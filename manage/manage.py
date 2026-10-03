from __future__ import annotations
import difflib, hashlib, json, os, re, subprocess, sys, tempfile, time
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.common import AGENT_OPTIONS, AgentProvider, Health, Store, backup_file, default_agent_settings, load_config, resolve

WORKERS=("manage","think","trade","ui")
EDITABLE_SUFFIXES={".py",".md",".yaml",".json",".txt",".example"}
HIDDEN_PARTS={".venv",".git","__pycache__","runtime",".pytest_cache"}
LOCKED_FILES={".env","release_manifest.json"}
HISTORY_KEEP=200  # messages kept per chat
MAX_CHATS=100
HISTORY_PROMPT=12
# The chat is the Supervisor (manage/supervisor.py, prompt agents/prompts/supervisor_chat.md); these are its base tools.
TOOLS=[
    {"type":"function","name":"get_status","description":"Current health of every component, trade status/metrics, pending actions and recent log errors.","parameters":{"type":"object","properties":{},"additionalProperties":False}},
    {"type":"function","name":"read_logs","description":"Tail the application log (all components).","parameters":{"type":"object","properties":{"lines":{"type":"integer","description":"How many lines, max 400"},"contains":{"type":"string","description":"Only lines containing this text (case-insensitive); empty for all"}},"required":["lines","contains"],"additionalProperties":False}},
    {"type":"function","name":"list_files","description":"List project files under a directory (relative to project root).","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"],"additionalProperties":False}},
    {"type":"function","name":"read_file","description":"Read a project file with line numbers.","parameters":{"type":"object","properties":{"path":{"type":"string"},"start_line":{"type":"integer"},"max_lines":{"type":"integer","description":"max 600"}},"required":["path","start_line","max_lines"],"additionalProperties":False}},
    {"type":"function","name":"search_files","description":"Regex search across project text files.","parameters":{"type":"object","properties":{"pattern":{"type":"string"},"path":{"type":"string","description":"Directory to search, '.' for all"}},"required":["pattern","path"],"additionalProperties":False}},
    {"type":"function","name":"propose_file_edit","description":"Propose replacing old_text with new_text in an existing project file. old_text must match exactly once. The user must approve it.","parameters":{"type":"object","properties":{"path":{"type":"string"},"old_text":{"type":"string"},"new_text":{"type":"string"},"reason":{"type":"string"}},"required":["path","old_text","new_text","reason"],"additionalProperties":False}},
    {"type":"function","name":"propose_restart","description":"Propose restarting a component (manage, think, trade, ui) or 'all'. The user must approve it.","parameters":{"type":"object","properties":{"worker":{"type":"string","enum":[*WORKERS,"all"]},"reason":{"type":"string"}},"required":["worker","reason"],"additionalProperties":False}},
]
from manage.supervisor import TOOLS as SUPERVISOR_TOOLS, TOOL_NAMES as SUPERVISOR_TOOL_NAMES, Supervisor
CHAT_TOOLS=TOOLS+SUPERVISOR_TOOLS

def _sha(text: str) -> str: return hashlib.sha256(text.encode()).hexdigest()

def _git(root: Path, *args, timeout: int=60):
    # The app user has no git identity (no global config, empty user name): name the committer, or every commit fails.
    env={**os.environ,"HOME":tempfile.gettempdir(),"GIT_CONFIG_GLOBAL":"/dev/null","GIT_TERMINAL_PROMPT":"0"}
    return subprocess.run(["git","-c",f"safe.directory={Path(root).resolve()}","-c","user.name=Manage","-c","user.email=manage@tt-trade.local",*args],
                          cwd=root,env=env,capture_output=True,text=True,timeout=timeout)

def git_commit(root: Path, paths: list[str], message: str, author: str) -> dict:
    """Record an applied change in the project's git history; the push to GitHub is best effort."""
    if not (root/".git").is_dir(): return {"committed":False,"why":"no git repository"}
    try:
        _git(root,"add","--",*paths); r=_git(root,"commit","-q","-m",message[:2000],f"--author={author}")
        if r.returncode: return {"committed":False,"why":(r.stderr or r.stdout)[-300:]}
        push=_git(root,"push","-q","origin","HEAD:main",timeout=45)
        return {"committed":True,"sha":_git(root,"rev-parse","--short","HEAD").stdout.strip(),"pushed":push.returncode==0}
    except Exception as exc: return {"committed":False,"why":f"{type(exc).__name__}: {exc}"}

def git_push_pending(root: Path) -> None:
    """Retry pushing commits that could not reach GitHub earlier."""
    if (root/".git").is_dir() and _git(root,"rev-list","--count","origin/main..HEAD").stdout.strip() not in ("","0"):
        _git(root,"push","-q","origin","HEAD:main",timeout=45)

class ManageEngine:
    def __init__(self, root: Path=ROOT):
        self.root=root; self.cfg=load_config(root); self.store=Store(resolve(root,self.cfg["runtime"]["database"]))
        self.log_path=resolve(root,"runtime/logs/app.log")
        if self.store.get("settings","manage_agent") is None:
            self.store.put("settings","manage_agent",default_agent_settings(self.cfg,"manage"))
        self._supervisor=None
    @property
    def supervisor(self) -> Supervisor:
        if self._supervisor is None: self._supervisor=Supervisor(self)
        return self._supervisor

    # ---------- status ----------
    def health(self):
        now=time.time()
        return {k:{**v,"age_seconds":round(now-float(v["updated_at"] or 0),1),"stale":now-float(v["updated_at"] or 0)>15} for k,v in self.store.health().items()}
    def status(self):
        snap=self.store.get("trade","snapshot",{}) or {}
        return {"project":self.cfg["project"],"health":self.health(),
                "trade":{"status":snap.get("status"),"health":snap.get("health"),"metrics":snap.get("metrics"),"open_positions":len(snap.get("positions") or [])},
                "pending_actions":[self.brief(a) for a in self.store.actions("PENDING")],
                "recent_log_errors":self.log_errors(10)}
    def brief(self, a: dict):
        p=a["payload"]; return {"id":a["id"],"kind":a["kind"],"status":a["status"],"target":p.get("path") or p.get("worker"),"reason":p.get("reason","")}

    # ---------- logs & files (read-only) ----------
    def read_logs(self, lines: int=100, contains: str=""):
        if not self.log_path.is_file(): return []
        with open(self.log_path,"rb") as f:
            f.seek(0,2); f.seek(max(0,f.tell()-512_000)); rows=f.read().decode(errors="replace").splitlines()[1:]
        if contains: rows=[r for r in rows if contains.lower() in r.lower()]
        return rows[-max(1,min(int(lines),400)):]
    def log_errors(self, n: int=10):
        return [r for r in self.read_logs(2000) if re.search(r"Error|Exception|Traceback",r)][-n:]
    def _rel(self, path: str) -> tuple[Path,str]:
        p=resolve(self.root,path or "."); rel=p.relative_to(self.root.resolve()).as_posix()
        parts=set(Path(rel).parts)
        if parts & HIDDEN_PARTS or any(x.startswith(".env") and x!=".env.example" for x in parts): raise PermissionError(f"not accessible: {path}")
        return p,rel
    def _editable(self, path: str) -> tuple[Path,str]:
        p,rel=self._rel(path)
        if rel in LOCKED_FILES or (p.suffix not in EDITABLE_SUFFIXES and p.name!=".env.example"): raise PermissionError(f"not editable: {path}")
        if not p.is_file(): raise FileNotFoundError(path)
        return p,rel
    def _visible(self, p: Path) -> bool:
        rel=p.relative_to(self.root.resolve())
        return p.is_file() and not set(rel.parts) & HIDDEN_PARTS and not (p.name.startswith(".env") and p.name!=".env.example")
    def list_files(self, path: str="."):
        base,_=self._rel(path); out=[]
        for p in sorted(base.rglob("*")):
            if self._visible(p): out.append(f"{p.relative_to(self.root.resolve()).as_posix()} ({p.stat().st_size} B)")
            if len(out)>=300: break
        return out
    def read_file(self, path: str, start_line: int=1, max_lines: int=400):
        p,_=self._rel(path)
        if not p.is_file(): raise FileNotFoundError(path)
        lines=p.read_text(errors="replace").splitlines(); s=max(1,int(start_line)); e=min(len(lines),s-1+max(1,min(int(max_lines),600)))
        return {"path":path,"total_lines":len(lines),"text":"\n".join(f"{i}\t{lines[i-1]}" for i in range(s,e+1))}
    def search_files(self, pattern: str, path: str="."):
        rx=re.compile(pattern); base,_=self._rel(path); hits=[]
        for p in sorted(base.rglob("*")):
            if not self._visible(p) or p.suffix not in EDITABLE_SUFFIXES: continue
            for i,line in enumerate(p.read_text(errors="replace").splitlines(),1):
                if rx.search(line): hits.append(f"{p.relative_to(self.root.resolve()).as_posix()}:{i}: {line.strip()[:200]}")
                if len(hits)>=100: return hits
        return hits

    # ---------- actions ----------
    def propose_file_edit(self, path: str, old_text: str, new_text: str, reason: str) -> int:
        p,rel=self._editable(path); text=p.read_text()
        if not old_text: raise ValueError("old_text must not be empty")
        n=text.count(old_text)
        if n!=1: raise ValueError(f"old_text must match exactly once (matched {n} times)")
        if old_text==new_text: raise ValueError("no change")
        aid=self.store.propose_action("EDIT_FILE",{"path":rel,"old_text":old_text,"new_text":new_text,"reason":reason,"base_sha256":_sha(text)})
        self.store.audit("manage","action_proposed",{"id":aid,"kind":"EDIT_FILE","path":rel}); return aid
    def propose_restart(self, worker: str, reason: str) -> int:
        if worker not in (*WORKERS,"all"): raise ValueError(f"unknown worker: {worker}")
        aid=self.store.propose_action("RESTART",{"worker":worker,"reason":reason})
        self.store.audit("manage","action_proposed",{"id":aid,"kind":"RESTART","worker":worker}); return aid
    def _pending(self, action_id: int):
        a=next((x for x in self.store.actions() if x["id"]==int(action_id)),None)
        if not a or a["status"]!="PENDING": raise ValueError("action not pending")
        return a
    def reject_action(self, action_id: int):
        self._pending(action_id); self.store.finish_action(int(action_id),"REJECTED",{"ok":False,"stage":"rejected"}); self.store.audit("manage","action_rejected",{"id":int(action_id)})
        return {"ok":True}
    def approve_action(self, action_id: int):
        a=self._pending(action_id)
        self.store.finish_action(a["id"],"RUNNING",{})
        try:
            if a["kind"]=="RESTART":
                self.request_restart([a["payload"]["worker"]]); result={"ok":True,"restarted":[a["payload"]["worker"]]}
            elif a["kind"]=="EDIT_FILE": result=self._apply_edit(a["id"],a["payload"])
            elif a["kind"]=="LAB_CHANGE": result=self.supervisor.apply_change(a["payload"])
            elif a["kind"]=="RETIRE_SHADOW": result=self.supervisor.retire(a["id"],a["payload"])
            else: result={"ok":False,"stage":"unsupported","error":f"unsupported action: {a['kind']}"}
        except Exception as exc: result={"ok":False,"stage":"error","error":f"{type(exc).__name__}: {exc}"}
        status="COMPLETED" if result["ok"] else ("ROLLED_BACK" if result.get("rolled_back") else "FAILED")
        self.store.finish_action(a["id"],status,result); self.store.audit("manage","action_"+status.lower(),{"id":a["id"],**{k:v for k,v in result.items() if k!="output"}})
        return result
    def _validate(self, p: Path, rel: str, text: str):
        if p.suffix==".py": compile(text,rel,"exec")
        if p.suffix==".json": json.loads(text)
        if rel=="config/project.yaml":
            cfg=json.loads(text)
            if cfg.get("mode",{}).get("paper_only") is not True: raise ValueError("mode.paper_only must stay true")
        if rel==self.cfg["trade"]["file"]:
            if '"paper_only": True' not in text: raise ValueError("trade must stay paper-only")
            if (self.store.get("trade","snapshot",{}) or {}).get("positions"):
                raise ValueError("the champion has an open position; approve again once it is flat")
            with tempfile.NamedTemporaryFile("w",suffix=".py",delete=False) as f: f.write(text)
            try:
                proc=subprocess.run([sys.executable,str(self.root/"trade"/"validate_trade_contract.py"),f.name,"--canonical",str(p),"--run-self-test"],capture_output=True,text=True,timeout=60)
                out=json.loads(proc.stdout or "{}")
                if proc.returncode or not out.get("ok") or not out.get("self_test",{}).get("ok"): raise ValueError(f"trade contract failed: {out.get('errors') or proc.stderr[-1500:]}")
            finally: os.unlink(f.name)
    def _write(self, p: Path, text: str):
        tmp=p.with_name(f".{p.name}.tmp"); tmp.write_text(text); os.replace(tmp,p)
    def _apply_edit(self, action_id: int, pl: dict):
        p,rel=self._editable(pl["path"]); text=p.read_text()
        if _sha(text)!=pl["base_sha256"]: return {"ok":False,"stage":"conflict","error":"file changed since the proposal; ask Manage to propose again"}
        new=text.replace(pl["old_text"],pl["new_text"],1)
        try: self._validate(p,rel,new)
        except Exception as exc: return {"ok":False,"stage":"validation","error":f"{type(exc).__name__}: {exc}"}
        backup=backup_file(self.root,p,f"action_{action_id}_{int(time.time())}")
        try: self._write(p,new)
        except OSError as exc: return {"ok":False,"stage":"write","error":str(exc)}
        with tempfile.TemporaryDirectory(prefix="tt-manage-test-") as td:
            proc=subprocess.run([sys.executable,"-m","tests.run_all"],cwd=self.root,capture_output=True,text=True,timeout=600,
                                env={**os.environ,"APP_RUNTIME_DIR":td,"PYTHONDONTWRITEBYTECODE":"1"})
        output=(proc.stdout+proc.stderr)[-4000:]
        if proc.returncode!=0:
            self._write(p,text); return {"ok":False,"stage":"tests","rolled_back":True,"error":"test suite failed; change rolled back","output":output}
        reason=str(pl.get("reason") or "approved change")
        author="Think <think@tt-trade.local>" if reason.startswith(("PROMOTE","AUTO-ROLLBACK")) else "Manage <manage@tt-trade.local>"
        git=git_commit(self.root,[rel],f"{reason[:200]}\n\nApproved action #{action_id} ({rel})",author)
        restart=self.restart_targets(rel)
        if restart: self.request_restart(restart)
        return {"ok":True,"backup":str(backup),"restarted":restart,"git":git,"output":output}
    @staticmethod
    def restart_targets(rel: str) -> list[str]:
        top=rel.split("/")[0]
        if top in ("trade","think"): return [top]
        if top in ("manage","ui"): return ["manage","ui"]
        if top in ("core","config"): return ["all"]
        return []
    def request_restart(self, workers: list[str]):
        bad=[w for w in workers if w not in (*WORKERS,"all")]
        if bad: raise ValueError(f"unknown worker: {bad}")
        self.store.put("control","restart",{"workers":sorted(set(workers)),"requested_at":time.time()})
        self.store.audit("manage","restart_requested",{"workers":workers})

    # ---------- settings ----------
    def update_settings(self, who: str, values: dict):
        if who not in ("manage","think"): raise ValueError("unknown agent")
        cur=self.store.get("settings",f"{who}_agent",default_agent_settings(self.cfg,who))
        for k,v in values.items():
            v=str(v).strip()
            if k in ("model","fallback_model"):
                if not re.fullmatch(r"[\w.\-:]{0,64}",v) or (k=="model" and not v): raise ValueError(f"invalid {k.replace('_',' ')}")
            elif k not in AGENT_OPTIONS or v not in AGENT_OPTIONS[k]: raise ValueError(f"invalid setting {k}={v}")
            cur[k]=v
        self.store.put("settings",f"{who}_agent",cur); self.store.audit("manage","settings_updated",{"agent":who,**cur}); return cur

    # ---------- chats ----------
    def chats(self):
        chats=self.store.get("manage","chats")
        if chats is None:  # migrate the single conversation kept by earlier versions
            chats=[]; old=self.store.get("manage","chat_history") or []
            if old:
                cid=f"{int(old[0]['ts']*1000):x}"; self.store.put("manage_chat",cid,old)
                chats=[{"id":cid,"title":self._title(old[0]["text"]),"created_at":old[0]["ts"],"updated_at":old[-1]["ts"]}]
            self.store.put("manage","chats",chats); self.store.delete("manage","chat_history")
        return sorted(chats,key=lambda c:-c["updated_at"])
    @staticmethod
    def _title(text: str) -> str:
        t=" ".join(text.split()); return t[:60]+("…" if len(t)>60 else "")
    def history(self, chat_id: str): return self.store.get("manage_chat",str(chat_id),[]) or []
    def delete_chat(self, chat_id: str):
        chats=self.chats()
        if not any(c["id"]==chat_id for c in chats): raise ValueError("unknown chat")
        self.store.put("manage","chats",[c for c in chats if c["id"]!=chat_id]); self.store.delete("manage_chat",chat_id)
        return {"ok":True}
    def tool(self, name: str, args: dict):
        self.store.audit("manage","tool",{"name":name,"args":{k:(v[:200] if isinstance(v,str) else v) for k,v in args.items()}})
        if name=="get_status": return self.status()
        if name=="read_logs": return self.read_logs(args.get("lines",100),args.get("contains",""))
        if name=="list_files": return self.list_files(args.get("path","."))
        if name=="read_file": return self.read_file(args["path"],args.get("start_line",1),args.get("max_lines",400))
        if name=="search_files": return self.search_files(args["pattern"],args.get("path","."))
        if name=="propose_file_edit": return {"action_id":self.propose_file_edit(args["path"],args["old_text"],args["new_text"],args["reason"]),"status":"PENDING user approval"}
        if name=="propose_restart": return {"action_id":self.propose_restart(args["worker"],args["reason"]),"status":"PENDING user approval"}
        if name in SUPERVISOR_TOOL_NAMES: return self.supervisor.tool(name,args)
        raise ValueError(f"unknown tool: {name}")
    def chat(self, message: str, chat_id: str|None=None):
        """Answer in an existing chat, or start a new one when chat_id is empty."""
        message=message.strip()
        if not message: raise ValueError("empty message")
        chats=self.chats(); meta=next((c for c in chats if c["id"]==chat_id),None)
        if chat_id and meta is None: raise ValueError("unknown chat")
        hist=self.history(chat_id) if meta else []
        convo="\n\n".join(f"{t['role'].upper()}:\n{t['text'][:3000]}" for t in hist[-HISTORY_PROMPT:])
        try: lab=json.dumps(self.supervisor.overview(full=False),default=str)[:12000]
        except Exception as exc: lab=f"(Lab unavailable: {type(exc).__name__}: {exc})"
        prompt=(f"PROJECT STATUS:\n{json.dumps(self.status(),indent=1,default=str)[:12000]}\n\nLAB:\n{lab}\n\n"
                f"CONVERSATION SO FAR:\n{convo or '(none)'}\n\nUSER:\n{message}")
        answer=self.supervisor.answer(prompt,CHAT_TOOLS,self.tool)
        now=time.time()
        if meta is None:
            chat_id=f"{int(now*1000):x}"; meta={"id":chat_id,"title":self._title(message),"created_at":now}; chats.append(meta)
        meta["updated_at"]=now
        self.store.put("manage_chat",chat_id,(hist+[{"role":"user","text":message,"ts":now},{"role":"assistant","text":answer,"ts":now}])[-HISTORY_KEEP:])
        chats=sorted(chats,key=lambda c:-c["updated_at"])
        for old in chats[MAX_CHATS:]: self.store.delete("manage_chat",old["id"])
        self.store.put("manage","chats",chats[:MAX_CHATS])
        self.store.audit("manage","chat",{"chat_id":chat_id,"user":message,"answer":answer[:2000]})
        return {"answer":answer,"chat_id":chat_id}

def edit_diff(payload: dict) -> str:
    return "\n".join(difflib.unified_diff(payload["old_text"].splitlines(),payload["new_text"].splitlines(),payload["path"],payload["path"],lineterm="",n=2))

def worker(root: str):
    eng=ManageEngine(Path(root)); tick=0
    while True:
        eng.store.set_health(Health("manage","HEALTHY","worker running",os.getpid())); time.sleep(2); tick+=1
        if tick%300==0:  # every ~10 min
            try: git_push_pending(eng.root)
            except Exception: pass

if __name__=="__main__":
    print(json.dumps(ManageEngine().status(),indent=2,default=str))
