"""Scenario test for a strategy built from the starter (no network): fake but realistic market events through the
strategy and the protected paper broker. Run: python examples/starter/scenario_test.py [path/to/trade.py]

Copy this next to your strategy and add your own scenario at the bottom (e.g. the market pattern your entry rule
is meant to catch, and one it must ignore)."""
import importlib.util, json, math, pathlib, sys

def load(path):
    spec = importlib.util.spec_from_file_location("strategy_under_test", path); m = importlib.util.module_from_spec(spec)
    sys.modules["strategy_under_test"] = m; spec.loader.exec_module(m); return m

class Market:
    """Feeds events the way the runtime does: trades and book changes as history, then a current event that decides."""
    def __init__(self, m, t0=1_790_000_000_000):
        self.m, self.t, self.seq, self.tid = m, t0, 0, 1000
        self.core = m.ScientificCore(); self.broker = m._ProtectedPaperBroker(10_000.0); self.fills = []
        self.ok = m.MarketIntegrity(True, True, True, True, True, False, 1, 0, 1, 0, 1, 0, 0)
        self.core._apply_event(self.ev("l3_snapshot", [[1, 84099.0, 0.5], [2, 84101.0, -0.5]], 84100.0))
    def ev(self, kind, payload, mid, spread_usd=2.0):
        self.seq += 1
        return self.m.CleanMarketEvent(kind, "test", "tBTCUSD", "book", 1, self.seq, self.t * 1_000_000, self.t, self.t, payload, None, {},
                                       mid - spread_usd / 2, mid + spread_usd / 2, mid, spread_usd / mid * 1e4, self.ok)
    def step(self, seconds, mid, trades=()):
        self.t += int(seconds * 1000)
        for amount in trades:
            self.tid += 1; self.core._apply_event(self.ev("public_trade", [self.tid, self.t, amount, mid], mid))
        self.core._apply_event(self.ev("l3_update", [3, mid - 1.5, 0.2], mid))
        e = self.ev("l3_update", [4, mid + 1.5, -0.2], mid); self.core.config.assumed_equity_usd = self.broker.equity(e)
        d = self.core.decide(e, list(self.broker.positions.values())); x = self.broker.execute(d, e)
        if x: self.fills.append((x.action, round(x.fill_price, 2), x.reason, round(x.realized_pnl, 2)))
        return d
    def wander(self, minutes, center=84100.0, amp=60.0):
        for s in range(0, int(minutes * 60), 5):
            self.step(5, center + amp * math.sin(s / 300), (0.01 if s % 20 else -0.01,) if s % 10 == 0 else ())

def test_never_trades_as_shipped(path):
    mk = Market(load(path)); reasons = {}
    for s in range(0, 3 * 3600, 5):
        d = mk.step(5, 84100 + 80 * math.sin(s / 400), (0.02 if s % 15 else -0.03,) if s % 10 == 0 else ())
        reasons[d.reason] = reasons.get(d.reason, 0) + 1
    assert "warming_up" in reasons and not mk.fills, (reasons, mk.fills)
    return reasons

def _forced(path, **config):
    """A strategy from the starter whose entry rule goes long once - to exercise the ready-made RISK exits."""
    mk = Market(load(path))
    for k, v in config.items(): setattr(mk.core.config, k, v)
    mk.armed = False
    mk.core.entry_signal = lambda f: ("long", "test_entry") if mk.armed and not mk.fills else (None, "not_armed")
    mk.wander(62)                                  # warm-up: 60 min volume baseline + window
    assert not mk.fills, mk.fills
    mk.armed = True; d = mk.step(5, 84100.0); assert d.action == "ENTER_LONG", d
    return mk

def test_risk_exits(path):
    mk = _forced(path, stop_bps=15.0)
    for mid in (84080, 84050, 84000, 83960):
        mk.step(5, float(mid))
    assert mk.fills[-1][0] == "EXIT" and mk.fills[-1][2] == "stop_loss" and mk.fills[-1][3] < 0, mk.fills
    d = mk.step(5, 83960.0); assert d.reason == "cooldown", d.reason

    mk = _forced(path, stop_bps=15.0, target_bps=30.0)
    for mid in (84150, 84250, 84400):
        mk.step(5, float(mid))
    assert mk.fills[-1][2] == "take_profit" and mk.fills[-1][3] > 0, mk.fills

    mk = _forced(path, stop_bps=15.0, trail_activate_bps=25.0, trail_bps=10.0)
    for mid in (84200, 84350, 84500, 84480, 84440, 84400, 84380, 84360):
        mk.step(5, float(mid))
    assert mk.fills[-1][2] == "trailing_stop" and mk.fills[-1][3] > 0, mk.fills     # a trailing exit is always a net gain

    mk = _forced(path, stop_bps=50.0, max_hold_seconds=120)
    for _ in range(30):
        mk.step(5, 84110.0)
    assert mk.fills[-1][2] == "maximum_hold", mk.fills
    json.dumps({"position_state": mk.core.position_state, "cooldown_until_ms": mk.core.cooldown_until_ms})   # checkpoint-safe
    return "stop_loss, take_profit, trailing_stop, maximum_hold, cooldown ok"

if __name__ == "__main__":
    path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).with_name("trade.py")).resolve()
    print("as shipped:", test_never_trades_as_shipped(path))
    print("risk exits:", test_risk_exits(path))
    # TODO: your scenario, e.g. build a Market, feed the pattern your entry rule should catch, assert the ENTER decision.
    print("ALL SCENARIOS PASSED")
