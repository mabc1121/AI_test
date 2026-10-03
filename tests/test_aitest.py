"""AI_test strategy tests: model arrays, the multi-position paper broker and costs, the horizon slots' entries and
exits, the prediction log, and restart without warm-up (saved history + refill from the recordings).
They run offline, without scikit-learn or pandas, like the rest of the suite."""
from __future__ import annotations

import gzip
import json
import math
import os
import random
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def trade_module(name="aitest_trade_under_test"):
    from core.common import import_module
    return import_module(ROOT / "trade" / "trade.py", name)


def integrity(m):
    return m.MarketIntegrity(True, True, True, True, True, False, 1, 0, 1, 0, 1, 0, 0)


def event(m, kind, t_ms, payload, bid, ask):
    mid = (bid + ask) / 2
    return m.CleanMarketEvent(kind, "test", "tBTCUSD", "book" if kind.startswith("l3") else "trades", 1, None,
                              int(t_ms) * 1_000_000, int(t_ms), int(t_ms), payload, None, {}, bid, ask, mid,
                              (ask - bid) / mid * 1e4, integrity(m))


def synthetic_stream(m, t0_ms, minutes, seed=7):
    """a small random-walk R0 book with updates and trades, a few events per second"""
    rnd = random.Random(seed)
    orders = {}; oid = [1000]; mid = [84000.0]

    def best():
        bids = [p for p, a in orders.values() if a > 0]; asks = [p for p, a in orders.values() if a < 0]
        return max(bids), min(asks)

    rows = []
    for i in range(1, 26):
        for sign in (1, -1):
            oid[0] += 1; amt = round(rnd.uniform(0.01, 0.8), 5) * sign
            orders[oid[0]] = (mid[0] - sign * i, amt); rows.append([oid[0], mid[0] - sign * i, amt])
    b, a = best()
    out = [event(m, "l3_snapshot", t0_ms, rows, b, a)]
    for s in range(minutes * 60):
        for k in range(rnd.randint(1, 5)):
            t = t0_ms + s * 1000 + k * 150
            if rnd.random() < 0.15:
                mid[0] += rnd.choice((-1.0, 1.0))
            r = rnd.random()
            if r < 0.45 and len(orders) > 30:                     # cancel a random order
                victim = rnd.choice(list(orders)); p, amt = orders.pop(victim)
                if not [1 for q, x in orders.values() if x > 0] or not [1 for q, x in orders.values() if x < 0]:
                    orders[victim] = (p, amt); continue
                b, a = best(); out.append(event(m, "l3_update", t, [victim, 0, 1 if amt > 0 else -1], b, a))
            elif r < 0.9:                                          # new order near the mid, never crossing
                sign = 1 if rnd.random() < 0.5 else -1; b, a = best()
                p = (min(b + rnd.randint(-3, 1), a - 1)) if sign > 0 else (max(a + rnd.randint(-1, 3), b + 1))
                oid[0] += 1; amt = round(rnd.uniform(0.001, 1.5), 5) * sign
                orders[oid[0]] = (float(p), amt); b, a = best()
                out.append(event(m, "l3_update", t, [oid[0], float(p), amt], b, a))
            else:                                                  # a public trade at the touch
                b, a = best(); amt = round(rnd.uniform(0.001, 1.2), 5) * (1 if rnd.random() < 0.5 else -1)
                out.append(event(m, "public_trade", t, [oid[0], t, amt, a if amt > 0 else b], b, a))
    return out


def test_aitest_model_arrays_match_sklearn_reference():
    m = trade_module("aitest_models_check")
    models = m.load_models()
    fx = json.loads((ROOT / "tests" / "fixtures" / "aitest_reference_predictions.json").read_text())
    assert models["feature_names"] == fx["feature_names"] and len(models["feature_names"]) == 109
    assert set(models["horizons"]) == {"30", "60", "120", "240"} and models["l2_signals"]
    for row in fx["rows"]:
        x = [m.NAN if v is None else v for v in row["x"]]
        for H, exp in row["expected"].items():
            hm = models["horizons"][H]["models"]
            assert abs(m._sigmoid(m._tree_raw(hm["dir"], x)) - exp["p_up"]) < 1e-9
            assert abs(m._sigmoid(m._tree_raw(hm["hit"], x)) - exp["p_hit"]) < 1e-9
            assert abs(m._tree_raw(hm["vol"], x) - exp["vol_raw"]) < 1e-9


