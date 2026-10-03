#!/usr/bin/env python3
"""
live_replay_smoke.py — run the real app trade engine on recorded raw Bitfinex websocket messages, at their original
timestamps, as if they were arriving live: the protected R0 layer (sequence, checksum gate, staleness), the strategy,
the paper broker, the checkpoints, the market recorder and the prediction log all run unmodified. Optionally the
engine is stopped and a new one started part-way (a restart), which must continue without warm-up.

  python live_replay_smoke.py --raw <raw dir> --hours 8 --restart-after 6 [--out dir]

The process clock is replaced by the message timestamps (time.time / time_ns / monotonic), so staleness checks and
file names behave as they would live. Use a throw-away runtime directory (default: a temp dir).
"""
import argparse
import gzip
import json
import math
import os
import re
import sys
import tempfile
import time as _time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
try:
    import orjson
    loads = orjson.loads
except ImportError:
    loads = json.loads

SIM = {"ns": 0}
_real = {"time": _time.time, "time_ns": _time.time_ns, "monotonic": _time.monotonic}


def install_clock():
    _time.time_ns = lambda: SIM["ns"]
    _time.time = lambda: SIM["ns"] / 1e9
    _time.monotonic = lambda: SIM["ns"] / 1e9


def session_files(root):
    files = sorted(Path(root).glob("*/*/*/*.jsonl.gz"))
    starts = {}
    for f in files:
        with gzip.open(f, "rt") as fh:
            starts[f] = loads(fh.readline())["r"]
    files.sort(key=lambda f: starts[f])
    sess_of = {f: int(re.search(r"-(\d+)\.jsonl\.gz$", str(f)).group(1)) for f in files}
    first = {}
    for f in files:
        first[sess_of[f]] = min(first.get(sess_of[f], starts[f]), starts[f])
    order = sorted(first, key=lambda s: first[s])
    cutoff = {s: (first[order[i + 1]] if i + 1 < len(order) else math.inf) for i, s in enumerate(order)}
    return files, sess_of, cutoff, starts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--restart-after", type=float, default=6.0, help="hours; 0 = no restart")
    ap.add_argument("--downtime-min", type=float, default=10.0, help="minutes the app is down during the restart")
    ap.add_argument("--runtime", default=None)
    ap.add_argument("--out", default=str(HERE / "live_replay_out"))
    a = ap.parse_args()
    runtime = Path(a.runtime or tempfile.mkdtemp(prefix="aitest_smoke_"))
    os.environ["APP_RUNTIME_DIR"] = str(runtime)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    files, sess_of, cutoff, starts = session_files(a.raw)
    t_begin = starts[files[0]]
    SIM["ns"] = t_begin * 1_000_000
    install_clock()
    from trade.worker import TradeEngine

    def new_engine():
        eng = TradeEngine(REPO, record=True)
        eng.recorder.keep_days = 3650
        feed = eng.mod._BitfinexPublicFeed(eng.runtime, eng.runtime.market_config)
        return eng, feed

    eng, feed = new_engine()
    stats = {"messages": 0, "integrity_errors": [], "restarts": 0, "reconnects": 0, "decisions": {}, "minutes": 0}
    end_ms = t_begin + int(a.hours * 3_600_000)
    restart_ms = t_begin + int(a.restart_after * 3_600_000) if a.restart_after else None
    wall0 = _real["time"]()
    connected = False
    done = False
    old_mod = old_channels = tracker = last_seq = down_until = None
    for f in files:
        cut = cutoff[sess_of[f]]
        with gzip.open(f, "rt") as fh:
            for line in fh:
                rec = loads(line)
                r = rec["r"]
                if r >= cut:
                    continue
                if r >= end_ms:
                    done = True; break
                SIM["ns"] = max(SIM["ns"], r * 1_000_000)
                if restart_ms and r >= restart_ms:
                    restart_ms = None
                    eng.recorder.close()                      # the old process stops: nothing is recorded while it is down
                    stats["restarts"] += 1
                    stats["before_restart"] = {"minutes": len(eng.runtime.core.hist), "positions": len(eng.runtime.broker.positions),
                                               "executions": len(eng.runtime.broker.executions)}
                    old_mod, old_channels = eng.mod, dict(feed.channels)
                    tracker = old_mod._ProtectedL3Book()      # Bitfinex's book keeps moving during the outage
                    tracker.orders = dict(eng.runtime.input.book.orders)
                    last_seq = eng.runtime.input.last_sequence
                    down_until = r + int(a.downtime_min * 60_000)
                    eng = feed = None
                if eng is None:
                    if "k" not in rec:
                        m = loads(rec["m"])
                        if isinstance(m, list) and len(m) >= 3:
                            body, seq = old_mod._BitfinexPublicFeed._split_sequence(m)
                            last_seq = seq if seq is not None else last_seq
                            if old_channels.get(body[0]) == "book" and isinstance(body[1], list) and body[1]:
                                if isinstance(body[1][0], list):
                                    tracker.apply_snapshot([[x[0], str(x[1]), str(x[2])] for x in body[1]], r)
                                else:
                                    tracker.apply_update([body[1][0], str(body[1][1]), str(body[1][2])], r)
                    if r < down_until:
                        continue
                    eng, feed = new_engine()                  # the new process: checkpoint + saved history + refill
                    core = eng.runtime.core
                    stats["after_restart"] = {"minutes": len(core.hist), "positions": len(eng.runtime.broker.positions),
                                              "executions": len(eng.runtime.broker.executions), "status": core.status,
                                              "gapfill_events": core.last_features.get("gapfill_events"),
                                              "history_restored_minutes": core.last_features.get("history_restored_minutes")}
                    feed.channels = dict(old_channels)        # it connects and receives a fresh snapshot of the book
                    eng.runtime.input.reset_connection(); eng.runtime.input.mark_connected()
                    for ch in set(old_channels.values()):
                        eng.runtime.input.mark_subscribed(ch)
                    book_id = next(k for k, v in old_channels.items() if v == "book")
                    rows = ",".join(f"[{o.order_id},{o.price},{o.amount}]" for o in tracker.orders.values())
                    feed._handle_raw(f"[{book_id},[{rows}],{last_seq}]", SIM["ns"])
                    connected = True
                    if "k" not in rec and isinstance(loads(rec["m"]), list):
                        continue                              # this message was already applied to the snapshot
                if "k" in rec:
                    if rec["k"] == "connect":
                        feed.channels.clear(); eng.runtime.input.reset_connection(); eng.runtime.input.mark_connected(); connected = True
                    continue
                if not connected:                             # a new process waits for the next connection (snapshot)
                    m = loads(rec["m"])
                    if isinstance(m, dict) and m.get("event") == "subscribed":
                        feed.channels.clear(); eng.runtime.input.reset_connection(); eng.runtime.input.mark_connected(); connected = True
                    else:
                        continue
                try:
                    feed._handle_raw(rec["m"], SIM["ns"])
                    stats["messages"] += 1
                except eng.mod.L3IntegrityError as exc:
                    stats["integrity_errors"].append(f"{r}: {exc}")
                    inp = eng.runtime.input
                    orders, seq, chans = dict(inp.book.orders), inp.last_sequence, dict(feed.channels)
                    inp.reset_connection(repr(exc)); connected = False
                    if any(x in str(exc) for x in ("pending_evidence_limit", "checksum_deadline_exceeded")) and orders and seq is not None:
                        # a checksum arrived late (> 5 s): the live feed reconnects and gets a fresh snapshot of the book
                        stats["reconnects"] += 1
                        inp.mark_connected(); feed.channels = chans
                        for ch in set(chans.values()):
                            inp.mark_subscribed(ch)
                        book_id = next(k for k, v in chans.items() if v == "book")
                        rows = ",".join(f"[{o.order_id},{o.price},{o.amount}]" for o in orders.values())
                        feed._handle_raw(f"[{book_id},[{rows}],{seq}]", SIM["ns"])
                        connected = True
        if done:
            break
        print(f"  {f.relative_to(a.raw)}  sim {_time.strftime('%m-%d %H:%M', _time.gmtime(SIM['ns']/1e9))}  wall {_real['time']()-wall0:.0f}s  "
              f"minutes {len(eng.runtime.core.hist)}  positions {len(eng.runtime.broker.positions)}  executions {len(eng.runtime.broker.executions)}", flush=True)
    eng.recorder.close()
    rt = eng.runtime
    from think.replay import pair_trades
    trades = pair_trades([x.as_dict() for x in rt.broker.executions])
    by = {}
    for t in trades:
        g = by.setdefault(t["setup"], {"n": 0, "net_usd": 0.0, "wins": 0, "bps": []})
        g["n"] += 1; g["net_usd"] += t["net"]; g["wins"] += t["net"] > 0; g["bps"].append(t["net_bps"])
    for g in by.values():
        g["avg_net_bps"] = sum(g["bps"]) / len(g["bps"]); g.pop("bps")
    signals = list(rt.signals)
    max_open = 0
    opened = set()
    for x in rt.broker.executions:
        if x.action.startswith("ENTER"): opened.add(x.position_id)
        elif x.action == "EXIT": opened.discard(x.position_id)
        max_open = max(max_open, len(opened))
    log_lines = sum(len(p.read_text().splitlines()) for p in (runtime / "aitest_predictions").glob("*.jsonl")) if (runtime / "aitest_predictions").is_dir() else 0
    summary = {"runtime_dir": str(runtime), "sim_from": _time.strftime("%Y-%m-%d %H:%M", _time.gmtime(t_begin / 1000)),
               "sim_to": _time.strftime("%Y-%m-%d %H:%M", _time.gmtime(SIM["ns"] / 1e9)), "wall_seconds": round(_real["time"]() - wall0),
               "messages": stats["messages"], "integrity_errors": stats["integrity_errors"][:10], "n_integrity_errors": len(stats["integrity_errors"]),
               "emulated_reconnects": stats["reconnects"], "health": rt.health()["state"], "checksums_verified": rt.input.checksum_count, "checksum_mismatches": rt.input.checksum_mismatches,
               "minutes_in_history": len(rt.core.hist), "status": rt.core.status, "model_error": rt.core.model_error,
               "executions": len(rt.broker.executions), "closed_trades": len(trades), "open_positions": len(rt.broker.positions),
               "max_concurrent_positions": max_open, "by_horizon": by, "metrics": rt.get_metrics(),
               "prediction_log_lines": log_lines, "restarts": stats["restarts"], "before_restart": stats.get("before_restart"),
               "after_restart": stats.get("after_restart"), "last_decision": signals[-1]["decision"]["reason"] if signals else None}
    (out / "smoke_summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps(summary, indent=1, default=str))


if __name__ == "__main__":
    main()
