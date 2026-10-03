"""Workers group: turns one selected hypothesis into exactly one trade.py (or, for a probe, a measurement).

params -> built by code (TradeConfig defaults); several proposed values are screened by replay, the best kept
code   -> built independently by 2+ AI builders; builds are compared by replaying them on the same recorded
          market: identical decisions = the spec was clear (smallest build kept); different decisions = builders
          review each other; still different = the hypothesis goes back to the Scientists as ambiguous
probe  -> no trading change: the champion is replayed without trading, every confirmed setup is logged and the
          real forward price move after 1/5/15/30 minutes is measured against the round-trip cost
Every build passes the contract validator (protected blocks, paper-only, self-test) before it can be tested.
"""
from __future__ import annotations
import bisect, hashlib, itertools, json, math, subprocess, sys, tempfile, time
from pathlib import Path

from agents.documents import check_build, check_build_exchange
from agents.unit import Member, Unit
from core.common import import_module
from think.replay import decode_event, iter_rows, replay, stats

EDITABLE_BEGIN = "# EDITABLE:SCIENTIFIC_WORKSPACE BEGIN"; EDITABLE_END = "# EDITABLE:SCIENTIFIC_WORKSPACE END"
PROBE_HINTS = ("trading decisions do not change", "instrumentation only", "shadow-only probe", "no trading change")
ROUND_TRIP_COST_BPS = 13.0


def triage(h: dict) -> tuple[str, str | None]:
    """The build route for a hypothesis; an instrumentation-only 'code' hypothesis is routed as a probe (recorded)."""
    kind = h.get("kind"); text = (str((h.get("change") or {}).get("description", "")) + " " + str(h.get("title", ""))).lower()
    if kind == "code" and any(x in text for x in PROBE_HINTS): return "probe", "code hypothesis changes no trading: routed as a probe"
    return kind, None


def apply_edits(src: str, edits: list[dict]) -> str:
    for i, e in enumerate(edits):
        old, new = e["old_text"], e["new_text"]; n = src.count(old)
        if n != 1: raise ValueError(f"edits[{i}].old_text must occur exactly once (found {n})")
        lo, hi, pos = src.index(EDITABLE_BEGIN), src.index(EDITABLE_END), src.index(old)
        if not (lo < pos and pos + len(old) < hi): raise ValueError(f"edits[{i}] is outside the editable scientific workspace")
        src = src.replace(old, new, 1)
    return src


def validate_source(root: Path, src: str, champion: Path) -> list[str]:
    if '"paper_only": True' not in src: return ["trade must stay paper-only"]
    try: compile(src, "trade.py", "exec")
    except SyntaxError as exc: return [f"SyntaxError: {exc}"]
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f: f.write(src)
    try:
        proc = subprocess.run([sys.executable, str(root / "trade" / "validate_trade_contract.py"), f.name, "--canonical", str(champion), "--run-self-test"],
                              capture_output=True, text=True, timeout=120)
        out = json.loads(proc.stdout or "{}")
        if proc.returncode or not out.get("ok") or not out.get("self_test", {}).get("ok"):
            return [f"contract validation failed: {out.get('errors') or proc.stderr[-800:]}"]
        return []
    finally: Path(f.name).unlink(missing_ok=True)


def changed_lines(a: str, b: str) -> int:
    import difflib
    return sum(1 for l in difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=0) if l[:1] in "+-" and not l.startswith(("+++", "---")))


def _fingerprint(arm: dict) -> list:
    return [(t["entry_ms"], t["side"], t["exit_ms"], t["exit_reason"]) for t in arm["trades"]]


def compare_by_replay(think, variants: dict[str, tuple], hours: float) -> dict:
    """Replay the champion and each variant on the same recorded window; report decisions and differences."""
    champ = think.champion_module(); now = int(time.time() * 1000)
    arms = [("champion", champ, {})] + [(n, m, ov) for n, (m, ov) in variants.items()]
    res = replay(arms, think.rec_dir, now - int(hours * 3_600_000), now, budget_s=1800, base_mod=champ)
    out = {"window_hours": hours, "events": res["events"], "complete": res["complete"], "arms": {}}
    fps = {n: _fingerprint(a) for n, a in res["arms"].items()}
    for n, a in res["arms"].items():
        s = a["stats"]
        out["arms"][n] = {"error": a["error"], "trades": s.get("n", 0), "net": s.get("net"), "net_bps": s.get("expectancy_bps"),
                          "reasons": a["reasons"], "same_decisions_as_champion": fps[n] == fps["champion"],
                          "first_trades": [(time.strftime("%H:%M:%S", time.gmtime(e / 1000)), side, r) for e, side, _, r in fps[n][:12]]}
    names = list(variants); out["identical_pairs"] = [[a, b] for a, b in itertools.combinations(names, 2) if fps[a] == fps[b]]
    return out