def test_aitest_broker_costs_and_concurrent_positions():
    m = trade_module("aitest_broker_check")
    assert m.PAPER_DEFAULTS["fee_bps"] == 0.0 and m.PAPER_DEFAULTS["slippage_bps"] == 0.5
    b = m._ProtectedPaperBroker(10_000.0, fee_bps=2.0, slippage_bps=1.0)
    e = event(m, "l3_update", 1_790_000_000_000, [1, 100.0, 1.0], 84000.0, 84002.0)
    for pid, act in (("H30-1", "ENTER_LONG"), ("H60-1", "ENTER_SHORT"), ("H240-1", "ENTER_LONG")):
        rec = b.execute(m.TradeDecision(act, "tBTCUSD", 0.01, reason=f"{pid.split('-')[0]}_{'long' if act == 'ENTER_LONG' else 'short'}",
                                        metadata={"position_id": pid, "strategy_tag": pid.split("-")[0]}), e)
        assert rec.position_id == pid
    assert b.execute(m.TradeDecision("ENTER_LONG", "tBTCUSD", 0.01, metadata={"position_id": "H30-1"}), e) is None   # id taken
    assert sorted(b.positions) == ["H240-1", "H30-1", "H60-1"]
    long_fill = b.positions["H30-1"].entry_price; short_fill = b.positions["H60-1"].entry_price
    assert abs(long_fill - 84002.0 * (1 + 1e-4)) < 1e-9 and abs(short_fill - 84000.0 * (1 - 1e-4)) < 1e-9
    assert abs(b.entry_fees["H30-1"] - long_fill * 0.01 * 2e-4) < 1e-12
    x = b.execute(m.TradeDecision("EXIT", "tBTCUSD", metadata={"position_id": "H60-1"}), e)
    assert x.position_id == "H60-1" and "H60-1" not in b.positions and len(b.positions) == 2
    exit_fill = 84002.0 * (1 + 1e-4)
    assert abs(x.realized_pnl - (-0.01 * (exit_fill - short_fill) - short_fill * 0.01 * 2e-4 - exit_fill * 0.01 * 2e-4)) < 1e-9
    assert b.metrics(e).accounting_valid
    # old single-position strategies: no position_id -> keyed by the symbol, as in contract 2.2
    b2 = m._ProtectedPaperBroker()
    assert b2.execute(m.TradeDecision("ENTER_LONG", "tBTCUSD", 0.01), e).position_id == "tBTCUSD"
    assert b2.execute(m.TradeDecision("ENTER_LONG", "tBTCUSD", 0.01), e) is None
    from think.replay import pair_trades
    ex = [r.as_dict() for r in b.executions]
    trades = pair_trades(ex)
    assert len(trades) == 1 and trades[0]["setup"] == "H60" and trades[0]["side"] == "short" and trades[0]["position_id"] == "H60-1"


def test_aitest_slots_enter_concurrently_and_exit_on_barrier_and_time():
    m = trade_module("aitest_slots_check")
    core = m.ScientificCore(m.TradeConfig()); core.history_mode, core.history_checked = "off", True
    broker = m._ProtectedPaperBroker()
    t0 = 1_790_000_040_000
    snap = [[1, 83999.0, 1.0], [2, 84001.0, -1.0]]
    def step(t, bid, ask, kind="l3_update", payload=None):
        e = event(m, kind, t, payload if payload is not None else [9, 1.0, 0.001], bid, ask)
        d = core.decide(e, list(broker.positions.values())); broker.execute(d, e); return d
    step(t0, 83999.0, 84001.0, "l3_snapshot", snap)
    for H, side in ((30, "long"), (60, "short"), (240, "long")):
        core.entry_queue.append({"H": H, "side": side, "minute_ms": t0 - 60_000, "p_hit": 0.8, "score": 0.1 if side == "long" else -0.1,
                                 "sigma1": 2.0, "queued_ms": t0})
    acts = [step(t0 + 100 * (i + 1), 83999.0, 84001.0).action for i in range(4)]
    assert acts[:3] == ["ENTER_LONG", "ENTER_SHORT", "ENTER_LONG"] and acts[3] == "HOLD", acts
    assert sorted(p.position_id for p in broker.positions.values()) == [f"H240-{t0-60_000}", f"H30-{t0-60_000}", f"H60-{t0-60_000}"]
    st30 = core.position_state[f"H30-{t0-60_000}"]
    assert abs((st30["tp"] / st30["entry_est"] - 1) * 1e4 - 2.0 * math.sqrt(30)) < 0.01      # barrier = k * sigma * sqrt(H)
    # price jumps up 15 bps: the 30-min long hits its target (10.95 bps), the 60-min short its stop
    d1 = step(t0 + 1000, 84125.0, 84127.0); d2 = step(t0 + 1100, 84125.0, 84127.0)
    assert {d1.reason, d2.reason} == {"H30_target", "H60_stop"}, (d1.reason, d2.reason)
    assert [p.position_id for p in broker.positions.values()] == [f"H240-{t0-60_000}"]
    # the 240-min long (barrier 31 bps) stays until its time cap
    assert step(t0 + 240 * 60_000 - 1000, 84125.0, 84127.0).action == "HOLD"
    d = step(t0 + 240 * 60_000 + 500, 84125.0, 84127.0)
    assert d.action == "EXIT" and d.reason == "H240_time" and not broker.positions
    # a disabled slot is predicted and logged but a full slot never opens a second position
    core.config.max_positions_per_horizon = 1
    trades = __import__("think.replay", fromlist=["pair_trades"]).pair_trades([r.as_dict() for r in broker.executions])
    assert sorted(t["setup"] for t in trades) == ["H240", "H30", "H60"]
    json.dumps({"position_state": core.position_state, "cooldown_until_ms": core.cooldown_until_ms})


