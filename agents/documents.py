"""The fixed documents passed between groups, checked by code before hand-over.

A member's work that does not pass `check_*` is sent back once with the list of problems; a member that still
fails is recorded as failed for that round (never silently patched).
"""
from __future__ import annotations
from typing import Any

CHECKLIST = ("lessons", "signal", "costs", "entries", "exits", "risk", "regimes", "alternative_explanation", "cheapest_test")
STANCES = ("agree", "partial", "disagree")
HYPOTHESIS_KINDS = ("params", "code", "probe", "note")
BUILD_VERDICTS = ("correct", "incorrect", "equivalent")
OUTCOMES = ("supported", "falsified", "inconclusive")
RECOMMENDATIONS = ("promote", "retire", "extend", "rethink")
OWNERS = ("scientists", "workers", "evaluators", "trade", "data", "lab")
SEVERITIES = ("low", "medium", "high")

# USD per million tokens (input, output); models not listed record tokens only.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0), "claude-fable-5-1": (10.0, 50.0),
          "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0)}


def cost_usd(model: str, usage: dict) -> float | None:
    p = PRICES.get(model)
    return None if p is None else round(usage.get("input_tokens", 0) / 1e6 * p[0] + usage.get("output_tokens", 0) / 1e6 * p[1], 4)


def _need(d: Any, key: str, typ, problems: list[str], where: str) -> Any:
    v = d.get(key) if isinstance(d, dict) else None
    if not isinstance(v, typ) or (isinstance(v, str) and not v.strip()): problems.append(f"{where}.{key} missing or not {getattr(typ, '__name__', typ)}")
    return v


def check_hypothesis(h: Any, where: str) -> list[str]:
    p: list[str] = []
    if not isinstance(h, dict): return [f"{where} is not an object"]
    for k in ("id", "title", "rationale", "evidence", "falsified_if", "kill_rule"): _need(h, k, str, p, where)
    if h.get("kind") not in HYPOTHESIS_KINDS: p.append(f"{where}.kind must be one of {HYPOTHESIS_KINDS}")
    ch = h.get("change")
    if h.get("kind") == "params" and not (isinstance(ch, dict) and isinstance(ch.get("params"), dict) and ch["params"]):
        p.append(f"{where}.change.params must map parameter -> list of values")
    if h.get("kind") in ("code", "probe") and not (isinstance(ch, dict) and isinstance(ch.get("description"), str)):
        p.append(f"{where}.change.description required for a {h.get('kind')} hypothesis")
    preds = h.get("predictions")
    if not isinstance(preds, list) or not preds: p.append(f"{where}.predictions must be a non-empty list")
    else:
        for i, pr in enumerate(preds):
            for k in ("metric", "direction", "target"): _need(pr, k, str, p, f"{where}.predictions[{i}]")
    mt = h.get("min_trades")
    if not isinstance(mt, int) or mt < 5: p.append(f"{where}.min_trades must be an integer >= 5")
    c = h.get("confidence")
    if not isinstance(c, (int, float)) or not 0 <= c <= 1: p.append(f"{where}.confidence must be 0..1")
    return p


def check_scientist_report(r: Any, max_hypotheses: int = 3) -> list[str]:
    p: list[str] = []
    if not isinstance(r, dict): return ["report is not an object"]
    cl = r.get("checklist")
    if not isinstance(cl, dict): p.append("checklist missing")
    else:
        for k in CHECKLIST: _need(cl, k, str, p, "checklist")
    _need(r, "diagnosis", str, p, "report")
    hs = r.get("hypotheses")
    if not isinstance(hs, list): p.append("hypotheses must be a list")
    else:
        if len(hs) > max_hypotheses: p.append(f"at most {max_hypotheses} hypotheses")
        ids = [h.get("id") for h in hs if isinstance(h, dict)]
        if len(ids) != len(set(ids)): p.append("hypothesis ids must be unique")
        for i, h in enumerate(hs): p += check_hypothesis(h, f"hypotheses[{i}]")
    return p


def check_build(b: Any) -> list[str]:
    p: list[str] = []
    if not isinstance(b, dict): return ["build is not an object"]
    edits = b.get("edits")
    if not isinstance(edits, list) or not edits: p.append("edits must be a non-empty list")
    else:
        for i, e in enumerate(edits):
            if not isinstance(e, dict) or not isinstance(e.get("old_text"), str) or not e["old_text"] or not isinstance(e.get("new_text"), str):
                p.append(f"edits[{i}] needs non-empty old_text and a new_text")
    _need(b, "summary", str, p, "build")
    if not isinstance(b.get("spec_mapping"), list) or not b["spec_mapping"]: p.append("spec_mapping must list how each spec point was implemented")
    return p


def check_build_exchange(r: Any, others: list[str]) -> list[str]:
    p: list[str] = []
    if not isinstance(r, dict): return ["exchange reply is not an object"]
    rev = r.get("reviews") if isinstance(r.get("reviews"), list) else []
    got = {x.get("member") for x in rev if isinstance(x, dict) and x.get("verdict") in BUILD_VERDICTS and str(x.get("reason") or "").strip()}
    p += [f"missing verdict (correct/incorrect/equivalent + reason) on {m}" for m in others if m not in got]
    p += [f"revised_build: {x}" for x in check_build(r.get("revised_build"))]
    return p


