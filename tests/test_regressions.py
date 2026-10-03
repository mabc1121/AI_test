from __future__ import annotations

import json
import os
import tempfile
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def module(name="regression_trade"):
    from core.common import import_module
    return import_module(ROOT / "tests" / "fixtures" / "reference_trade.py", name)   # fixed reference strategy


def verified_runtime(name="regression_verified"):
    m = module(name)
    runtime = m.create_trade_system()
    runtime.initialize({})
    runtime.input.mark_connected()
    runtime.input.mark_subscribed("book")
    runtime.input.mark_subscribed("trades")
    now = time.time_ns()
    runtime.on_l3_snapshot([[1, 100.0, 1.0], [2, 101.0, -1.0]], 1, now)
    runtime.on_l3_checksum(runtime.input.book.checksum(), 2, now + 1_000_000)
    return m, runtime


def test_missing_sequence_and_crossed_quote_fail_closed():
    m = module("sequence_regression")
    runtime = m.create_trade_system()
    runtime.input.mark_connected(); runtime.input.mark_subscribed("book"); runtime.input.mark_subscribed("trades")
    try:
        runtime.on_l3_snapshot([[1, 100, 1], [2, 101, -1]], None, time.time_ns())
    except m.L3IntegrityError: pass
    else: raise AssertionError("missing enabled sequence accepted")
    runtime.input.reset_connection(); runtime.input.mark_connected(); runtime.input.mark_subscribed("book"); runtime.input.mark_subscribed("trades")
    runtime.on_l3_snapshot([[1, 102, 1], [2, 101, -1]], 1, time.time_ns())
    assert not runtime.input.integrity().valid
    try:
        runtime.on_l3_checksum(runtime.input.book.checksum(), 2, time.time_ns())
    except m.L3IntegrityError: pass
    else: raise AssertionError("crossed book accepted")


def test_history_coverage_and_reconnect_reset():
    m = module("history_regression")
    core = m.ScientificCore()
    now = int(time.time() * 1000)
    core.first_event_ms = now - 900_000
    core.price_history = deque(((now - 400_000 + i * 10, 100.0) for i in range(40_000)), maxlen=40_000)
    assert not core._warm(now)
    core.pending_breakout = {"side": "up", "start_ms": now - 60_000}
    _, runtime = verified_runtime("reconnect_regression")
    runtime.core.pending_breakout = core.pending_breakout
    runtime.input.reset_connection()
    runtime.input.mark_connected(); runtime.input.mark_subscribed("book"); runtime.input.mark_subscribed("trades")
    fresh = time.time_ns()
    runtime.on_l3_snapshot([[1, 100, 1], [2, 101, -1]], 1, fresh)
    runtime.on_l3_checksum(runtime.input.book.checksum(), 2, fresh + 1_000_000)
    assert runtime.core.pending_breakout is None and not runtime.core._warm(fresh // 1_000_000)


def test_out_of_order_trades_and_refill_consumption():
    m = module("flow_regression")
    core = m.ScientificCore()
    now = int(time.time() * 1000)
    core._record_trade([1, now - 1000, 0.4, 100], now)
    core._record_trade([2, now - 5000, -0.4, 100], now)
    core._record_trade([3, now - 500, 0.6, 100], now)
    ratio, count, quantity = core._trade_flow(now, 2)
    assert count == 2 and abs(quantity - 1.0) < 1e-9 and abs(ratio - 1.0) < 1e-9
    core._record_lifecycle(now, {"kind": "remove", "side": "bid", "price": 100, "qty": 1})
    for _ in range(3): core._record_lifecycle(now, {"kind": "add", "side": "bid", "price": 100, "qty": 1})
    assert sum(x[2] for x in core.refill_events) == 1


def test_entry_fee_and_durable_restart():
    from trade.worker import TradeEngine
    from trade.state import RecoveryError
    from core.common import Store
    with tempfile.TemporaryDirectory() as temp:
        old = os.environ.get("APP_RUNTIME_DIR")
        os.environ["APP_RUNTIME_DIR"] = temp
        try:
            engine = TradeEngine(ROOT)
            m, runtime = verified_runtime("fee_regression")
            event = runtime.last_event
            decision = m.TradeDecision(action="ENTER_LONG", symbol=event.symbol, requested_size=0.1, reason="test")
            record = engine.runtime.broker.execute(decision, event)
            assert record and record.fee_usd > 0
            engine.runtime.core.position_state[event.symbol] = {"runner": True, "peak_net_bps": 7.0}
            engine.runtime.core.cooldown_until_ms = event.recv_time_ms + 1000
            engine.runtime.persist_callback(engine.runtime)
            expected = 10_000 + 0.1 * (event.best_bid - record.fill_price) - record.fee_usd
            assert abs(engine.runtime.broker.equity(event) - expected) < 1e-8
            recovered = TradeEngine(ROOT)
            assert len(recovered.runtime.broker.positions) == 1
            assert recovered.runtime.broker.entry_fees == engine.runtime.broker.entry_fees
            assert recovered.runtime.core.position_state[event.symbol]["runner"] is True
            assert recovered.runtime.core.first_event_ms is None
            assert not recovered.runtime.input.integrity().valid
            store = Store(Path(temp) / "platform.sqlite3")
            with store.connect() as db: db.execute("DELETE FROM trade_journal")
            try: TradeEngine(ROOT)
            except RecoveryError: pass
            else: raise AssertionError("journal corruption accepted")
        finally:
            if old is None: os.environ.pop("APP_RUNTIME_DIR", None)
            else: os.environ["APP_RUNTIME_DIR"] = old


def test_stale_pending_batch_fails_closed():
    m = module("stale_regression")
    runtime = m.create_trade_system()
    runtime.input.mark_connected(); runtime.input.mark_subscribed("book"); runtime.input.mark_subscribed("trades")
    old = time.time_ns() - 6_000_000_000
    runtime.on_l3_snapshot([[1, 100, 1], [2, 101, -1]], 1, old)
    try: runtime.on_l3_checksum(runtime.input.book.checksum(), 2, time.time_ns())
    except m.L3IntegrityError: pass
    else: raise AssertionError("stale checksum batch accepted")


def test_doctor_preserves_active_database():
    import subprocess
    import sys
    from core.common import Store
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "platform.sqlite3"
        store = Store(path)
        store.put("trade_state", "sentinel", {"balance": 123})
        before = store.get("trade_state", "sentinel")
        env = os.environ.copy(); env["APP_RUNTIME_DIR"] = temp
        proc = subprocess.run([sys.executable, "-m", "core.doctor", "--full"], cwd=ROOT, env=env,
                              text=True, capture_output=True, timeout=30)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert store.get("trade_state", "sentinel") == before
        assert not (Path(temp) / "think").exists()


def test_manage_git_commit_without_user_identity():
    """Installed apps run as a system user with no git identity; approved edits must still be committed."""
    import subprocess
    from manage.manage import git_commit
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        env = {**os.environ, "HOME": temp, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, env=env)
        (root / "a.txt").write_text("x\n")
        saved = {k: os.environ.get(k) for k in ("GIT_CONFIG_NOSYSTEM", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL")}
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"                     # like the server: no git identity anywhere
        for k in ("GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"): os.environ.pop(k, None)
        try:
            result = git_commit(root, ["a.txt"], "test commit", "Manage <manage@tt-trade.local>")
        finally:
            for k, v in saved.items():
                if v is None: os.environ.pop(k, None)
                else: os.environ[k] = v
        assert result["committed"], result
