"""Scientists group: every member reviews the whole system on the same evidence, independently, then they
exchange and revise; the merged Research Brief says which hypotheses are agreed, partly agreed or disputed."""
from __future__ import annotations
import json
from pathlib import Path

from agents.documents import CHECKLIST, check_exchange, check_scientist_report
from agents.unit import Member, Unit

HYPOTHESIS_SCHEMA = {"type": "object", "properties": {
    "id": {"type": "string"}, "title": {"type": "string"}, "kind": {"type": "string", "enum": ["params", "code", "note"]},
    "change": {"type": "object", "description": "params: {\"params\": {name: [values]}}; code: {\"description\": \"...\"}"},
    "rationale": {"type": "string"}, "evidence": {"type": "string"},
    "predictions": {"type": "array", "items": {"type": "object", "properties": {"metric": {"type": "string"}, "direction": {"type": "string"}, "target": {"type": "string"}}}},
    "min_trades": {"type": "integer"}, "kill_rule": {"type": "string"}, "falsified_if": {"type": "string"}, "confidence": {"type": "number"}}}
REPORT_SCHEMA = {"type": "object", "properties": {
    "checklist": {"type": "object", "properties": {k: {"type": "string"} for k in CHECKLIST}},
    "diagnosis": {"type": "string"}, "hypotheses": {"type": "array", "items": HYPOTHESIS_SCHEMA}}, "required": ["checklist", "diagnosis", "hypotheses"]}
SUBMIT_REPORT = {"type": "function", "name": "submit_report", "strict": False,
                 "description": "Submit your complete report (checklist, diagnosis, up to 3 pre-registered hypotheses).", "parameters": REPORT_SCHEMA}
SUBMIT_EXCHANGE = {"type": "function", "name": "submit_exchange", "strict": False,
                   "description": "Submit your stance on every colleague hypothesis and your revised report.",
                   "parameters": {"type": "object", "properties": {
                       "reviews": {"type": "array", "items": {"type": "object", "properties": {
                           "member": {"type": "string", "description": "Colleague label, e.g. Colleague B"}, "hypothesis_id": {"type": "string"},
                           "stance": {"type": "string", "enum": ["agree", "partial", "disagree"]}, "reason": {"type": "string"}}}},
                       "revised_report": REPORT_SCHEMA}, "required": ["reviews", "revised_report"]}}


def _stable(prev: dict, new: dict, reviews: dict) -> bool:
    """Stop when everyone agrees with everything, or nobody changed their report."""
    all_agree = all(r.get("stance") == "agree" for rs in reviews.values() for r in rs)
    unchanged = all(json.dumps(prev.get(k), sort_keys=True) == json.dumps(new.get(k), sort_keys=True) for k in new)
    return all_agree or unchanged


def merge(result: dict, slots: int) -> dict:
    """Team Research Brief: every final hypothesis with its author, each colleague's stance, and the agreement level."""
    label_to_name = {v: k for k, v in result["labels"].items()}; hyps = []
    for author, rep in result["reports"].items():
        for h in rep.get("hypotheses", []):
            stances = {}
            for reviewer, revs in result["reviews"].items():
                if reviewer == author: continue
                for r in revs:
                    if label_to_name.get(r.get("member")) == author and r.get("hypothesis_id") == h.get("id"):
                        stances[reviewer] = {"stance": r["stance"], "reason": r.get("reason", "")}
            vals = [s["stance"] for s in stances.values()]
            agreement = ("unreviewed" if not vals else "agreed" if all(v == "agree" for v in vals)
                         else "disagreed" if "disagree" in vals else "partial")
            hyps.append({**h, "key": f"{author}:{h.get('id')}", "author": author, "agreement": agreement, "stances": stances})
    rank = {"agreed": 0, "partial": 1, "unreviewed": 2, "disagreed": 3}
    testable = sorted((h for h in hyps if h.get("kind") != "note"), key=lambda h: (rank[h["agreement"]], -float(h.get("confidence") or 0)))
    selected = [h["key"] for h in testable if h["agreement"] != "disagreed" or float(h.get("confidence") or 0) >= 0.6][:slots]
    return {"group": "scientists", "members": result["available"], "unavailable": result["unavailable"],
            "reduced_independence": result["independence"]["reduced"], "independence": result["independence"], "rounds": result["rounds"],
            "diagnoses": {a: r.get("diagnosis") for a, r in result["reports"].items()},
            "checklists": {a: r.get("checklist") for a, r in result["reports"].items()},
            "hypotheses": hyps, "selected": selected,
            "dissent": [{"key": h["key"], **{"by": n, **s}} for h in hyps for n, s in h["stances"].items() if s["stance"] != "agree"]}


def run_scientists(members: list[Member], evidence: dict, study_dir: Path, record, budget_ok, tools: list[dict], handler,
                   rounds: int = 3, max_hypotheses: int = 3, slots: int = 3, call=None) -> dict:
    task = ("TASK: review the whole strategy on this evidence and deliver your report via submit_report.\n\n"
            f"EVIDENCE PACK:\n{json.dumps(evidence, default=str)[:60000]}")
    def exchange_prompt(m, mine, others):
        return (f"{task}\n\nYOUR CURRENT REPORT:\n{json.dumps(mine, default=str)}\n\nYOUR COLLEAGUES' CURRENT REPORTS:\n"
                + "\n\n".join(f"{label}:\n{json.dumps(rep, default=str)}" for label, rep in others.items())
                + "\n\nGive your stance on each of their hypotheses and submit your revised report via submit_exchange.")
    def check_exchange_for(m, others):
        ids = {label: [h.get("id") for h in (rep.get("hypotheses") or []) if isinstance(h, dict)] for label, rep in others.items()}
        return lambda doc: check_exchange(doc, ids, max_hypotheses)
    kw = {"call": call} if call else {}
    unit = Unit("scientists", members, "scientist", study_dir, record, budget_ok, tools=tools, handler=handler, rounds=rounds, **kw)
    result = unit.run(first_prompt=lambda m: task, exchange_prompt=exchange_prompt, first_submit=SUBMIT_REPORT, exchange_submit=SUBMIT_EXCHANGE,
                      check_first=lambda doc: check_scientist_report(doc, max_hypotheses), check_exchange=check_exchange_for, stable=_stable)
    brief = merge(result, slots); brief["prompt"] = unit.prompt_id
    (Path(study_dir) / "brief.json").write_text(json.dumps(brief, indent=1, default=str))
    for author, rep in result["reports"].items():  # each member's final report, kept separate for scorecards
        p = Path(study_dir) / "scientists" / f"{author}.json"; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(rep, indent=1, default=str))
    return brief