def _grid(h: dict, tun: dict) -> dict:
    def differs(x, k):
        try: return float(x) != float(tun[k])
        except (TypeError, ValueError): return x != tun[k]
    g = {k: [x for x in (v if isinstance(v, list) else [v]) if differs(x, k)][:4] for k, v in ((h.get("change") or {}).get("params") or {}).items() if k in tun}
    return dict(list((k, v) for k, v in g.items() if v)[:2])


def build_params(think, h: dict, out: Path, hours: float) -> dict:
    from think.think import set_config_defaults
    tun = think.tunables(); grid = _grid(h, tun)
    if not grid: return {"status": "rejected", "why": "no proposed value differs from the champion's current settings"}
    combos = [dict(zip(grid, c)) for c in itertools.product(*grid.values())][:8]; screen = None
    if len(combos) > 1:
        champ = think.champion_module()
        screen = compare_by_replay(think, {f"v{i}": (champ, ov) for i, ov in enumerate(combos)}, hours)
        scored = [(screen["arms"][f"v{i}"].get("net_bps") if screen["arms"][f"v{i}"]["trades"] >= 3 else None, i) for i in range(len(combos))]
        best = max((s for s in scored if s[0] is not None), default=(None, 0))[1]
    else: best = 0
    overrides = combos[best]; champion_src = think.trade_file.read_text(encoding="utf-8")
    src = set_config_defaults(champion_src, overrides); problems = validate_source(think.root, src, think.trade_file)
    if problems: return {"status": "failed", "why": "; ".join(problems)}
    (out / "trade.py").write_text(src, encoding="utf-8")
    return {"status": "built", "route": "params", "overrides": overrides, "candidates_screened": combos if screen else None,
            "screen": screen, "changed_lines": changed_lines(champion_src, src), "builders": "deterministic"}


