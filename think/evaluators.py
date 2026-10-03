"""Evaluators group: every member judges the same study results against the pre-registration, independently,
then they exchange and revise. The code gate stays final on the numbers: evaluators can stop or hold a
candidate, never promote one the gate has not passed, and promotion needs every evaluator to agree (and then
still goes through Manage approval). They also report systematic issues of the process across studies,
next to the issues detected by code."""
from __future__ import annotations
import json
from pathlib import Path

from agents.documents import OUTCOMES, OWNERS, RECOMMENDATIONS, SEVERITIES, check_evaluation, check_evaluation_exchange
from agents.unit import Member, Unit
from think.scientists import _stable

VERDICT = {"type": "object", "properties": {
    "subject": {"type": "string"}, "outcome": {"type": "string", "enum": list(OUTCOMES)},
    "recommendation": {"type": "string", "enum": list(RECOMMENDATIONS)}, "reason": {"type": "string"},
    "prediction_checks": {"type": "array", "items": {"type": "object", "properties": {
        "metric": {"type": "string"}, "expected": {"type": "string"}, "observed": {"type": "string"}, "met": {"type": ["boolean", "null"]}}}}}}
EVALUATION = {"type": "object", "properties": {
    "verdicts": {"type": "array", "items": VERDICT},
    "systematic_issues": {"type": "array", "items": {"type": "object", "properties": {
        "issue": {"type": "string"}, "evidence": {"type": "string"}, "owner": {"type": "string", "enum": list(OWNERS)},
        "severity": {"type": "string", "enum": list(SEVERITIES)}, "suggestion": {"type": "string"}}}},
    "lessons": {"type": "array", "items": {"type": "string"}}}, "required": ["verdicts", "systematic_issues", "lessons"]}
SUBMIT_EVALUATION = {"type": "function", "name": "submit_evaluation", "strict": False,
                     "description": "Submit your evaluation: one verdict per subject, systematic issues, lessons.", "parameters": EVALUATION}
SUBMIT_EXCHANGE = {"type": "function", "name": "submit_exchange", "strict": False,
                   "description": "Submit your stance on every colleague verdict and your revised evaluation.",
                   "parameters": {"type": "object", "properties": {
                       "reviews": {"type": "array", "items": {"type": "object", "properties": {
                           "member": {"type": "string", "description": "Colleague label, e.g. Colleague B"}, "subject": {"type": "string"},
                           "stance": {"type": "string", "enum": ["agree", "partial", "disagree"]}, "reason": {"type": "string"}}}},
                       "revised_evaluation": EVALUATION}, "required": ["reviews", "revised_evaluation"]}}


def ready(subject: dict, max_days: float) -> bool:
    """A shadow is judged once the gate decided or it reached its pre-registered trade count, and after max_days
    in any case (a review, not a removal: the evaluators decide); a probe once it has its pre-registered number of
    signals, or its own time cap ran out."""
    if subject.get("route") == "probe":
        return (subject.get("probe") or {}).get("complete") is not False or subject.get("age_days", 0) >= min(7.0, max_days)
    if subject.get("pending"): return False                                              # built, waiting for a shadow slot
    if not subject.get("shadow"): return True
    g = subject.get("gate") or {}; n = (g.get("candidate") or {}).get("n", 0)
    return g.get("verdict") in ("PASS", "FAIL") or n >= int((subject.get("preregistration") or {}).get("min_trades") or 30) or g.get("age_h", 0) >= 24 * max_days


def decide(subject: dict, recs: list[str], max_days: float) -> tuple[str, str]:
    """The team action for one subject, from the code gate and the evaluators' recommendations."""
    if subject.get("pending"): return "extend", "built, still waiting for a shadow slot"
    if not subject.get("shadow"): return "close", "nothing running: result recorded"
    g = subject.get("gate") or {}; gate = g.get("verdict")
    if gate == "FAIL": return "retire", f"failed the code gate: {g.get('why')}"
    if gate == "PASS":
        if recs and all(r == "promote" for r in recs): return "promote", "code gate passed and every evaluator recommends promotion"
        return "hold", "code gate passed but evaluators do not all recommend promotion: Supervisor/user decides"
    if any(r in ("retire", "rethink") for r in recs): return "retire", "evaluators recommend stopping before the gate decided"
    return "extend", "evaluators keep it running (no time limit): judged again later"


