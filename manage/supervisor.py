"""The Supervisor: the one agent the user talks to (it replaces the Manage chat; Manage's actions, approvals,
validation and git history are its tool layer).

* Sees the whole system: component health, Trade, the Lab loop (studies, stages, spend), shadow pool,
  systematic issues, seat scorecards, directives and pending approvals.
* Steers research with directives (seen by every Scientist); everything else is a proposal the user approves:
  file edits, restarts, Lab settings and seats.
* Chat runs on one seat (with fallback); the daily review runs on the supervisor pair and is merged like every
  other group. Every AI call is recorded and counts against the Lab's daily budget.
"""
from __future__ import annotations
import json, re, time
from pathlib import Path

from agents.documents import check_review, check_review_exchange, cost_usd
from agents.unit import Member, Unit, prompt_identity, run_seat
from core.common import _model_for, default_agent_settings, resolve_provider

TOOLS = [
    {"type": "function", "name": "lab_overview", "description": "The research Lab: loop settings, spend vs budget, recent studies and their stages, shadow pool, systematic issues, directives, seats, last daily review.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "read_study", "description": "One study in detail. part: manifest, brief (Scientists), builds (Workers), evaluation (Evaluators), or scorecards (all seats, any study_id).",
     "parameters": {"type": "object", "properties": {"study_id": {"type": "string"}, "part": {"type": "string", "enum": ["manifest", "brief", "builds", "evaluation", "scorecards"]}},
                    "required": ["study_id", "part"], "additionalProperties": False}},
    {"type": "function", "name": "set_directive", "description": "Give the Scientists a research directive (seen in every study's evidence). Active for `days` (1-30).",
     "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "reason": {"type": "string"}, "days": {"type": "integer"}},
                    "required": ["text", "reason", "days"], "additionalProperties": False}},
    {"type": "function", "name": "clear_directive", "description": "Remove an active directive by id.",
     "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}},
    {"type": "function", "name": "propose_retire_shadow", "description": "Propose stopping a live shadow candidate early (frees its slot). The user must approve it.",
     "parameters": {"type": "object", "properties": {"candidate": {"type": "string"}, "reason": {"type": "string"}}, "required": ["candidate", "reason"], "additionalProperties": False}},
    {"type": "function", "name": "propose_lab_change", "description": "Propose Lab settings (auto, budget_usd_per_day, max_active_studies, min_hours_between_studies, step_minutes, reevaluate_hours) and/or the seats of one group (scientists, workers, evaluators, supervisor; key 'chat' = the one Supervisor chat seat). Each seat: name, provider (anthropic/openai), model, reasoning, optional fallback {provider, model, reasoning}. The user must approve it.",
     "parameters": {"type": "object", "properties": {"settings": {"type": "object"}, "group": {"type": "string"}, "key": {"type": "string", "enum": ["members", "chat"]},
                                                     "seats": {"type": "array", "items": {"type": "object"}}, "reason": {"type": "string"}},
                    "required": ["reason"], "additionalProperties": False}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}
MAX_DIRECTIVES = 5
STUDY_ID = re.compile(r"s\d{8}-\d{6}(-[0-9a-f]{4})?")
REVIEW_SCHEMA = {"type": "object", "properties": {
    "summary": {"type": "string"}, "health": {"type": "string", "enum": ["ok", "attention", "problem"]}, "alarms": {"type": "array", "items": {"type": "string"}},
    "directives": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "reason": {"type": "string"}}}},
    "proposals": {"type": "array", "items": {"type": "object", "properties": {"what": {"type": "string"}, "why": {"type": "string"}}}}},
    "required": ["summary", "health", "alarms", "directives", "proposals"]}
SUBMIT_REVIEW = {"type": "function", "name": "submit_review", "strict": False, "description": "Submit your daily review.", "parameters": REVIEW_SCHEMA}
SUBMIT_EXCHANGE = {"type": "function", "name": "submit_exchange", "strict": False, "description": "Submit your stance on every colleague directive and your revised review.",
                   "parameters": {"type": "object", "properties": {
                       "reviews": {"type": "array", "items": {"type": "object", "properties": {
                           "member": {"type": "string", "description": "Colleague label, e.g. Colleague B"}, "directive_id": {"type": "string"},
                           "stance": {"type": "string", "enum": ["agree", "partial", "disagree"]}, "reason": {"type": "string"}}}},
                       "revised_review": REVIEW_SCHEMA}, "required": ["reviews", "revised_review"]}}