def build_code(think, h: dict, out: Path, members: list[Member], record, budget_ok, tools, handler, hours: float, rounds: int, call=None) -> dict:
    champion_src = think.trade_file.read_text(encoding="utf-8"); cache: dict[str, dict] = {}
    spec = {k: h.get(k) for k in ("key", "title", "change", "rationale", "predictions", "falsified_if")}
    task = ("TASK: implement this pre-registered hypothesis in trade/trade.py and submit your build via submit_build.\n\n"
            f"HYPOTHESIS:\n{json.dumps(spec, default=str)}")
    def check(doc):
        p = check_build(doc)
        if p: return p
        try: return validate_source(think.root, apply_edits(champion_src, doc["edits"]), think.trade_file)
        except (ValueError, KeyError) as exc: return [str(exc)]
    def comparison(builds: dict) -> dict:
        key = hashlib.sha256(json.dumps(builds, sort_keys=True, default=str).encode()).hexdigest()
        if key not in cache:
            mods = {}
            for label, b in builds.items():
                p = out / "replay" / f"{hashlib.sha256(json.dumps(b['edits']).encode()).hexdigest()[:10]}.py"; p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(apply_edits(champion_src, b["edits"]), encoding="utf-8"); mods[label] = (import_module(p, f"build_{p.stem}"), {})
            cache[key] = compare_by_replay(think, mods, hours)
        return cache[key]
    def exchange_prompt(m, mine, others):
        me = next(lbl for n, lbl in unit.labels.items() if n == m.name)
        cmp = comparison({me: mine, **others})
        return (f"{task}\n\nYOUR CURRENT BUILD ({me}):\n{json.dumps(mine)}\n\nCOLLEAGUE BUILDS:\n"
                + "\n\n".join(f"{lbl}:\n{json.dumps(b)}" for lbl, b in others.items())
                + f"\n\nREPLAY COMPARISON (same recorded market, {hours} h):\n{json.dumps(cmp, default=str)}"
                + "\n\nGive a verdict on each colleague build and submit your revised build via submit_exchange.")
    def check_exchange(m, others):
        return lambda doc: check_build_exchange(doc, list(others)) or check(doc.get("revised_build"))
    def stable(prev, new, reviews):
        return all(r.get("verdict") in ("correct", "equivalent") for rs in reviews.values() for r in rs) or prev == new
    build_schema = {"type": "object", "properties": {"edits": {"type": "array", "items": {"type": "object", "properties": {"old_text": {"type": "string"}, "new_text": {"type": "string"}}}},
                    "summary": {"type": "string"}, "spec_mapping": {"type": "array", "items": {"type": "object"}}, "ambiguities": {"type": "array", "items": {"type": "string"}}},
                    "required": ["edits", "summary", "spec_mapping"]}
    submit_build = {"type": "function", "name": "submit_build", "strict": False, "description": "Submit your build: exact edits, summary, spec mapping, ambiguities.", "parameters": build_schema}
    submit_ex = {"type": "function", "name": "submit_exchange", "strict": False, "description": "Submit verdicts on colleague builds and your revised build.",
                 "parameters": {"type": "object", "properties": {"reviews": {"type": "array", "items": {"type": "object", "properties": {
                     "member": {"type": "string"}, "verdict": {"type": "string", "enum": ["correct", "incorrect", "equivalent"]}, "reason": {"type": "string"}}}},
                     "revised_build": build_schema}, "required": ["reviews", "revised_build"]}}
    kw = {"call": call} if call else {}
    unit = Unit("workers", members, "worker", out, record, budget_ok, tools=tools, handler=handler, rounds=rounds, **kw)
    res = unit.run(first_prompt=lambda m: task, exchange_prompt=exchange_prompt, first_submit=submit_build, exchange_submit=submit_ex,
                   check_first=check, check_exchange=check_exchange, stable=stable, revised_key="revised_build")
    builds = res["reports"]; name_of = {v: k for k, v in res["labels"].items()}
    accepted = {}
    for author, b in builds.items():  # accepted = every colleague that reviewed it called it correct/equivalent
        verdicts = [r["verdict"] for rv, rs in res["reviews"].items() if rv != author for r in rs if name_of.get(r.get("member")) == author]
        accepted[author] = all(v in ("correct", "equivalent") for v in verdicts)
    final = comparison({res["labels"][a]: b for a, b in builds.items()}) if builds else {}
    label_fp = {a: final["arms"][res["labels"][a]] for a in builds} if builds else {}
    groups: list[list[str]] = []
    for a in builds:
        for g in groups:
            if [res["labels"][a], res["labels"][g[0]]] in final["identical_pairs"] or [res["labels"][g[0]], res["labels"][a]] in final["identical_pairs"]: g.append(a); break
        else: groups.append([a])
    card = {"route": "code", "members": res["available"], "unavailable": res["unavailable"], "reduced_independence": res["independence"]["reduced"],
            "independence": res["independence"],
            "rounds": res["rounds"], "accepted": accepted, "decision_groups": groups, "replay": final, "prompt": unit.prompt_id}
    candidates = [g for g in groups if all(accepted[a] for a in g)]
    if len(groups) > 1 and len(candidates) != 1:
        return {**card, "status": "ambiguous", "why": "independent builds make different trading decisions and the builders did not resolve it"}
    chosen_group = candidates[0] if candidates else groups[0]
    if not candidates: return {**card, "status": "failed", "why": "no build was accepted by its reviewers"}
    chosen = min(chosen_group, key=lambda a: changed_lines(champion_src, apply_edits(champion_src, builds[a]["edits"])))
    src = apply_edits(champion_src, builds[chosen]["edits"]); problems = validate_source(think.root, src, think.trade_file)
    if problems: return {**card, "status": "failed", "why": "; ".join(problems)}
    (out / "trade.py").write_text(src, encoding="utf-8")
    return {**card, "status": "built", "chosen_build": chosen, "summary": builds[chosen].get("summary"), "spec_mapping": builds[chosen].get("spec_mapping"),
            "ambiguities": builds[chosen].get("ambiguities") or [], "changed_lines": changed_lines(champion_src, src),
            "same_decisions_as_champion": label_fp[chosen]["same_decisions_as_champion"]}


