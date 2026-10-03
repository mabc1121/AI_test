"""The core unit used by every group: 2+ agents (e.g. Claude + GPT) do the same task independently, then exchange
and revise their work for a few rounds, then the group module merges it into one team output.

* Every member output is stored separately (<study>/<group>/r<round>/<member>.json) with its identity
  (provider, model, prompt version) and token cost, and recorded in SQLite.
* Resumable: an output already on disk is reused, so a restart continues where it stopped.
* Seats, not hard-coded models: each seat has a provider/model and an optional fallback used only when the
  provider is unavailable (credits, auth, network) - never because of a poor answer. Every output records the
  model actually used ("substitute_for" when the fallback ran); a seat with no working model is unavailable.
* Members see each other under neutral labels (Colleague A/B) so ideas are judged, not brands.
"""
from __future__ import annotations
import concurrent.futures, hashlib, json, time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from core.common import AgentProvider
from agents.documents import cost_usd

PROMPTS = Path(__file__).with_name("prompts")


@dataclass
class Member:
    """A seat in a group: its model, and an optional fallback model for when the provider is unavailable."""
    name: str
    provider: str
    model: str
    reasoning: str = "high"
    fallback: dict | None = None      # {"provider", "model", "reasoning"}
    options: dict | None = None       # response_detail / tool_autonomy / budget overrides for this seat

    def settings(self, alt: dict | None = None, web: bool = False) -> dict:
        m = alt or {"provider": self.provider, "model": self.model, "reasoning": self.reasoning}
        opts = {k: v for k, v in (self.options or {}).items() if k in ("response_detail", "tool_autonomy", "budget")}
        # the adapter's own fallback is off: substitution is decided and recorded here, per seat
        return {"tool_autonomy": "high", "budget": "standard", "response_detail": "normal", **opts,
                "provider": m["provider"], "model": m["model"], "reasoning": m.get("reasoning", self.reasoning),
                "web_research": "automatic" if web else "off", "fallback_provider": "none"}


def prompt_identity(name: str) -> tuple[str, str]:
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    return text, f"{name}.md@{hashlib.sha256(text.encode()).hexdigest()[:10]}"


class BudgetExceeded(RuntimeError):
    pass


class UnitFailed(RuntimeError):
    pass


def run_seat(member: Member, system: str, prompt: str, tools: list[dict], handler: Callable | None, web: bool = False):
    """One turn on a seat: the primary model, then the seat's fallback if the provider is unavailable.
    Returns (answer_text, usage, error, model_used); model_used is None when no model of the seat worked, and
    error is also set when the fallback answered (why the primary was skipped)."""
    usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}; error = None
    for alt in [None] + ([member.fallback] if member.fallback else []):
        s = member.settings(alt, web); p = AgentProvider(s)
        used = {"provider": s["provider"], "model": s["model"], "reasoning": s["reasoning"]}
        try:
            text = p.run(prompt, system=system, allow_web=web, tools=tools, handler=handler)
            for k in usage: usage[k] += p.usage.get(k, 0)
            return text, usage, error, used
        except Exception as exc:  # provider unavailable: try the seat's fallback, if any
            for k in usage: usage[k] += p.usage.get(k, 0)
            error = f"{used['model']}: {type(exc).__name__}: {str(exc)[:300]}"
    return None, usage, error, None


def default_call(member: Member, system: str, prompt: str, tools: list[dict], handler: Callable, submit_name: str):
    """A group member's turn: the member must deliver its document via the submit tool.
    Returns (document, usage, error, model_used)."""
    captured: dict = {}
    def h(name, args):
        if name == submit_name: captured["doc"] = args; return {"ok": True, "note": "received; finish now"}
        return handler(name, args)
    _, usage, error, used = run_seat(member, system, prompt, tools, h)
    if used is None: return None, usage, error, None
    return captured.get("doc"), usage, (None if "doc" in captured else f"did not call {submit_name}"), used