def similar(a: str, b: str) -> bool:
    """Two directives say the same thing (near-identical wording or mostly the same words)."""
    import difflib
    a, b = a.lower(), b.lower(); wa, wb = set(re.findall(r"\w+", a)), set(re.findall(r"\w+", b))
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.75 or len(wa & wb) / max(1, len(wa | wb)) >= 0.6


def _review_stable(prev, new, reviews):
    return all(r.get("stance") == "agree" for rs in reviews.values() for r in rs) or json.dumps(prev, sort_keys=True) == json.dumps(new, sort_keys=True)


class Supervisor:
    def __init__(self, manage, lab=None):
        self.m = manage; self.store = manage.store; self._lab = lab

    @property
    def lab(self):
        if self._lab is None:
            from think.think import ThinkEngine
            from think.loop import Lab
            self._lab = Lab(ThinkEngine(self.m.root))
        self._lab.cfg = self._lab.load_config(); return self._lab

    # ---------------- what the Supervisor sees ----------------
    def directives(self) -> list[dict]:
        now = time.time(); return [d for d in (self.store.get("think", "directives", []) or []) if isinstance(d, dict) and float(d.get("expires") or 0) > now]

    def stuck(self) -> list[str]:
        """Things that stop the research loop from moving, found by code (raised as alarms in chat and the daily review)."""
        now = time.time(); out = []; last = self.lab.think.state().get("lab_last") or {}
        if last.get("step") == "idle" and now - float(last.get("since") or now) > 86400:
            out.append(f"research loop idle for {(now - float(last['since'])) / 3600:.0f} h: {last.get('why')}")
        for a in self.store.actions("PENDING"):
            if now - float(a.get("created_at") or now) > 86400:
                out.append(f"action #{a['id']} ({a['kind']}) has waited {(now - float(a['created_at'])) / 3600:.0f} h for your approval")
        for p in self.lab.think.pool():
            if p.get("status") == "SHADOW" and p.get("hold"): out.append(f"{p['id']} passed the gate but the evaluators disagree: {p['hold'].get('why')}")
        return out

    def overview(self, full: bool = True) -> dict:
        from think.loop import SETTINGS
        lab = self.lab; t = lab.think; studies = []
        for s in lab.studies(8):
            p = lab.dir / s["id"] / "study.json"; m = json.loads(p.read_text()) if p.is_file() else {}
            studies.append({**{k: s[k] for k in ("id", "stage", "status", "error")}, "created": time.strftime("%Y-%m-%d %H:%M", time.gmtime(s["created"])),
                            "hypotheses": [(h["key"], h.get("title"), h["agreement"], h["selected"]) for h in m.get("hypotheses") or []],
                            "builds": {k: b.get("status") for k, b in (m.get("builds") or {}).items()}, "evaluation": (m.get("evaluation") or {}).get("actions"),
                            "cost_usd": (m.get("cost") or {}).get("usd")})
        pool = [{**{k: p.get(k) for k in ("id", "study_id", "kind", "hypothesis", "status", "hold")}, "gate": (p.get("evaluation") or {}).get("verdict"),
                 "gate_why": (p.get("evaluation") or {}).get("why")} for p in t.pool() if p.get("status") in ("SHADOW", "PASS_PENDING")]
        g = lab.cfg["groups"]; reviews = self.store.get("supervisor", "reviews", []) or []
        out = {"stuck": self.stuck(), "loop": {k: lab.cfg[k] for k in SETTINGS}, "loop_last_step": t.state().get("lab_last"),
               "shadow_slots": {"total": int(t.control()["pool_slots"]), "free": t._free_slots(), "waiting_builds": lab.pending_shadows(), "blocked_by": lab.shadow_room()}, "spent_today_usd": round(lab.spent_today(), 2),
               "studies": studies, "shadow_pool": pool, "systematic_issues": (self.store.get("lab", "issues", {}) or {}).get("issues", []),
               "directives": self.directives(), "seats": {k: {"members": v.get("members"), **({"chat": vars(self.chat_seat())} if k == "supervisor" else {})} for k, v in g.items()},
               "last_review": ({k: reviews[-1].get(k) for k in ("day", "health", "alarms", "summaries", "proposals")} if reviews else None)}
        if full: out.update(scorecards=lab.scorecards(), daily_card=t.daily_check())
        return out

    def read_study(self, sid: str, part: str):
        lab = self.lab
        if part == "scorecards": return lab.scorecards()
        if not STUDY_ID.fullmatch(sid or ""): raise ValueError("unknown study id")
        d = lab.dir / sid
        if not d.is_dir(): raise ValueError("unknown study id")
        if part == "manifest": data = json.loads((d / "study.json").read_text())
        elif part == "brief": data = json.loads((d / "brief.json").read_text()) if (d / "brief.json").is_file() else "no brief yet"
        elif part == "builds": data = {p.parent.name: {k: v for k, v in json.loads(p.read_text()).items() if k not in ("replay", "screen", "sample")} for p in sorted(d.glob("build/*/build_card.json"))}
        elif part == "evaluation":
            passes = sorted(d.glob("evaluation/pass*/evaluation.json"), key=lambda p: int(p.parent.name[4:]))
            data = json.loads(passes[-1].read_text()) if passes else "not evaluated yet"
        else: raise ValueError(f"unknown part {part}")
        text = json.dumps(data, default=str); return json.loads(text) if len(text) <= 40000 else {"truncated": text[:40000]}

    # ---------------- what it may do ----------------
    def set_directive(self, text: str, reason: str, days: int = 7, by: str = "supervisor") -> dict:
        text = " ".join(str(text).split())[:500]
        if not text: raise ValueError("empty directive")
        active = self.directives()
        if len(active) >= MAX_DIRECTIVES: raise ValueError(f"already {MAX_DIRECTIVES} active directives; clear one first")
        dup = next((d for d in active if similar(text, d["text"])), None)
        if dup: raise ValueError(f"same as active directive {dup['id']}")
        d = {"id": f"d{int(time.time() * 1000) % 10**8:08d}{len(active)}", "text": text, "reason": str(reason)[:500], "by": by,
             "created": time.time(), "expires": time.time() + 86400 * max(1, min(30, int(days)))}
        self.store.put("think", "directives", active + [d]); self.store.audit("supervisor", "directive_set", d); return d

    def clear_directive(self, did: str) -> dict:
        active = self.directives(); keep = [d for d in active if d["id"] != did]
        if len(keep) == len(active): raise ValueError("unknown directive")
        self.store.put("think", "directives", keep); self.store.audit("supervisor", "directive_cleared", {"id": did}); return {"ok": True}

    def check_change(self, settings: dict | None, group: str | None, key: str | None, seats: list | None) -> dict:
        lab = self.lab; out = {}
        if settings: out["settings"] = lab.check_settings(settings)
        if seats is not None or group:
            if not group: raise ValueError("group is required with seats")
            out["seats"] = {"group": group, "key": key or "members", "seats": lab.check_seats(group, seats or [], key or "members")}
        if not out: raise ValueError("nothing to change")
        return out

    def propose_change(self, settings=None, group=None, key=None, seats=None, reason: str = "") -> int:
        ch = self.check_change(settings, group, key, seats)
        aid = self.store.propose_action("LAB_CHANGE", {"path": "lab settings", **ch, "reason": f"{reason} | {json.dumps(ch)}"[:3000]})
        self.store.audit("supervisor", "action_proposed", {"id": aid, "kind": "LAB_CHANGE"}); return aid

    def propose_retire(self, candidate: str, reason: str) -> int:
        e = next((p for p in self.lab.think.pool() if p["id"] == candidate and p.get("status") in ("SHADOW", "PASS_PENDING")), None)
        if e is None: raise ValueError(f"no live shadow candidate {candidate}")
        c = (e.get("evaluation") or {}).get("candidate") or {}
        aid = self.store.propose_action("RETIRE_SHADOW", {"path": candidate, "candidate": candidate,
                                                           "reason": f"{reason} | {c.get('n')} trades, {c.get('expectancy_bps')} bps/trade, t {c.get('t_stat')}"[:2000]})
        self.store.audit("supervisor", "action_proposed", {"id": aid, "kind": "RETIRE_SHADOW", "candidate": candidate}); return aid

    def retire(self, action_id: int, payload: dict) -> dict:
        """Queued for the Think worker, the only process that changes the shadow pool (applied within seconds)."""
        self.store.put("think_retire", str(action_id), {"candidate": payload["candidate"], "reason": payload.get("reason", ""), "at": time.time()})
        return {"ok": True, "queued": payload["candidate"], "note": "the Think worker retires it within seconds"}

    def apply_change(self, payload: dict) -> dict:
        lab = self.lab; out = {"ok": True}
        if payload.get("settings"): out["settings"] = lab.update_settings(payload["settings"])
        if payload.get("seats"): s = payload["seats"]; out["seats"] = lab.update_seats(s["group"], s["seats"], s.get("key", "members"))
        return out

    def tool(self, name: str, args: dict):
        if name == "lab_overview": return self.overview(full=False)
        if name == "read_study": return self.read_study(args.get("study_id", ""), args["part"])
        if name == "set_directive": return self.set_directive(args["text"], args["reason"], args.get("days", 7))
        if name == "clear_directive": return self.clear_directive(args["id"])
        if name == "propose_retire_shadow": return {"action_id": self.propose_retire(args["candidate"], args["reason"]), "status": "PENDING user approval"}
        if name == "propose_lab_change":
            return {"action_id": self.propose_change(args.get("settings"), args.get("group"), args.get("key"), args.get("seats"), args["reason"]), "status": "PENDING user approval"}
        raise ValueError(f"unknown tool: {name}")

    # ---------------- chat (one seat with fallback) ----------------
    def chat_seat(self) -> Member:
        c = self.lab.cfg["groups"]["supervisor"].get("chat")
        if c: return Member("supervisor", c["provider"], c["model"], c.get("reasoning", "high"), fallback=c.get("fallback"))
        s = self.store.get("settings", "manage_agent", default_agent_settings(self.m.cfg, "manage")) or {}
        p = resolve_provider(s.get("provider", "mock")); fb = str(s.get("fallback_provider") or "none"); r = s.get("reasoning", "high")
        return Member("supervisor", p, _model_for(p, s.get("model")), r,
                      fallback={"provider": fb, "model": _model_for(fb, s.get("fallback_model")), "reasoning": r} if fb not in ("none", "", p) else None,
                      options={k: s[k] for k in ("response_detail", "tool_autonomy", "budget") if k in s})

    def _record(self, sid: str, group: str, member: Member, used: dict | None, usage: dict, error: str | None, prompt_id: str) -> None:
        u = used or {"provider": member.provider, "model": member.model}
        self.lab.record(sid)({"group": group, "round": 0, "member": member.name, "provider": u["provider"], "model": u["model"], "prompt": prompt_id,
                              "status": "ok" if used else "unavailable", "error": error, "usage": usage, "cost_usd": cost_usd(u["model"], usage), "finished": time.time()})

    def answer(self, prompt: str, tools: list[dict], handler) -> str:
        system, pid = prompt_identity("supervisor_chat"); m = self.chat_seat()
        s = self.store.get("settings", "manage_agent", {}) or {}
        text, usage, err, used = run_seat(m, system, prompt, tools, handler, web=str(s.get("web_research", "off")) == "automatic")
        self._record("chat", "supervisor", m, used, usage, err, pid)
        if used is None: raise RuntimeError(f"Supervisor unavailable: {err}")
        if (used["provider"], used["model"]) != (m.provider, m.model): text = f"{text}\n\n_(answered by the fallback {used['model']}; {m.model} unavailable: {err})_"
        return text

    # ---------------- daily review (the supervisor pair) ----------------
    def facts(self) -> dict:
        return {"generated_at": time.time(), "status": self.m.status(), "lab": self.overview(full=True)}

    def daily_review(self, call=None, force: bool = False) -> dict | None:
        lab = self.lab; g = lab.cfg["groups"]["supervisor"]; now = time.gmtime(); day = time.strftime("%Y-%m-%d", now)
        if not force and (self.store.get("supervisor", "last_review_day") == day or now.tm_hour < int(g.get("review_hour_utc", 1))): return None
        if not lab.budget_ok(): return None
        facts = json.dumps(self.facts(), default=str)[:60000]
        task = f"TASK: write today's daily review ({day}) via submit_review.\n\nFACTS:\n{facts}"
        def exchange_prompt(m, mine, others):
            return (f"{task}\n\nYOUR CURRENT REVIEW:\n{json.dumps(mine, default=str)}\n\nYOUR COLLEAGUES' CURRENT REVIEWS:\n"
                    + "\n\n".join(f"{label}:\n{json.dumps(rep, default=str)}" for label, rep in others.items())
                    + "\n\nGive your stance on each of their directives and submit your revised review via submit_exchange.")
        def check_ex(m, others):
            ids = {label: [d.get("id") for d in (rep.get("directives") or []) if isinstance(d, dict)] for label, rep in others.items()}
            return lambda doc: check_review_exchange(doc, ids)
        kw = {"call": call} if call else {}
        unit = Unit("supervisor", lab.members("supervisor"), "supervisor", lab.think.think_dir / "reviews" / day, lab.record(f"review-{day}"), lab.budget_ok,
                    tools=[], handler=None, rounds=int(g.get("rounds", 1)), **kw)
        res = unit.run(first_prompt=lambda m: task, exchange_prompt=exchange_prompt, first_submit=SUBMIT_REVIEW, exchange_submit=SUBMIT_EXCHANGE,
                       check_first=check_review, check_exchange=check_ex, stable=_review_stable, revised_key="revised_review")
        review = self._merge(res, day); review["prompt"] = unit.prompt_id
        review["alarms"] = sorted(set(review["alarms"]) | set(self.stuck()))   # code-found blockers are always raised
        for d in review["directives_applied"]:
            try: d["active_id"] = self.set_directive(d["text"], d["reason"], 7, by=f"daily review {day}")["id"]
            except ValueError as exc: d["not_applied"] = str(exc)
        reviews = (self.store.get("supervisor", "reviews", []) or []) + [review]
        self.store.put("supervisor", "reviews", reviews[-30:]); self.store.put("supervisor", "last_review_day", day)
        lab.think.note("report", text="\n\n".join(f"**{m}**\n{s}" for m, s in review["summaries"].items()), why=f"daily review {day}: {review['health']}")
        return review

    @staticmethod
    def _merge(res: dict, day: str) -> dict:
        label_to_name = {v: k for k, v in res["labels"].items()}; reps = res["reports"]
        applied, disputed = [], []
        for author, rep in reps.items():
            for d in rep.get("directives", []):
                stances = [r.get("stance") for rv, rs in res["reviews"].items() if rv != author for r in rs
                           if label_to_name.get(r.get("member")) == author and r.get("directive_id") == d.get("id")]
                others = len(reps) - 1
                (applied if len(stances) == others and all(s == "agree" for s in stances) else disputed).append({**d, "by": author, "stances": stances})
        worst = max((rep.get("health", "ok") for rep in reps.values()), key=["ok", "attention", "problem"].index)
        return {"day": day, "at": time.time(), "health": worst, "summaries": {m: rep.get("summary") for m, rep in reps.items()},
                "alarms": sorted({a for rep in reps.values() for a in rep.get("alarms", [])}), "directives_applied": applied, "directives_disputed": disputed,
                "proposals": [{**p, "by": m} for m, rep in reps.items() for p in rep.get("proposals", [])],
                "members": res["available"], "unavailable": res["unavailable"], "independence": res["independence"]}
