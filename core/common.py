from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sqlite3
import time
from contextlib import contextmanager, closing
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable


HEALTH_STATES = {"HEALTHY", "DEGRADED", "PAUSED", "ERROR"}


_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_root_env(root: Path, *, override: bool = False) -> dict[str, bool]:
    """Load <project-root>/.env without third-party dependencies.

    Existing process/service environment variables win by default. Values are
    never logged or returned; only a boolean loaded/skipped summary is exposed.
    Supported syntax is intentionally small: KEY=value, optional `export `,
    comments/blank lines, and matching single/double quotes around whole values.
    """
    path = root / ".env"
    result = {"present": path.is_file(), "loaded": False}
    if not path.is_file():
        return result
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"invalid .env line {lineno}: expected KEY=value")
        key, value = line.split("=", 1)
        key = key.strip(); value = value.strip()
        if not _ENV_KEY.fullmatch(key):
            raise ValueError(f"invalid .env key on line {lineno}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
    result["loaded"] = True
    return result


def load_config(root: Path) -> dict[str, Any]:
    load_root_env(root)
    path = root / "config" / "project.yaml"
    text = path.read_text(encoding="utf-8")
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # optional convenience only
        except Exception as exc:
            raise ValueError(
                "config/project.yaml must use JSON-compatible YAML when PyYAML is not installed"
            ) from exc
        cfg = yaml.safe_load(text)
    if not isinstance(cfg, dict):
        raise ValueError("config/project.yaml must contain a mapping")
    if cfg.get("mode", {}).get("paper_only") is not True:
        raise ValueError("template requires mode.paper_only=true")
    return cfg


def resolve(root: Path, rel: str) -> Path:
    runtime_dir = os.environ.get("APP_RUNTIME_DIR")
    if runtime_dir and (rel == "runtime" or rel.startswith("runtime/")):
        runtime_path = Path(runtime_dir)
        if not runtime_path.is_absolute():
            raise ValueError("APP_RUNTIME_DIR must be absolute")
        base = runtime_path.resolve()
        target = (base / Path(rel).relative_to("runtime")).resolve()
        if target != base and base not in target.parents:
            raise ValueError("runtime path escapes APP_RUNTIME_DIR")
        return target
    p = (root / rel).resolve()
    rr = root.resolve()
    if p != rr and rr not in p.parents:
        raise ValueError(f"path escapes project root: {rel}")
    return p


@dataclass
class Health:
    component: str
    state: str = "HEALTHY"
    reason: str = "ok"
    pid: int | None = None
    updated_at: float = 0.0
    def __post_init__(self):
        if self.state not in HEALTH_STATES:
            raise ValueError(self.state)
        if not self.updated_at:
            self.updated_at = time.time()


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()
    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()
    def _init(self):
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS kv(scope TEXT,key TEXT,value_json TEXT,PRIMARY KEY(scope,key));
            CREATE TABLE IF NOT EXISTS health(component TEXT PRIMARY KEY,state TEXT,reason TEXT,pid INTEGER,updated_at REAL);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,ts REAL,source TEXT,event TEXT,detail_json TEXT);
            CREATE TABLE IF NOT EXISTS actions(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at REAL,status TEXT,kind TEXT,payload_json TEXT,result_json TEXT);
            """)
    def put(self, scope: str, key: str, value: Any):
        with self.connect() as db:
            db.execute("INSERT INTO kv(scope,key,value_json) VALUES(?,?,?) ON CONFLICT(scope,key) DO UPDATE SET value_json=excluded.value_json",(scope,key,json.dumps(value,sort_keys=True)))
    def get(self, scope: str, key: str, default=None):
        with self.connect() as db:
            row=db.execute("SELECT value_json FROM kv WHERE scope=? AND key=?",(scope,key)).fetchone()
        return default if row is None else json.loads(row[0])
    def delete(self, scope: str, key: str):
        with self.connect() as db:
            db.execute("DELETE FROM kv WHERE scope=? AND key=?",(scope,key))
    def set_health(self, h: Health):
        h.updated_at=time.time()
        with self.connect() as db:
            db.execute("INSERT INTO health(component,state,reason,pid,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(component) DO UPDATE SET state=excluded.state,reason=excluded.reason,pid=excluded.pid,updated_at=excluded.updated_at",(h.component,h.state,h.reason,h.pid,h.updated_at))
    def health(self):
        with self.connect() as db:
            rows=db.execute("SELECT component,state,reason,pid,updated_at FROM health ORDER BY component").fetchall()
        return {r[0]:{"state":r[1],"reason":r[2],"pid":r[3],"updated_at":r[4]} for r in rows}
    def audit(self, source: str, event: str, detail: Any):
        with self.connect() as db:
            db.execute("INSERT INTO audit(ts,source,event,detail_json) VALUES(?,?,?,?)",(time.time(),source,event,json.dumps(detail,sort_keys=True)))
    def propose_action(self, kind: str, payload: Any) -> int:
        with self.connect() as db:
            cur=db.execute("INSERT INTO actions(created_at,status,kind,payload_json,result_json) VALUES(?,?,?,?,?)",(time.time(),"PENDING",kind,json.dumps(payload,sort_keys=True),"{}"))
            return int(cur.lastrowid)
    def actions(self, status: str | None=None):
        q="SELECT id,created_at,status,kind,payload_json,result_json FROM actions"
        args=()
        if status:
            q += " WHERE status=?"; args=(status,)
        q += " ORDER BY id DESC"
        with self.connect() as db:
            rows=db.execute(q,args).fetchall()
        return [{"id":r[0],"created_at":r[1],"status":r[2],"kind":r[3],"payload":json.loads(r[4]),"result":json.loads(r[5])} for r in rows]
    def finish_action(self, action_id: int, status: str, result: Any):
        with self.connect() as db:
            db.execute("UPDATE actions SET status=?,result_json=? WHERE id=?",(status,json.dumps(result,sort_keys=True),action_id))
    def snapshot(self, dest: Path):
        dest.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as src, closing(sqlite3.connect(dest)) as dst:
            src.backup(dst)
        return dest


def import_module(path: Path, name: str):
    spec=importlib.util.spec_from_file_location(name,path)
    if not spec or not spec.loader:
        raise ImportError(path)
    mod=importlib.util.module_from_spec(spec)
    import sys
    sys.modules[name]=mod
    spec.loader.exec_module(mod)
    return mod


AGENT_OPTIONS={
    "provider":["auto","mock","openai","anthropic"],   # auto: Claude if its key is set, else OpenAI, else mock
    "fallback_provider":["none","openai","anthropic","mock"],
    "reasoning":["minimal","low","medium","high","xhigh","max"],
    "response_detail":["concise","normal","detailed"],
    "tool_autonomy":["low","balanced","high"],
    "web_research":["off","automatic"],
    "budget":["low","standard","high"],
}
_DETAIL={"concise":"Keep answers short.","normal":"","detailed":"Give thorough, detailed answers."}
_TOOL_STEPS={"low":3,"balanced":8,"high":16}
_MAX_OUTPUT={"low":4000,"standard":16000,"high":32000}


DEFAULT_MODELS={"openai":"gpt-5.6-sol","anthropic":"claude-opus-5-5"}
# Offered in the Settings dropdowns (a model saved earlier that is not listed stays selectable).
MODEL_CHOICES={"anthropic":["claude-opus-5-5","claude-opus-5","claude-fable-5-1","claude-sonnet-5","claude-haiku-4-5"],
               "openai":["gpt-5.6-sol","gpt-5-nano"]}
OPENAI_EFFORT={"xhigh":"high","max":"high"}  # OpenAI reasoning stops at high

def _model_for(provider: str, model: Any) -> str:
    """Use the configured model when it belongs to the provider; otherwise that provider's default."""
    m=str(model or "")
    if provider=="anthropic": return m if m.startswith("claude") else DEFAULT_MODELS["anthropic"]
    if provider=="openai": return m if m and not m.startswith("claude") else DEFAULT_MODELS["openai"]
    return m

