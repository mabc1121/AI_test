"""The research loop's study state machine. SQLite is the authority for where each study is; the study folder
holds the artifacts (study.json manifest, evidence, each member's outputs, team documents).

Stages: evidence -> science -> building -> testing -> evaluating -> done   (evaluating -> testing when a shadow is extended)
"""
from __future__ import annotations
import hashlib, json, sys, time
from pathlib import Path

from agents.unit import BudgetExceeded, Member, UnitFailed
from think.evaluators import ready, run_evaluators, systematic_checks
from think.evidence import evidence_pack
from think.scientists import run_scientists
from think.workers import build_code, build_params, forward_return_probe, triage

# Default seats (editable: project.yaml "lab", then the database via Lab.update_seats - no code change needed).
# A fallback is used only when the seat's provider is unavailable, and is always recorded as a substitute.
PAIR = [{"name": "seat-1", "provider": "anthropic", "model": "claude-opus-5-5", "reasoning": "high", "fallback": None},
        {"name": "seat-2", "provider": "openai", "model": "gpt-5.6-sol", "reasoning": "high",
         "fallback": {"provider": "anthropic", "model": "claude-sonnet-5", "reasoning": "high"}}]
PROVIDERS = ("anthropic", "openai")
# Loop settings: name -> (default, type, min, max). The Supervisor may propose changes; the user approves them.
SETTINGS = {"auto": (True, bool, None, None),                       # the loop starts and advances studies by itself
            "budget_usd_per_day": (10.0, float, 0.0, 200.0),        # every AI call counts (studies, reviews, chat)
            "max_active_studies": (1, int, 1, 3),                   # studies using AI at once (testing ones wait on shadow slots)
            "min_hours_between_studies": (12.0, float, 0.0, 168.0),
            "step_minutes": (15.0, float, 5.0, 240.0),              # how often the loop looks for work
            "reevaluate_hours": (24.0, float, 1.0, 168.0)}          # after an "extend", judge again no sooner than this
DEFAULT_LAB = {
    **{k: v[0] for k, v in SETTINGS.items()},
    "groups": {
        "scientists": {"rounds": 2, "max_hypotheses": 3, "members": PAIR},
        "workers": {"rounds": 2, "replay_hours": 6, "probe_max_days": 7, "members": PAIR},
        "evaluators": {"rounds": 2, "shadow_max_days": None, "members": PAIR},   # days before a shadow is reviewed anyway - never removed by time (None: Think control)
        # chat: one seat (None = the Manage agent settings page); members: the pair for the daily review
        "supervisor": {"rounds": 1, "review_hour_utc": 1, "chat": None, "members": PAIR}},
}
ACTIVE = ("evidence", "science", "science_done", "building", "evaluating")
SCHEMA = """
CREATE TABLE IF NOT EXISTS studies(id TEXT PRIMARY KEY, created REAL, updated REAL, stage TEXT, status TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS agent_outputs(id INTEGER PRIMARY KEY AUTOINCREMENT, study_id TEXT, grp TEXT, round INTEGER, member TEXT,
    provider TEXT, model TEXT, prompt TEXT, status TEXT, error TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, created REAL);
CREATE TABLE IF NOT EXISTS hypothesis_outcomes(id INTEGER PRIMARY KEY AUTOINCREMENT, study_id TEXT, hkey TEXT, title TEXT, author TEXT, provider TEXT, model TEXT,
    kind TEXT, confidence REAL, agreement TEXT, outcome TEXT, action TEXT, created REAL);
CREATE TABLE IF NOT EXISTS evaluator_verdicts(id INTEGER PRIMARY KEY AUTOINCREMENT, study_id TEXT, hkey TEXT, member TEXT, provider TEXT, model TEXT,
    outcome TEXT, recommendation TEXT, team_outcome TEXT, team_action TEXT, created REAL);
"""