class Unit:
    def __init__(self, group: str, members: list[Member], prompt: str, out_dir: Path, record: Callable[[dict], None],
                 budget_ok: Callable[[], bool], tools: list[dict] | None = None, handler: Callable | None = None,
                 rounds: int = 3, call: Callable = default_call):
        self.group, self.members, self.out_dir, self.record, self.budget_ok = group, members, Path(out_dir), record, budget_ok
        self.system, self.prompt_id = prompt_identity(prompt)
        self.tools, self.handler, self.rounds, self.call = tools or [], handler or (lambda n, a: {"error": f"unknown tool {n}"}), rounds, call
        self.labels = {m.name: f"Colleague {chr(65 + i)}" for i, m in enumerate(members)}

    def identity(self, m: Member) -> dict:
        return {"member": m.name, "provider": m.provider, "model": m.model, "reasoning": m.reasoning, "prompt": self.prompt_id}

    def _member_turn(self, m: Member, rnd: int, prompt: str, submit_tool: dict, check: Callable[[Any], list[str]]) -> dict:
        path = self.out_dir / self.group / f"r{rnd}" / f"{m.name}.json"
        if path.is_file(): return json.loads(path.read_text())            # resume
        if not self.budget_ok(): raise BudgetExceeded("daily AI budget reached")
        started = time.time(); usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}; problems: list[str] = []; doc = err = used = None
        for attempt in range(2):
            p = prompt if attempt == 0 else prompt + "\n\nYOUR PREVIOUS SUBMISSION WAS REJECTED. Fix these problems and submit again:\n- " + "\n- ".join(problems)
            r = self.call(m, self.system, p, self.tools + [submit_tool], self.handler, submit_tool["name"])
            doc, u, err = r[:3]; used = (r[3] if len(r) > 3 else None) or used
            for k in ("input_tokens", "output_tokens", "calls"): usage[k] += int((u or {}).get(k, 0))
            if err: break
            problems = check(doc)
            if not problems: break
        status = "ok" if not err and not problems else ("unavailable" if err and not used else "invalid" if not err else "failed")
        used = used or {"provider": m.provider, "model": m.model, "reasoning": m.reasoning}
        substitute = None if (used["provider"], used["model"]) == (m.provider, m.model) else f"{m.provider}/{m.model}"
        out = {**self.identity(m), "provider": used["provider"], "model": used["model"], "reasoning": used.get("reasoning", m.reasoning),
               "substitute_for": substitute, "group": self.group, "round": rnd, "status": status, "error": err, "problems": problems,
               "output": doc if status == "ok" else None, "usage": usage, "cost_usd": cost_usd(used["model"], usage),
               "started": started, "finished": time.time()}
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(out, indent=1, default=str))
        self.record(out)
        return out

    def run(self, first_prompt: Callable[[Member], str], exchange_prompt: Callable[[Member, dict, dict], str],
            first_submit: dict, exchange_submit: dict, check_first: Callable, check_exchange: Callable[[Member, dict], Callable],
            stable: Callable[[dict, dict, dict], bool], revised_key: str = "revised_report") -> dict:
        """Returns {"reports": member->final document, "reviews": member->last-round reviews, "rounds", "available", "unavailable"}."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(self.members))) as ex:
            r0 = dict(zip([m.name for m in self.members], ex.map(lambda m: self._member_turn(m, 0, first_prompt(m), first_submit, check_first), self.members)))
        available = [m for m in self.members if r0[m.name]["status"] == "ok"]
        unavailable = {n: (o["error"] or "; ".join(o["problems"])) for n, o in r0.items() if o["status"] != "ok"}
        if not available: raise UnitFailed(f"{self.group}: no member produced a valid result ({unavailable})")
        seat = lambda o: {k: o.get(k) for k in ("member", "provider", "model", "reasoning", "prompt", "substitute_for")}
        used = {m.name: seat(r0[m.name]) for m in available}   # the model each seat actually ran on (latest round)
        reports = {m.name: r0[m.name]["output"] for m in available}; reviews: dict = {}; rounds = 0
        for rnd in range(1, self.rounds + 1):
            if len(available) < 2: break
            others_of = {m.name: {self.labels[o.name]: reports[o.name] for o in available if o.name != m.name} for m in available}
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(available)) as ex:
                res = dict(zip([m.name for m in available], ex.map(
                    lambda m: self._member_turn(m, rnd, exchange_prompt(m, reports[m.name], others_of[m.name]), exchange_submit,
                                                check_exchange(m, others_of[m.name])), available)))
            new_reports = dict(reports); new_reviews = {}
            for m in list(available):
                o = res[m.name]
                if o["status"] != "ok": unavailable[m.name] = o["error"] or "; ".join(o["problems"]); available.remove(m); new_reports.pop(m.name, None); continue
                new_reports[m.name] = o["output"][revised_key]; new_reviews[m.name] = o["output"]["reviews"]; used[m.name] = seat(o)
            rounds = rnd; done = bool(new_reviews) and stable(reports, new_reports, new_reviews)
            reports, reviews = new_reports, new_reviews
            if done: break
        seats = [used[m.name] for m in available]
        models = {(s["provider"], s["model"]) for s in seats}
        independence = {"seats_configured": len(self.members), "seats_working": len(seats), "distinct_models": len(models),
                        "distinct_providers": len({p for p, _ in models}), "substitutes": {s["member"]: s["substitute_for"] for s in seats if s["substitute_for"]},
                        "reduced": len(seats) < max(2, len(self.members)) or len(models) < len(seats), "single_provider": len({p for p, _ in models}) < 2}
        return {"reports": reports, "reviews": reviews, "rounds": rounds, "labels": self.labels,
                "available": seats, "unavailable": unavailable, "independence": independence}