def systematic_checks(history: list[dict], outcomes: list[dict]) -> list[dict]:
    """Process problems visible across recent studies (history: study manifests, newest first)."""
    issues = []
    def add(issue, owner, severity, evidence, suggestion):
        issues.append({"issue": issue, "owner": owner, "severity": severity, "evidence": evidence, "suggestion": suggestion, "by": "code"})
    judged = [h for h in history if h.get("evaluation")][:5]
    if len(judged) >= 3 and not any("supported" in (h["evaluation"].get("outcomes") or {}).values() for h in judged[:3]):
        add("no supported hypothesis in the last 3 evaluated studies", "scientists", "high", ", ".join(h["id"] for h in judged[:3]),
            "question the strategy family itself (edge before costs) rather than tuning it")
    confident = [o for o in outcomes if (o.get("confidence") or 0) >= 0.6 and o.get("outcome") in ("supported", "falsified")]
    wrong = [o for o in confident if o["outcome"] == "falsified"]
    if len(wrong) >= 3 and len(wrong) / len(confident) >= 0.6:
        add("confident hypotheses are mostly falsified", "scientists", "medium", f"{len(wrong)} of {len(confident)} with confidence >= 0.6 falsified",
            "calibrate confidence against the scorecard; prefer cheaper probes first")
    recent = history[:5]
    def builds(status): return [f"{h['id']}:{k}" for h in recent for k, b in (h.get("builds") or {}).items() if (b or {}).get("status") == status]
    if len(builds("ambiguous")) >= 2: add("hypotheses are too vague to build the same way twice", "scientists", "medium", ", ".join(builds("ambiguous")), "state the exact rule change in change.description")
    if len(builds("failed")) >= 2: add("builds keep failing validation or review", "workers", "medium", ", ".join(builds("failed")), "check the worker prompt and contract errors")
    reduced = [h["id"] for h in recent if any((h.get("reduced_independence") or {}).values())]
    if len(reduced) >= 2 and len(reduced) * 2 >= len(recent):
        add("groups often run with fewer independent models than configured", "lab", "medium", ", ".join(reduced), "restore the provider (credits/keys) or configure a fallback seat")
    bad_data = [h["id"] for h in history[:3] if ((h.get("data_window") or {}).get("tt_input_quality") or {}).get("ok") is False]
    if bad_data: add("market data quality was not OK during studies", "data", "high", ", ".join(bad_data), "check tt_input alarms before trusting results")
    holds = [f"{h['id']}:{k}" for h in recent for k, a in ((h.get("evaluation") or {}).get("actions") or {}).items() if a == "hold"]
    if len(holds) >= 2: add("evaluators often disagree on candidates that passed the gate", "evaluators", "medium", ", ".join(holds), "review the gate criteria or evaluator prompt")
    none = [h["id"] for h in recent if "hypotheses" in h and not any(x.get("selected") for x in h["hypotheses"])]
    if len(none) >= 2: add("studies end with nothing testable", "scientists", "low", ", ".join(none), "ask for at least one cheap probe per study")
    return issues


def merge(result: dict, subjects: dict[str, dict], code_issues: list[dict], max_days: float) -> dict:
    """Team evaluation: per subject every member's verdict, stances, agreement and the team action."""
    label_to_name = {v: k for k, v in result["labels"].items()}; out = {}
    for sid_, subj in subjects.items():
        verdicts = {m: next((v for v in rep.get("verdicts", []) if v.get("subject") == sid_), {}) for m, rep in result["reports"].items()}
        recs = [v.get("recommendation") for v in verdicts.values()]; outs = {v.get("outcome") for v in verdicts.values()}
        stances = {}
        for reviewer, revs in result["reviews"].items():
            for r in revs:
                if r.get("subject") == sid_: stances.setdefault(label_to_name.get(r.get("member"), r.get("member")), {})[reviewer] = {"stance": r.get("stance"), "reason": r.get("reason", "")}
        action, why = decide(subj, recs, max_days)
        out[sid_] = {"key": subj["key"], "title": subj.get("title"), "shadow": subj.get("shadow"), "gate": (subj.get("gate") or {}).get("verdict"),
                     "outcome": next(iter(outs)) if len(outs) == 1 else "disputed",
                     "agreement": "single" if len(verdicts) < 2 else "agreed" if len(set(recs)) == 1 and len(outs) == 1 else "split",
                     "action": action, "why": why, "back_to_scientists": "rethink" in recs or (subj.get("build") or {}).get("status") == "ambiguous",
                     "verdicts": verdicts, "stances": stances}
    issues = code_issues + [{**x, "by": m} for m, rep in result["reports"].items() for x in rep.get("systematic_issues", [])]
    lessons = [{"by": m, "lesson": x} for m, rep in result["reports"].items() for x in rep.get("lessons", [])]
    return {"group": "evaluators", "members": result["available"], "unavailable": result["unavailable"], "independence": result["independence"],
            "reduced_independence": result["independence"]["reduced"], "rounds": result["rounds"], "subjects": out, "systematic_issues": issues, "lessons": lessons}


def run_evaluators(members: list[Member], subjects: dict[str, dict], history: list[dict], code_issues: list[dict], out_dir: Path, record, budget_ok,
                   tools: list[dict], handler, rounds: int = 2, max_days: float = 7.0, call=None) -> dict:
    ids = list(subjects)
    task = ("TASK: evaluate every subject against its pre-registration and report systematic issues, via submit_evaluation.\n\n"
            f"SUBJECTS:\n{json.dumps(subjects, default=str)[:50000]}\n\nRECENT STUDY HISTORY (newest first):\n{json.dumps(history, default=str)[:15000]}"
            f"\n\nISSUES DETECTED BY CODE:\n{json.dumps(code_issues, default=str)}")
    def exchange_prompt(m, mine, others):
        return (f"{task}\n\nYOUR CURRENT EVALUATION:\n{json.dumps(mine, default=str)}\n\nYOUR COLLEAGUES' CURRENT EVALUATIONS:\n"
                + "\n\n".join(f"{label}:\n{json.dumps(rep, default=str)}" for label, rep in others.items())
                + "\n\nGive your stance on each of their verdicts and submit your revised evaluation via submit_exchange.")
    kw = {"call": call} if call else {}
    unit = Unit("evaluators", members, "evaluator", out_dir, record, budget_ok, tools=tools, handler=handler, rounds=rounds, **kw)
    result = unit.run(first_prompt=lambda m: task, exchange_prompt=exchange_prompt, first_submit=SUBMIT_EVALUATION, exchange_submit=SUBMIT_EXCHANGE,
                      check_first=lambda doc: check_evaluation(doc, ids),
                      check_exchange=lambda m, others: (lambda doc: check_evaluation_exchange(doc, list(others), ids)),
                      stable=_stable, revised_key="revised_evaluation")
    ev = merge(result, subjects, code_issues, max_days); ev["prompt"] = unit.prompt_id
    for m, rep in result["reports"].items():  # each member's final evaluation, kept separate for scorecards
        p = Path(out_dir) / "evaluators" / f"{m}.json"; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(rep, indent=1, default=str))
    return ev