class Lab:
    def __init__(self, think):
        self.think = think; self.store = think.store; self.dir = think.think_dir / "studies"
        self.cfg = self.load_config()
        with self.store.connect() as db: db.executescript(SCHEMA)

    # ---------------- configuration (defaults <- project.yaml "lab" <- database) ----------------
    def load_config(self) -> dict:
        import copy
        cfg = copy.deepcopy(DEFAULT_LAB)
        for over in (self.think.cfg.get("lab") or {}, self.store.get("lab", "config", {}) or {}):
            for k, (_, typ, _, _) in SETTINGS.items():
                if k in over: cfg[k] = typ(over[k])
            for g, gc in (over.get("groups") or {}).items(): cfg["groups"][g] = {**cfg["groups"].get(g, {}), **gc}
        return cfg

    @staticmethod
    def check_settings(values: dict) -> dict:
        out = {}
        for k, v in values.items():
            if k not in SETTINGS: raise ValueError(f"unknown lab setting {k}")
            _, typ, lo, hi = SETTINGS[k]
            if typ is bool:
                if v not in (True, False, "true", "false", 1, 0): raise ValueError(f"{k} must be true or false")
                out[k] = v in (True, "true", 1)
            else:
                out[k] = typ(v)
                if not lo <= out[k] <= hi: raise ValueError(f"{k} must be between {lo} and {hi}")
        return out

    def update_settings(self, values: dict) -> dict:
        clean = self.check_settings(values); saved = self.store.get("lab", "config", {}) or {}; saved.update(clean)
        self.store.put("lab", "config", saved); self.cfg = self.load_config(); return {k: self.cfg[k] for k in SETTINGS}

    @staticmethod
    def _check_model(m: dict, where: str) -> dict:
        import re
        if m.get("provider") not in PROVIDERS: raise ValueError(f"{where}: provider must be one of {PROVIDERS}")
        if not re.fullmatch(r"[\w.\-:]{1,64}", str(m.get("model", ""))): raise ValueError(f"{where}: invalid model name")
        if m.get("reasoning", "high") not in ("minimal", "low", "medium", "high", "xhigh", "max"): raise ValueError(f"{where}: invalid reasoning")
        return {"provider": m["provider"], "model": m["model"], "reasoning": m.get("reasoning", "high")}

    def check_seats(self, group: str, seats: list[dict], key: str = "members") -> list[dict]:
        if group not in self.cfg["groups"]: raise ValueError(f"unknown group {group}")
        if key not in ("members", "chat") or (key == "chat" and group != "supervisor"): raise ValueError(f"unknown seat list {group}.{key}")
        if not seats: raise ValueError("a group needs at least one seat")
        if key == "chat" and len(seats) != 1: raise ValueError("the Supervisor chat uses exactly one seat (with an optional fallback)")
        clean, names = [], set()
        for i, s in enumerate(seats):
            name = str(s.get("name") or f"seat-{i + 1}")
            if name in names: raise ValueError(f"duplicate seat name {name}")
            names.add(name); fb = s.get("fallback")
            clean.append({"name": name, **self._check_model(s, name), "fallback": self._check_model(fb, f"{name}.fallback") if fb else None})
        return clean

    def update_seats(self, group: str, seats: list[dict], key: str = "members") -> dict:
        """Set a group's seats (1 or more; same or different providers), each with an optional fallback."""
        clean = self.check_seats(group, seats, key)
        saved = self.store.get("lab", "config", {}) or {}; saved.setdefault("groups", {}).setdefault(group, {})[key] = clean[0] if key == "chat" else clean
        self.store.put("lab", "config", saved); self.cfg = self.load_config(); return self.cfg["groups"][group]

    # ---------------- bookkeeping ----------------
    def members(self, group: str) -> list[Member]:
        return [Member(**m) for m in self.cfg["groups"][group]["members"]]

    def spent_today(self) -> float:
        day0 = time.time() // 86400 * 86400
        with self.store.connect() as db: return float(db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM agent_outputs WHERE created>=?", (day0,)).fetchone()[0])

    def budget_ok(self) -> bool: return self.spent_today() < float(self.cfg["budget_usd_per_day"])

    def record(self, sid: str):
        def rec(o: dict) -> None:
            with self.store.connect() as db:
                db.execute("INSERT INTO agent_outputs(study_id,grp,round,member,provider,model,prompt,status,error,input_tokens,output_tokens,cost_usd,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (sid, o["group"], o["round"], o["member"], o["provider"], o["model"], o["prompt"], o["status"], o["error"],
                            o["usage"]["input_tokens"], o["usage"]["output_tokens"], o["cost_usd"] or 0.0, o["finished"]))
        return rec

    def set_stage(self, sid: str, stage: str, status: str = "running", error: str | None = None) -> None:
        with self.store.connect() as db: db.execute("UPDATE studies SET stage=?,status=?,error=?,updated=? WHERE id=?", (stage, status, error, time.time(), sid))
        self._manifest(sid, **{"stage": stage, "status": status, "error": error, f"at_{stage}": time.time()})

    def study(self, sid: str) -> dict:
        with self.store.connect() as db: r = db.execute("SELECT id,created,updated,stage,status,error FROM studies WHERE id=?", (sid,)).fetchone()
        return dict(zip(("id", "created", "updated", "stage", "status", "error"), r)) if r else {}

    def studies(self, n: int = 20) -> list[dict]:
        with self.store.connect() as db: rows = db.execute("SELECT id,created,updated,stage,status,error FROM studies ORDER BY created DESC LIMIT ?", (n,)).fetchall()
        return [dict(zip(("id", "created", "updated", "stage", "status", "error"), r)) for r in rows]

    def _manifest(self, sid: str, **updates) -> dict:
        p = self.dir / sid / "study.json"; m = json.loads(p.read_text()) if p.is_file() else {}
        m.update(updates); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(m, indent=1, default=str)); return m

    def cost(self, sid: str) -> dict:
        with self.store.connect() as db:
            r = db.execute("SELECT COALESCE(SUM(cost_usd),0),COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0),COUNT(*) FROM agent_outputs WHERE study_id=?", (sid,)).fetchone()
        return {"usd": round(r[0], 4), "input_tokens": r[1], "output_tokens": r[2], "agent_outputs": r[3]}

    # ---------------- stages ----------------
    def new_study(self) -> str:
        sid = time.strftime("s%Y%m%d-%H%M%S", time.gmtime()) + "-" + hashlib.sha256(f"{time.time_ns()}".encode()).hexdigest()[:4]
        champ_text = self.think.trade_file.read_bytes(); ch = self.store.get("think", "champion", {}) or {}
        with self.store.connect() as db: db.execute("INSERT INTO studies(id,created,updated,stage,status) VALUES(?,?,?,?,?)", (sid, time.time(), time.time(), "evidence", "running"))
        self._manifest(sid, id=sid, created=time.time(), stage="evidence", status="running",
                       champion={"id": ch.get("id", "baseline"), "sha256": hashlib.sha256(champ_text).hexdigest()},
                       groups={g: [vars(m) for m in self.members(g)] for g in self.cfg["groups"]})
        return sid

    def run_science(self, sid: str, tools: list[dict], handler, call=None) -> dict:
        d = self.dir / sid; ev_path = d / "evidence.json"
        if not ev_path.is_file():
            ev = evidence_pack(self.think); ev_path.write_text(json.dumps(ev, indent=1, default=str))
            rec = ev.get("recordings") or {}
            self._manifest(sid, data_window={"generated_at": ev["generated_at"], "recordings": rec, "tt_input_quality": ev.get("data_quality")})
        ev = json.loads(ev_path.read_text()); self.set_stage(sid, "science")
        g = self.cfg["groups"]["scientists"]
        try:
            brief = run_scientists(self.members("scientists"), ev, d, self.record(sid), self.budget_ok, tools, handler,
                                   rounds=int(g["rounds"]), max_hypotheses=int(g["max_hypotheses"]), slots=self.think._free_slots() or 1, call=call)
        except BudgetExceeded as exc:
            self.set_stage(sid, "science", "paused", str(exc)); raise
        except UnitFailed as exc:
            self.set_stage(sid, "science", "failed", str(exc)); raise
        self._manifest(sid, prompts={"scientists": brief["prompt"]}, reduced_independence={"scientists": brief["reduced_independence"]},
                       hypotheses=[{"key": h["key"], "title": h.get("title"), "agreement": h["agreement"], "selected": h["key"] in brief["selected"]} for h in brief["hypotheses"]],
                       cost=self.cost(sid))
        self.set_stage(sid, "science_done", "waiting_for_workers")
        return brief

    def run_build(self, sid: str, tools: list[dict], handler, call=None) -> dict:
        """Workers: each selected hypothesis -> exactly one trade.py (started as a live shadow) or a probe result."""
        d = self.dir / sid; brief = json.loads((d / "brief.json").read_text()); g = self.cfg["groups"]["workers"]
        by_key = {h["key"]: h for h in brief["hypotheses"]}; self.set_stage(sid, "building"); cards = {}
        for key in brief["selected"]:
            h = by_key[key]; hid = str(h.get("id")); out = d / "build" / hid; out.mkdir(parents=True, exist_ok=True)
            card_path = out / "build_card.json"
            if card_path.is_file(): cards[key] = json.loads(card_path.read_text()); continue   # resume
            route, note = triage(h)
            try:
                if route == "params": card = build_params(self.think, h, out, float(g["replay_hours"]))
                elif route == "code": card = build_code(self.think, h, out, self.members("workers"), self.record(sid), self.budget_ok, tools, handler,
                                                        float(g["replay_hours"]), int(g["rounds"]), call=call)
                elif route == "probe": card = self._probe(h)
                else: card = {"status": "skipped", "why": f"{route} hypotheses need no build"}
            except BudgetExceeded as exc:
                self.set_stage(sid, "building", "paused", str(exc)); raise
            card.update(hypothesis=key, title=h.get("title"), triage_note=note, built_at=time.time())
            if card.get("status") == "built":
                card["shadow"] = self.start_shadow(sid, h, card, out); card["shadow_pending"] = card["shadow"] is None   # waits for a slot
            card_path.write_text(json.dumps(card, indent=1, default=str)); cards[key] = card
        testing = [k for k, c in cards.items() if c.get("shadow") or c.get("shadow_pending") or (c.get("route") == "probe" and c.get("complete") is False)]
        self._manifest(sid, builds={k: {"status": c.get("status"), "route": c.get("route"), "why": c.get("why"), "shadow": c.get("shadow")} for k, c in cards.items()},
                       cost=self.cost(sid))
        self.set_stage(sid, "testing" if testing else "built", "running" if testing else "waiting_for_evaluators")
        return cards

    def shadow_room(self) -> str | None:
        """Why a new shadow cannot start now (None = it can): a free slot, and Trade healthy with the server not overloaded -
        every shadow runs inside the live Trade process."""
        import os
        if self.think._free_slots() <= 0: return "no free shadow slot"
        h = (self.store.health() or {}).get("trade") or {}
        if h.get("state") != "HEALTHY" or time.time() - float(h.get("updated_at") or 0) > 30: return f"Trade not healthy ({h.get('state')}: {str(h.get('reason'))[:80]})"
        load, cpus = os.getloadavg()[0], os.cpu_count() or 1
        if load > 3.5 * cpus: return f"server overloaded (load {load:.1f} on {cpus} CPUs)"
        return None

    def start_shadow(self, sid: str, h: dict, card: dict, out: Path) -> str | None:
        """Hand a built trade.py to the live shadow pool (the trade worker picks it up); None if it has to wait (shadow_room)."""
        if self.shadow_room(): return None
        src = (out / "trade.py").read_bytes(); champ = self.think.trade_file.read_bytes(); cid = f"{sid}-{h.get('id')}"
        entry = {"id": cid, "study_id": sid, "key": h["key"], "kind": card["route"], "hypothesis": h.get("title"), "evidence": h.get("evidence"),
                 "expected_effect": json.dumps(h.get("predictions")), "preregistration": {k: h.get(k) for k in ("predictions", "min_trades", "kill_rule", "falsified_if", "confidence")},
                 "overrides": card.get("overrides") or {}, "source_path": str(out / "trade.py") if card["route"] == "code" else None,
                 "source_sha": hashlib.sha256(src if card["route"] == "code" else champ).hexdigest(), "base_sha": hashlib.sha256(champ).hexdigest(),
                 "screen": {"status": "WORKERS", "why": card.get("summary") or card.get("route")}, "created_at": time.time(), "started_ms": int(time.time() * 1000), "status": "SHADOW"}
        pool = self.think.pool(); pool.append(entry); self.think._save_pool(pool)
        self.store.put("think", "tested", int(self.store.get("think", "tested", 0) or 0) + 1)
        self.think.note("shadow_started", candidate=cid, hypothesis=h.get("title"), kind=card["route"], params=entry["overrides"], verdict="WORKERS", why=f"study {sid}")
        return cid

    # ---------------- evaluation ----------------
    def max_days(self) -> float:
        return float(self.cfg["groups"]["evaluators"].get("shadow_max_days") or self.think.control()["shadow_max_days"])

    def subjects(self, sid: str) -> dict[str, dict]:
        """What the Evaluators judge: each selected hypothesis with its pre-registration, build and results (H1, H2...)."""
        d = self.dir / sid; brief = json.loads((d / "brief.json").read_text()); by_key = {h["key"]: h for h in brief["hypotheses"]}
        pool = {p["id"]: p for p in self.think.pool()}; out = {}
        for i, key in enumerate(brief["selected"], 1):
            h = by_key[key]; cp = d / "build" / str(h.get("id")) / "build_card.json"
            card = json.loads(cp.read_text()) if cp.is_file() else {"status": "not_built"}
            entry = pool.get(card.get("shadow") or ""); live = bool(entry and entry.get("status") == "SHADOW")
            out[f"H{i}"] = {"key": key, "title": h.get("title"), "kind": h.get("kind"), "route": card.get("route"),
                            "hypothesis": {k: h.get(k) for k in ("change", "rationale", "evidence", "agreement")},
                            "preregistration": {k: h.get(k) for k in ("predictions", "min_trades", "kill_rule", "falsified_if", "confidence")},
                            "build": {k: card.get(k) for k in ("status", "why", "summary", "changed_lines", "ambiguities", "same_decisions_as_champion", "overrides", "reduced_independence")},
                            "probe": {k: card.get(k) for k in ("days", "max_days", "min_signals", "complete", "signals", "cost_bps", "horizons", "best_horizon", "best", "any_horizon_beats_cost")} if card.get("route") == "probe" else None,
                            "age_days": round((time.time() - float(card.get("first_built_at") or card.get("built_at") or time.time())) / 86400, 2),
                            "shadow": card.get("shadow") if live else None, "shadow_id": card.get("shadow"), "pending": bool(card.get("shadow_pending") and not card.get("shadow")), "shadow_status": (entry or {}).get("status"),
                            "gate": self.think.evaluate(entry) if live else (entry or {}).get("evaluation")}
        return out

    def _probe(self, h: dict) -> dict:
        """A probe runs until its pre-registered number of signals, within its time cap (at most probe_max_days)."""
        g = self.cfg["groups"]["workers"]
        return forward_return_probe(self.think, min_signals=int(h.get("min_trades") or 30), max_days=min(7.0, float(g.get("probe_max_days", 7))))

    def _start_pending(self, sid: str) -> None:
        """Builds that waited for a slot start as soon as there is room."""
        d = self.dir / sid; brief = json.loads((d / "brief.json").read_text()) if (d / "brief.json").is_file() else {"hypotheses": []}
        for h in brief["hypotheses"]:
            out = d / "build" / str(h.get("id")); p = out / "build_card.json"
            if not p.is_file(): continue
            card = json.loads(p.read_text())
            if not card.get("shadow_pending") or card.get("shadow"): continue
            cid = self.start_shadow(sid, h, card, out)
            if not cid: return
            card.update(shadow=cid, shadow_pending=False, shadow_started_at=time.time()); p.write_text(json.dumps(card, indent=1, default=str))
            self._manifest(sid, builds={**(self._manifest(sid).get("builds") or {}), h["key"]: {"status": card.get("status"), "route": card.get("route"), "why": card.get("why"), "shadow": cid}})

    def pending_shadows(self) -> int:
        return sum(1 for s in self.studies(50) if s["stage"] == "testing" for p in (self.dir / s["id"]).glob("build/*/build_card.json")
                   if (lambda c: c.get("shadow_pending") and not c.get("shadow"))(json.loads(p.read_text())))

    def _refresh_probes(self, sid: str) -> None:
        """A probe that ran out of recorded data is measured again (on more data) every reevaluate_hours."""
        d = self.dir / sid; brief = json.loads((d / "brief.json").read_text()) if (d / "brief.json").is_file() else {"hypotheses": []}
        for h in brief["hypotheses"]:
            p = d / "build" / str(h.get("id")) / "build_card.json"
            if not p.is_file(): continue
            card = json.loads(p.read_text())
            if card.get("route") != "probe" or card.get("complete") is not False: continue
            if time.time() - float(card.get("measured_at") or 0) < 3600 * float(self.cfg["reevaluate_hours"]): continue
            new = self._probe(h); new.update({k: card.get(k) for k in ("hypothesis", "title", "triage_note")}, first_built_at=card.get("first_built_at") or card.get("built_at"), built_at=time.time())
            p.write_text(json.dumps(new, indent=1, default=str))
            self.think.note("probe", why=f"study {sid} {h.get('id')}: {new['signals']} of {new['min_signals']} signals over {new['days']} days")

    def history(self, exclude: str | None = None, n: int = 10) -> list[dict]:
        keep = ("id", "stage", "status", "hypotheses", "builds", "reduced_independence", "evaluation")
        out = []
        for s in self.studies(n + 1):
            if s["id"] == exclude: continue
            p = self.dir / s["id"] / "study.json"; m = json.loads(p.read_text()) if p.is_file() else {"id": s["id"]}
            out.append({**{k: m.get(k) for k in keep if k in m}, "data_window": {"tt_input_quality": (m.get("data_window") or {}).get("tt_input_quality")}})
        return out[:n]

    def outcomes(self, n: int = 100) -> list[dict]:
        cols = ("study_id", "hkey", "title", "author", "provider", "model", "kind", "confidence", "agreement", "outcome", "action", "created")
        with self.store.connect() as db: rows = db.execute(f"SELECT {','.join(cols)} FROM hypothesis_outcomes ORDER BY created DESC LIMIT ?", (n,)).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def run_evaluate(self, sid: str, tools: list[dict], handler, call=None, force: bool = False) -> dict:
        """Evaluators judge the study once every shadow is ready (gate decided, pre-registered trades reached, or time out)."""
        st = self.study(sid)
        if st.get("stage") not in ("testing", "built", "evaluating"): raise ValueError(f"study {sid} is at stage {st.get('stage')}, not ready for evaluation")
        self._refresh_probes(sid); self._start_pending(sid)
        subjects = self.subjects(sid); max_days = self.max_days(); m = self._manifest(sid)
        if not subjects:
            self._manifest(sid, evaluation={"pass": 0, "note": "nothing was selected for testing"}); self.set_stage(sid, "done", "complete"); return {"ready": True, "subjects": {}}
        waiting = {s: (x.get("gate") or {}).get("why") for s, x in subjects.items() if not ready(x, max_days)}
        if not force and st.get("stage") == "testing" and float(m.get("next_evaluation_at") or 0) > time.time():
            waiting = waiting or {s: "extended by the evaluators; judged again after " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(m["next_evaluation_at"])) for s in subjects}
        if waiting and not force: return {"ready": False, "waiting": waiting}
        n = int(m.get("evaluation_passes", 0)) + 1; out = self.dir / sid / "evaluation" / f"pass{n}"
        history = self.history(exclude=sid); code_issues = systematic_checks(history, self.outcomes())
        self.set_stage(sid, "evaluating"); g = self.cfg["groups"]["evaluators"]
        try:
            ev = run_evaluators(self.members("evaluators"), subjects, history, code_issues, out, self.record(sid), self.budget_ok, tools, handler,
                                rounds=int(g["rounds"]), max_days=max_days, call=call)
        except BudgetExceeded as exc:
            self.set_stage(sid, "evaluating", "paused", str(exc)); raise
        except UnitFailed as exc:
            self.set_stage(sid, "evaluating", "failed", str(exc)); raise
        (out / "evaluation.json").write_text(json.dumps(ev, indent=1, default=str))
        self._apply(sid, subjects, ev)
        extend = any(x["action"] in ("extend", "hold") for x in ev["subjects"].values())   # extended/held: judged again after reevaluate_hours
        self._manifest(sid, evaluation_passes=n, prompts={**(self._manifest(sid).get("prompts") or {}), "evaluators": ev["prompt"]},
                       reduced_independence={**(self._manifest(sid).get("reduced_independence") or {}), "evaluators": ev["reduced_independence"]},
                       evaluation={"pass": n, "outcomes": {x["key"]: x["outcome"] for x in ev["subjects"].values()},
                                   "actions": {x["key"]: x["action"] for x in ev["subjects"].values()},
                                   "back_to_scientists": [x["key"] for x in ev["subjects"].values() if x["back_to_scientists"]],
                                   "issues": [{k: i.get(k) for k in ("issue", "owner", "severity", "by")} for i in ev["systematic_issues"]]},
                       cost=self.cost(sid), next_evaluation_at=time.time() + 3600 * float(self.cfg["reevaluate_hours"]) if extend else None)
        self.set_stage(sid, "testing" if extend else "done", "extended" if extend else "complete")
        return ev

    def _apply(self, sid: str, subjects: dict, ev: dict) -> None:
        """Carry out the team actions; record outcomes (final ones only) for scorecards; issues and lessons go to the notebook."""
        pool = self.think.pool(); by_id = {p["id"]: p for p in pool}; promote = []; now = time.time()
        seats = {s["member"]: s for s in ev["members"]}
        for sub, x in ev["subjects"].items():
            subj = subjects[sub]; entry = by_id.get(subj.get("shadow") or "")
            if entry is not None and entry.get("status") == "SHADOW":
                if x["action"] == "retire": entry.update(status="RETIRED", verdict="EVALUATORS", why=x["why"])
                elif x["action"] == "hold": entry["hold"] = {"study": sid, "why": x["why"]}
                elif x["action"] == "promote": promote.append((entry, subj["gate"]))
            self.think.note("verdict", candidate=subj.get("shadow_id"), hypothesis=x["title"], verdict=x["action"].upper(), why=x["why"],
                            lesson=f"{x['outcome']} (evaluators {x['agreement']}); study {sid}")
            if x["action"] == "extend": continue
            author = x["key"].split(":")[0]; pre = subj["preregistration"]
            with self.store.connect() as db:
                r = db.execute("SELECT provider,model FROM agent_outputs WHERE study_id=? AND grp='scientists' AND member=? AND status='ok' ORDER BY round DESC LIMIT 1", (sid, author)).fetchone() or (None, None)
                db.execute("INSERT INTO hypothesis_outcomes(study_id,hkey,title,author,provider,model,kind,confidence,agreement,outcome,action,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           (sid, x["key"], x["title"], author, r[0], r[1], subj.get("kind"), pre.get("confidence"), subj["hypothesis"].get("agreement"), x["outcome"], x["action"], now))
                for m, v in x["verdicts"].items():
                    s = seats.get(m, {})
                    db.execute("INSERT INTO evaluator_verdicts(study_id,hkey,member,provider,model,outcome,recommendation,team_outcome,team_action,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (sid, x["key"], m, s.get("provider"), s.get("model"), v.get("outcome"), v.get("recommendation"), x["outcome"], x["action"], now))
        self.think._save_pool(pool)
        for entry, gate in promote: self.think._propose_promotion(entry, gate)   # still needs approval in Manage
        if promote: self.think._save_pool(pool)
        for l in ev["lessons"]: self.think.note("lesson", lesson=l["lesson"], why=f"study {sid} by {l['by']}")
        self.store.put("lab", "issues", {"study": sid, "at": now, "issues": ev["systematic_issues"]})

    def scorecards(self) -> dict:
        """Per seat and model: reliability and cost (every group), hypothesis track record (scientists), agreement (evaluators)."""
        with self.store.connect() as db:
            seats = db.execute("SELECT grp,member,provider,model,COUNT(*),SUM(status='ok'),ROUND(COALESCE(SUM(cost_usd),0),4) FROM agent_outputs GROUP BY grp,member,provider,model").fetchall()
            sci = db.execute("SELECT author,provider,model,COUNT(*),SUM(outcome='supported'),SUM(outcome='falsified'),SUM(outcome='inconclusive'),"
                             "AVG(CASE WHEN outcome IN ('supported','falsified') THEN (confidence-(outcome='supported'))*(confidence-(outcome='supported')) END) "
                             "FROM hypothesis_outcomes GROUP BY author,provider,model").fetchall()
            evs = db.execute("SELECT member,provider,model,COUNT(*),SUM(outcome=team_outcome) FROM evaluator_verdicts GROUP BY member,provider,model").fetchall()
        return {"seats": [dict(zip(("group", "member", "provider", "model", "outputs", "ok", "cost_usd"), r)) for r in seats],
                "scientists": [{**dict(zip(("member", "provider", "model", "judged", "supported", "falsified", "inconclusive"), r[:7])),
                                "brier": None if r[7] is None else round(r[7], 3)} for r in sci],
                "evaluators": [{**dict(zip(("member", "provider", "model", "verdicts"), r[:4])), "agrees_with_team_pct": round(100 * (r[4] or 0) / r[3], 1) if r[3] else None} for r in evs]}

    # ---------------- the automatic loop (called by the Think worker) ----------------
    def advance(self, tools: list[dict], handler, call=None) -> dict:
        """One step: resume or advance the oldest unfinished study, else start a new one when the limits allow.
        Every stage resumes from disk, so a crash or restart continues where it stopped."""
        self.cfg = self.load_config()
        if not self.cfg["auto"]: return {"step": "idle", "why": "automatic loop is off"}
        if not self.budget_ok(): return {"step": "idle", "why": f"daily budget reached (${self.spent_today():.2f})"}
        for s in reversed(self.studies(50)):          # oldest first
            sid, stage = s["id"], s["stage"]
            if s["status"] == "failed" or stage == "done": continue
            try:
                if stage in ("evidence", "science"): self.run_science(sid, tools, handler, call=call); return {"step": "science", "study": sid}
                if stage in ("science_done", "building"): self.run_build(sid, tools, handler, call=call); return {"step": "build", "study": sid}
                if stage in ("testing", "built", "evaluating"):
                    ev = self.run_evaluate(sid, tools, handler, call=call, force=stage == "evaluating")
                    if ev.get("ready") is False: continue
                    return {"step": "evaluate", "study": sid}
            except BudgetExceeded as exc:
                return {"step": "idle", "why": str(exc)}
            except UnitFailed as exc:
                return {"step": "failed", "study": sid, "why": str(exc)}
            except Exception as exc:  # transient errors are retried on later steps; the third one fails the study
                n = int(self._manifest(sid).get("retries", 0)) + 1; err = f"{type(exc).__name__}: {str(exc)[:300]}"
                self._manifest(sid, retries=n); self.set_stage(sid, self.study(sid)["stage"], "failed" if n >= 3 else "error", err)
                self.think.note("error", why=f"study {sid} ({stage}) attempt {n}: {err}")
                return {"step": "error", "study": sid, "why": err}
        active = [s for s in self.studies(50) if s["stage"] in ACTIVE and s["status"] != "failed"]
        last = max((s["created"] for s in self.studies(1)), default=0)
        if len(active) >= int(self.cfg["max_active_studies"]): return {"step": "idle", "why": "maximum active studies running"}
        if self.pending_shadows() >= 2: return {"step": "idle", "why": "2 built candidates are already waiting for a shadow slot"}
        if time.time() - last < 3600 * float(self.cfg["min_hours_between_studies"]): return {"step": "idle", "why": "too soon after the last study"}
        sid = self.new_study(); self.think.note("study_started", why=f"study {sid} started by the Supervisor loop")
        try: self.run_science(sid, tools, handler, call=call)
        except (BudgetExceeded, UnitFailed) as exc: return {"step": "science", "study": sid, "why": str(exc)}
        return {"step": "science", "study": sid, "new": True}


def lab_tools(think):
    """The read-only tools every Lab member gets: the strategy source and the recorded market."""
    from think.think import MARKET_TOOLS
    from manage.manage import ManageEngine, TOOLS as MT
    m = ManageEngine(think.root)
    def handler(name, args):
        if name == "read_file": return m.read_file(args["path"], args.get("start_line", 1), args.get("max_lines", 300))
        if name == "search_files": return m.search_files(args["pattern"], args.get("path", "trade"))
        if name == "market_candles": return think.market_candles(args["start"], args.get("minutes", 60), args.get("bar_seconds", 60))
        if name == "find_moves": return think.find_moves(args.get("hours_back", 24), float(args["min_move_bps"]), args.get("window_minutes", 10))
        raise ValueError(name)
    return [x for x in MT if x["name"] in ("read_file", "search_files")] + MARKET_TOOLS, handler


if __name__ == "__main__":  # manual run: python -m think.loop science
    ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
    from think.think import ThinkEngine
    t = ThinkEngine(ROOT); lab = Lab(t); tools, handler = lab_tools(t)
    if len(sys.argv) > 1 and sys.argv[1] == "step":  # python -m think.loop step  (one automatic loop step)
        print(json.dumps(lab.advance(tools, handler), indent=1, default=str)); sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "evaluate":  # python -m think.loop evaluate <study_id> [--force]
        ev = lab.run_evaluate(sys.argv[2], tools, handler, force="--force" in sys.argv)
        print(json.dumps(ev if ev.get("ready") is False else {"study": sys.argv[2], "stage": lab.study(sys.argv[2]), "cost": lab.cost(sys.argv[2]),
                          "subjects": {s: {k: x[k] for k in ("key", "outcome", "agreement", "action", "why", "back_to_scientists")} for s, x in ev["subjects"].items()},
                          "issues": [(i["owner"], i["severity"], i["issue"], i["by"]) for i in ev["systematic_issues"]]}, indent=1, default=str))
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "scorecards":
        print(json.dumps(lab.scorecards(), indent=1)); sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "build":  # python -m think.loop build <study_id>
        cards = lab.run_build(sys.argv[2], tools, handler)
        print(json.dumps({"study": sys.argv[2], "stage": lab.study(sys.argv[2]), "cost": lab.cost(sys.argv[2]),
                          "builds": {k: {x: c.get(x) for x in ("status", "route", "why", "triage_note", "shadow", "signals", "best_horizon", "best", "any_horizon_beats_cost", "horizons")} for k, c in cards.items()}}, indent=1, default=str))
        sys.exit(0)
    if len(sys.argv) > 2 and sys.argv[2]: lab.cfg["groups"]["scientists"]["members"] = json.loads(sys.argv[2])
    sid = lab.new_study(); print("study", sid)
    brief = lab.run_science(sid, tools, handler)
    print(json.dumps({"study": sid, "stage": lab.study(sid), "cost": lab.cost(sid), "rounds": brief["rounds"], "reduced_independence": brief["reduced_independence"],
                      "unavailable": brief["unavailable"], "hypotheses": [(h["key"], h.get("title"), h["agreement"]) for h in brief["hypotheses"]],
                      "selected": brief["selected"]}, indent=1, default=str))