def _bars(core, m):
    return {int(b[m.FI["t"]]): b for b in core.hist}


def _same(a, b):
    return all((x != x and y != y) or x == y for x, y in zip(a, b))


def test_aitest_restart_needs_no_warmup():
    """A: runs and saves; the app stops; B restores the saved history, refills the gap from the recordings, then
    continues live. B's minute bars and features must equal those of C, which never stopped."""
    from think.replay import encode_event
    m = trade_module("aitest_restart_check")
    now = int(time.time() * 1000)
    t0 = (now - 30 * 60_000) // 60_000 * 60_000 + 7_000
    events = synthetic_stream(m, t0, 24)
    with tempfile.TemporaryDirectory() as temp:
        old = os.environ.get("APP_RUNTIME_DIR"); os.environ["APP_RUNTIME_DIR"] = temp
        try:
            cfg = dict(save_history_every_minutes=5, min_history_minutes=1)   # the refill then covers several whole minutes
            C = m.ScientificCore(m.TradeConfig(**cfg)); C.history_mode, C.history_checked = "off", True
            for e in events:
                C._apply_event(e)
            A = m.ScientificCore(m.TradeConfig(**cfg)); A.history_mode, A.history_checked = "primary", True
            base = t0 // 60_000                               # A dies 25 s into the minute two after its last save,
            k = next(k for k in range(12, 18) if (base + k) % 5 == 1)  # so the refill covers a whole minute and a part
            stop_at = (base + k) * 60_000 + 25_000
            rec_dir = Path(temp) / "recordings"; rec_dir.mkdir()
            recorded = []
            for e in events:
                if e.recv_time_ms >= stop_at:
                    break
                A._apply_event(e); recorded.append(e)
            assert (Path(temp) / m.HISTORY_FILE).is_file()
            saved = json.loads(gzip.open(Path(temp) / m.HISTORY_FILE, "rt").read())
            assert saved["last_sec"] % 60 == 59 and saved["bars"]
            # the platform recorder: every event of the run, hourly files
            by_hour = {}
            for e in recorded:
                by_hour.setdefault(time.strftime("%Y%m%d-%H", time.gmtime(e.recv_time_ms / 1000)), []).append(encode_event(e, False))
            for hour, lines in by_hour.items():
                with gzip.open(rec_dir / f"{hour}.jsonl.gz", "at", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
            B = m.ScientificCore(m.TradeConfig(**cfg))
            B.on_runtime_initialize({"project": "aitest-test"})   # restore + refill up to now
            assert B.history_mode == "primary" and B.last_features.get("gapfill_events", 0) > 0
            for e in events:
                if e.recv_time_ms >= stop_at:
                    B._apply_event(e)
            cb, bb = _bars(C, m), _bars(B, m)
            assert sorted(cb) == sorted(bb), (len(cb), len(bb))
            diff = [t for t in cb if not _same(cb[t], bb[t])]
            assert not diff, f"{len(diff)} minutes differ, first {diff[:3]}"
            fc, fb = C.feature_values, B.feature_values
            assert all((fc[k] != fc[k] and fb[k] != fb[k]) or abs(fc[k] - fb[k]) < 1e-12 for k in fc)
            assert len(B.hist) >= 23 and B.status == "active"
            # the prediction log of the primary cores: one line per minute with every horizon
            log = sorted((Path(temp) / "aitest_predictions").glob("*.jsonl"))
            lines = [json.loads(x) for p in log for x in p.read_text().splitlines()]
            assert lines and all(f"H{H}" in lines[-1] for H in (30, 60, 120, 240)) and "p_hit" in lines[-1]["H60"]
            assert any(x["backfilled"] for x in lines) and not lines[-1]["backfilled"]
        finally:
            if old is None: os.environ.pop("APP_RUNTIME_DIR", None)
            else: os.environ["APP_RUNTIME_DIR"] = old