def resolve_provider(provider: Any) -> str:
    """'auto' = the provider whose key is configured (Claude first), else mock; decided at use, so adding keys + restart is enough."""
    p=str(provider or "mock").lower()
    if p!="auto": return p
    if os.getenv("ANTHROPIC_API_KEY"): return "anthropic"
    if os.getenv("OPENAI_API_KEY"): return "openai"
    return "mock"

class AgentProvider:
    """Provider adapter: mock (offline tests), openai, or anthropic, with an optional fallback provider.
    After run(), .used names the provider that answered and .fallback_error why the primary was skipped."""
    def __init__(self, settings: dict[str, Any]):
        self.settings=settings; self.used=None; self.fallback_error=None
        self.usage={"input_tokens":0,"output_tokens":0,"calls":0}  # accumulated over this provider's runs
    def chat(self, prompt: str, *, system: str, allow_web: bool=False) -> str:
        return self.run(prompt,system=system,allow_web=allow_web)
    def run(self, prompt: str, *, system: str, allow_web: bool=False, tools: list[dict]|None=None,
            handler: Callable[[str,dict],Any]|None=None) -> str:
        """One model turn; tool calls are passed to handler and fed back until the model answers."""
        s=self.settings; primary=resolve_provider(s.get("provider","mock")); fb=str(s.get("fallback_provider") or "none").lower()
        fb=resolve_provider(fb) if fb=="auto" else fb
        chain=[(primary,s.get("model"))]+([(fb,s.get("fallback_model"))] if fb not in ("none","",primary) else [])
        errors=[]
        for i,(provider,model) in enumerate(chain):
            try:
                out=self._run_one(provider,{**s,"model":_model_for(provider,model)},prompt,system,allow_web,tools,handler)
                self.used=provider; self.fallback_error=errors[0] if errors else None
                return out
            except Exception as exc:
                errors.append(f"{provider}: {type(exc).__name__}: {str(exc)[:200]}")
                if i==len(chain)-1:
                    if len(chain)==1: raise
                    raise RuntimeError("all AI providers failed - "+" | ".join(errors)) from exc
    def _run_one(self, provider, s, prompt, system, allow_web, tools, handler) -> str:
        if provider=="mock":
            return f"[mock] {prompt[:500]}"
        web=allow_web and str(s.get("web_research","off")).lower()=="automatic"
        steps=_TOOL_STEPS.get(str(s.get("tool_autonomy","balanced")),8)
        system=f"{system} {_DETAIL.get(str(s.get('response_detail','normal')),'')}".strip()
        max_tokens=_MAX_OUTPUT.get(str(s.get("budget","standard")),16000)
        timeout=float(os.getenv("AGENT_TIMEOUT_SECONDS","180")); attempts=max(1,int(os.getenv("AGENT_MAX_ATTEMPTS","2") or 2))
        if provider=="anthropic":
            from core.claude_provider import run_claude
            return run_claude(s,prompt,system,web,tools,handler,max_tokens,steps,timeout,attempts,usage=self.usage)
        if provider!="openai":
            raise RuntimeError(f"unsupported provider: {provider}")
        try:
            from openai import OpenAI
        except Exception as exc:
            raise RuntimeError("OpenAI SDK not installed; run pip install -r config/requirements-openai.txt") from exc
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not configured")
        client=OpenAI()
        all_tools=list(tools or [])
        if web: all_tools.append({"type":"web_search"})
        base={
            "model":s.get("model") or DEFAULT_MODELS["openai"],
            "instructions":system,
            "reasoning":{"effort":OPENAI_EFFORT.get(s.get("reasoning","medium"),s.get("reasoning","medium"))},
            "max_output_tokens":max_tokens,
            "timeout":timeout,
        }
        if all_tools: base["tools"]=all_tools
        def create(**kw):
            for i in range(attempts):
                try:
                    r=client.responses.create(**base,**kw); u=getattr(r,"usage",None); self.usage["calls"]+=1
                    if u is not None:
                        self.usage["input_tokens"]+=int(getattr(u,"input_tokens",0) or 0); self.usage["output_tokens"]+=int(getattr(u,"output_tokens",0) or 0)
                    return r
                except Exception:
                    if i==attempts-1: raise
                    time.sleep(2*(i+1))
        response=create(input=prompt)
        for _ in range(steps):
            calls=[o for o in (getattr(response,"output",None) or []) if getattr(o,"type",None)=="function_call"]
            if not calls or handler is None:
                return response.output_text
            outputs=[]
            for c in calls:
                try: result=handler(c.name,json.loads(c.arguments or "{}"))
                except Exception as exc: result={"error":f"{type(exc).__name__}: {exc}"}
                outputs.append({"type":"function_call_output","call_id":c.call_id,"output":json.dumps(result,default=str)[:30000]})
            response=create(input=outputs,previous_response_id=response.id)
        return (response.output_text or "")+"\n\n_(Stopped: tool step limit reached. Raise Tool Autonomy in Settings if needed.)_"


def default_agent_settings(cfg: dict[str,Any], who: str) -> dict[str,Any]:
    base={"provider":cfg.get("agents",{}).get("default_provider","mock")}
    base.update(cfg.get("agents",{}).get(who,{}))
    return base


def backup_file(root: Path, target: Path, tag: str) -> Path:
    rel=target.resolve().relative_to(root.resolve())
    dest=resolve(root,f"runtime/backups/{tag}/{rel.as_posix()}")
    dest.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(target,dest)
    return dest