def check_evaluation(r: Any, subjects: list[str]) -> list[str]:
    """subjects: the ids (H1, H2...) that must each get exactly one verdict."""
    p: list[str] = []
    if not isinstance(r, dict): return ["evaluation is not an object"]
    vs = r.get("verdicts") if isinstance(r.get("verdicts"), list) else None
    if vs is None: p.append("verdicts must be a list")
    else:
        ids = [v.get("subject") for v in vs if isinstance(v, dict)]
        p += [f"missing verdict on {s}" for s in subjects if s not in ids] + [f"duplicate verdict on {s}" for s in set(ids) if ids.count(s) > 1]
        p += [f"unknown subject {s}" for s in set(ids) if s not in subjects]
        for i, v in enumerate(vs):
            w = f"verdicts[{i}]"
            if not isinstance(v, dict): p.append(f"{w} is not an object"); continue
            if v.get("outcome") not in OUTCOMES: p.append(f"{w}.outcome must be one of {OUTCOMES}")
            if v.get("recommendation") not in RECOMMENDATIONS: p.append(f"{w}.recommendation must be one of {RECOMMENDATIONS}")
            _need(v, "reason", str, p, w)
            pc = v.get("prediction_checks")
            if not isinstance(pc, list) or not pc: p.append(f"{w}.prediction_checks must list each pre-registered prediction")
            else:
                for j, c in enumerate(pc):
                    for k in ("metric", "expected", "observed"): _need(c, k, str, p, f"{w}.prediction_checks[{j}]")
                    if isinstance(c, dict) and c.get("met") not in (True, False, None): p.append(f"{w}.prediction_checks[{j}].met must be true, false or null")
    iss = r.get("systematic_issues")
    if not isinstance(iss, list): p.append("systematic_issues must be a list (empty if none)")
    else:
        for i, x in enumerate(iss):
            for k in ("issue", "evidence", "suggestion"): _need(x, k, str, p, f"systematic_issues[{i}]")
            if isinstance(x, dict) and x.get("owner") not in OWNERS: p.append(f"systematic_issues[{i}].owner must be one of {OWNERS}")
            if isinstance(x, dict) and x.get("severity") not in SEVERITIES: p.append(f"systematic_issues[{i}].severity must be one of {SEVERITIES}")
    if not isinstance(r.get("lessons"), list) or not all(isinstance(x, str) and x.strip() for x in r["lessons"]) or not r["lessons"]:
        p.append("lessons must be a non-empty list of strings")
    return p


def check_evaluation_exchange(r: Any, others: list[str], subjects: list[str]) -> list[str]:
    p: list[str] = []
    if not isinstance(r, dict): return ["exchange reply is not an object"]
    rev = r.get("reviews") if isinstance(r.get("reviews"), list) else []
    seen = {(x.get("member"), x.get("subject")) for x in rev if isinstance(x, dict) and x.get("stance") in STANCES and str(x.get("reason") or "").strip()}
    p += [f"missing stance (agree/partial/disagree + reason) on {m}:{s}" for m in others for s in subjects if (m, s) not in seen]
    p += [f"revised_evaluation: {x}" for x in check_evaluation(r.get("revised_evaluation"), subjects)]
    return p


def check_review(r: Any) -> list[str]:
    p: list[str] = []
    if not isinstance(r, dict): return ["review is not an object"]
    s = _need(r, "summary", str, p, "review")
    if isinstance(s, str) and len(s.strip().splitlines()) > 12: p.append("summary must be at most 10 lines")
    if r.get("health") not in ("ok", "attention", "problem"): p.append("health must be ok, attention or problem")
    if not isinstance(r.get("alarms"), list) or not all(isinstance(x, str) for x in r["alarms"]): p.append("alarms must be a list of strings")
    ds = r.get("directives")
    if not isinstance(ds, list) or len(ds) > 3: p.append("directives must be a list of at most 3")
    else:
        for i, d in enumerate(ds):
            for k in ("id", "text", "reason"): _need(d, k, str, p, f"directives[{i}]")
        ids = [d.get("id") for d in ds if isinstance(d, dict)]
        if len(ids) != len(set(ids)): p.append("directive ids must be unique")
    ps = r.get("proposals")
    if not isinstance(ps, list): p.append("proposals must be a list (empty if none)")
    else:
        for i, x in enumerate(ps):
            for k in ("what", "why"): _need(x, k, str, p, f"proposals[{i}]")
    return p


def check_review_exchange(r: Any, others: dict[str, list[str]]) -> list[str]:
    """others: colleague label -> directive ids that need a stance."""
    p: list[str] = []
    if not isinstance(r, dict): return ["exchange reply is not an object"]
    rev = r.get("reviews") if isinstance(r.get("reviews"), list) else []
    seen = {(x.get("member"), x.get("directive_id")) for x in rev if isinstance(x, dict) and x.get("stance") in STANCES and str(x.get("reason") or "").strip()}
    p += [f"missing stance (agree/partial/disagree + reason) on {m}:{d}" for m, ids in others.items() for d in ids if (m, d) not in seen]
    p += [f"revised_review: {x}" for x in check_review(r.get("revised_review"))]
    return p


def check_exchange(r: Any, others: dict[str, list[str]], max_hypotheses: int = 3) -> list[str]:
    """others: member -> hypothesis ids that this member must give a stance on."""
    p: list[str] = []
    if not isinstance(r, dict): return ["exchange reply is not an object"]
    rev = r.get("reviews")
    if not isinstance(rev, list): p.append("reviews must be a list")
    else:
        seen = {(x.get("member"), x.get("hypothesis_id")) for x in rev if isinstance(x, dict)}
        for x in rev:
            if not isinstance(x, dict) or x.get("stance") not in STANCES or not str(x.get("reason") or "").strip():
                p.append(f"each review needs member, hypothesis_id, stance in {STANCES} and a reason")
                break
        for m, ids in others.items():
            for hid in ids:
                if (m, hid) not in seen: p.append(f"missing stance on {m}:{hid}")
    p += [f"revised_report: {x}" for x in check_scientist_report(r.get("revised_report"), max_hypotheses)]
    return p