def _probe_window(mod, rec_dir, start: int, end: int, horizons, cluster_s: int, duty: float) -> list[dict]:
    """Signals confirmed in [start, end) with their forward returns (mids are read past `end` for the horizons).
    CPU-throttled: after each block of rows it sleeps so the probe uses at most `duty` of one core."""
    core = mod.ScientificCore(mod.TradeConfig()); tail = end + max(horizons) * 1000 + 60_000
    mids_t: list[int] = []; mids: list[float] = []; signals: list[dict] = []; last_side: dict[str, int] = {}
    busy_from = time.monotonic(); rows = 0
    for row in iter_rows(rec_dir, start, tail):
        rows += 1
        if rows % 5000 == 0:
            spent = time.monotonic() - busy_from; time.sleep(spent * (1 - duty) / duty); busy_from = time.monotonic()
        if row.get("m") and (not mids_t or row["t"] // 1000 != mids_t[-1] // 1000): mids_t.append(row["t"]); mids.append(float(row["m"]))
        if row["t"] >= end: continue                      # past the window: only prices for the horizons
        ev = decode_event(row, mod)
        if row.get("d"):
            d = core.decide(ev, [])
            if str(d.action).startswith("ENTER"):
                side = "long" if "LONG" in d.action else "short"
                if row["t"] - last_side.get(side, -10**15) >= cluster_s * 1000 and row.get("m"):
                    signals.append({"t": row["t"], "side": side, "setup": "reversal" if "reversal" in d.reason else "continuation", "mid": float(row["m"])})
                last_side[side] = row["t"]; core.position_state.pop(ev.symbol, None)   # no position is ever opened
        else: core._apply_event(ev)
    for s in signals:
        for hz in horizons:
            i = bisect.bisect_left(mids_t, s["t"] + hz * 1000)
            if i < len(mids_t) and mids_t[i] - (s["t"] + hz * 1000) <= 60_000:
                s[f"r{hz}"] = round((mids[i] - s["mid"]) / s["mid"] * 1e4 * (1 if s["side"] == "long" else -1), 2)
    return signals


def forward_return_probe(think, min_signals: int = 30, max_days: float = 7.0, horizons=(60, 300, 900, 1800), cluster_s: int = 60, duty: float = 0.5) -> dict:
    """Replay the champion without trading on the recorded market, newest day first, until the pre-registered
    number of signals is reached or `max_days` (or all recorded data) is used; log each confirmed setup (one per
    side per minute) and measure the signed mid-price move after each horizon, against the ~13 bps round-trip cost.
    `complete` is False when the data ran out first - the Lab then measures again later on more data."""
    from think.replay import recorded_span
    mod = think.champion_module(); now = int(time.time() * 1000)
    avail = min(float(max_days), float(recorded_span(think.rec_dir).get("hours") or 0) / 24); used = 0.0; signals: list[dict] = []
    while used < avail and len(signals) < min_signals:
        step = min(1.0, avail - used); end = now - int(used * 86_400_000)
        signals = _probe_window(mod, think.rec_dir, end - int(step * 86_400_000), end, horizons, cluster_s, duty) + signals; used += step
    def summarise(rets):
        n = len(rets)
        if not n: return {"n": 0}
        mean = sum(rets) / n; sd = math.sqrt(sum((x - mean) ** 2 for x in rets) / (n - 1)) if n > 1 else 0.0
        return {"n": n, "mean_bps": round(mean, 2), "t_stat": round(mean / (sd / math.sqrt(n)), 2) if sd else None,
                "share_beating_cost": round(sum(x >= ROUND_TRIP_COST_BPS for x in rets) / n * 100, 1)}
    table = {}
    for hz in horizons:
        rets = {}
        for s in signals:
            if f"r{hz}" in s: rets.setdefault("all", []).append(s[f"r{hz}"]); rets.setdefault(f"{s['setup']}/{s['side']}", []).append(s[f"r{hz}"])
        table[f"{hz // 60}m"] = {k: summarise(v) for k, v in sorted(rets.items())}
    best = max(((k, v["all"]) for k, v in table.items() if v.get("all", {}).get("n")), key=lambda kv: kv[1]["mean_bps"], default=(None, {}))
    return {"route": "probe", "status": "measured", "days": round(used, 2), "max_days": max_days, "min_signals": min_signals, "complete": len(signals) >= min_signals,
            "measured_at": time.time(), "signals": len(signals), "cost_bps": ROUND_TRIP_COST_BPS, "horizons": table,
            "best_horizon": best[0], "best": best[1], "any_horizon_beats_cost": any(v.get("all", {}).get("mean_bps", -1e9) >= ROUND_TRIP_COST_BPS and (v["all"].get("t_stat") or 0) >= 2 for v in table.values()),
            "sample": signals[:20]}
