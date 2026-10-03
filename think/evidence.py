"""The evidence pack: facts computed by code for the Scientists (and later the Evaluators and Supervisor)."""
from __future__ import annotations
import json, time, urllib.request


def _mean(xs): return round(sum(xs) / len(xs), 2) if xs else None


def evidence_pack(think) -> dict:
    """Champion results split into edge before vs after costs, exits, regimes; blocks; candidates; lessons; data quality."""
    pack = think.analysis_pack(); trades = think.champion_trades()
    entries = think.store.get("trade", "entries", {}) or {}
    gross = [(t["net"] + t["fees"]) / t["notional"] * 1e4 for t in trades if t["notional"]]
    fees = [t["fees"] / t["notional"] * 1e4 for t in trades if t["notional"]]
    by_regime: dict = {}
    for t in trades:
        reg = ((entries.get(str(t["entry_ms"])) or {}).get("features") or {}).get("regime") or "unknown"
        by_regime.setdefault(reg, []).append(t["net_bps"])
    exc = (pack.get("champion") or {}).get("excursions") or []
    edge = {"trades": len(trades), "gross_bps_per_trade": _mean(gross), "fees_bps_per_trade": _mean(fees),
            "net_bps_per_trade": _mean([t["net_bps"] for t in trades]),
            "note": "gross = before fees (fills already include 1 bps slippage each side); net = after everything",
            "by_regime": {k: {"n": len(v), "net_bps": _mean(v)} for k, v in by_regime.items()},
            "exits": {"avg_best_bps_during_trade": _mean([e["mfe_bps"] for e in exc]), "avg_worst_bps_during_trade": _mean([e["mae_bps"] for e in exc]),
                      "avg_captured_pct_of_best": _mean([e["captured_pct"] for e in exc if e.get("captured_pct") is not None])}}
    lessons = [{k: n.get(k) for k in ("type", "candidate", "hypothesis", "verdict", "why", "lesson")}
               for n in think.notebook(300) if n["type"] in ("verdict", "screen", "rollback", "champion_changed", "lesson")][-20:]
    candidates = [{"id": p["id"], "kind": p.get("kind"), "hypothesis": p.get("hypothesis"), "status": p.get("status"),
                   "overrides": p.get("overrides"), "result": (p.get("evaluation") or {}).get("candidate", {})} for p in think.pool()][-8:]
    try:
        with urllib.request.urlopen("http://127.0.0.1:8900/status", timeout=3) as r: s = json.loads(r.read())
        f = next(iter(s["feeds"].values())); data_quality = {"source": "tt_input", "ok": s["ok"], "alarms": [a["text"] for a in s["alarms"]],
                                                              "today": {k: f["today"].get(k) for k in ("coverage_pct", "checksum_fail", "sequence_gaps", "reconnects")}}
    except Exception as exc:
        data_quality = {"source": "tt_input", "error": f"{type(exc).__name__}"}
    return {"generated_at": time.time(), "edge": edge, "champion": pack.get("champion"), "blocks_24h": pack.get("block_funnel_24h"),
            "blocks_7d": pack.get("block_funnel_7d"), "config": pack.get("config"), "candidates": candidates, "lessons": lessons,
            "directives": [{k: d.get(k) for k in ("text", "reason", "by")} for d in think.store.get("think", "directives", []) or []
                           if isinstance(d, dict) and float(d.get("expires") or 0) > time.time()], "data_quality": data_quality,
            "systematic_issues": (think.store.get("lab", "issues", {}) or {}).get("issues", []),
            "recordings": pack.get("recordings")}
