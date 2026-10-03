"""Scenario test of the example breakout strategy with the protected paper broker (no network).
Run: python examples/breakout/scenario_test.py   - a model for testing any new strategy before installing it."""
import importlib.util, json, math, pathlib, sys
spec = importlib.util.spec_from_file_location("bo", pathlib.Path(__file__).with_name("trade.py")); m = importlib.util.module_from_spec(spec)
sys.modules["bo"] = m; spec.loader.exec_module(m)

INT = m.MarketIntegrity(True, True, True, True, True, False, 1, 0, 1, 0, 1, 0, 0)
T0 = 1_790_000_000_000; seq = [0]
def ev(t_ms, mid, kind="l3_update", payload=None, spread_usd=2.0):
    seq[0] += 1
    return m.CleanMarketEvent(kind, "sim", "tBTCUSD", "book", 1, seq[0], t_ms * 1_000_000, t_ms, t_ms, payload, None, {},
                              mid - spread_usd / 2, mid + spread_usd / 2, mid, spread_usd / mid * 1e4, INT)
core = m.ScientificCore(); broker = m._ProtectedPaperBroker(10_000.0); log = []; tid = [1000]
def step(t, mid, trades=()):
    for amt in trades:                           # public trades first (as history), then the book tick decides
        tid[0] += 1; core._apply_event(ev(t, mid, "public_trade", [tid[0], t, amt, mid]))
    e = ev(t, mid); core.config.assumed_equity_usd = broker.equity(e)
    d = core.decide(e, list(broker.positions.values())); x = broker.execute(d, e)
    if x: log.append((round((t - T0) / 60000, 1), x.action, round(x.fill_price, 2), x.reason, round(x.realized_pnl, 2)))
    return d

core._apply_event(ev(T0, 84100.0, "l3_snapshot", []))
reasons = {}
# 70 min sideways between 84000 and 84200 (~24 bps), a small two-sided trade every 10 s
for s in range(0, 70 * 60, 5):
    t = T0 + s * 1000; mid = 84100 + 100 * math.sin(s / 300)
    d = step(t, mid, (0.01 if s % 20 else -0.01,) if s % 10 == 0 else ())
    reasons[d.reason] = reasons.get(d.reason, 0) + 1
print("range phase reasons:", reasons)
assert "warming_up" in reasons and not log, "no trade may happen during warm-up or inside the range"

# breakout: price clears 84200 by > 2 bps with heavy buying
t = T0 + 70 * 60_000
d = step(t, 84235.0, (0.3, 0.4, 0.3, -0.05)); print("breakout decision:", d.action, d.reason, d.requested_size, "stop", round(d.stop_price or 0, 2))
assert d.action == "ENTER_LONG" and broker.positions, d
# a small rise that stays below the trail start (+25 bps) must not trail; then rally +~40 bps and pull back: trailing stop must exit in profit
for i, mid in enumerate([84260, 84300, 84350, 84420, 84500, 84550, 84530, 84500, 84480, 84460, 84440]):
    d = step(t + (i + 1) * 5000, float(mid))
print("trades:", log)
assert log[-1][1] == "EXIT" and log[-1][3] == "trailing_stop" and log[-1][4] > 0, log
assert not core.position_state, core.position_state
json.dumps({"core": {"cooldown_until_ms": core.cooldown_until_ms, "position_state": core.position_state}})   # checkpoint must be JSON-safe

# a second breakout DOWN but with BUYING -> flow against, no entry. Continuous data (a gap would restart the warm-up).
t2 = t + 60_000
for s in range(0, 45 * 60, 5):
    step(t2 + s * 1000, 84400 + 80 * math.sin(s / 300), (0.01 if s % 20 else -0.01,) if s % 10 == 0 else ())
d = step(t2 + 45 * 60_000, 84250.0, (0.3, 0.3, 0.3)); print("down break with buying:", d.action, d.reason, "down_bps", round(core.last_features["down_bps"], 1))
assert d.action == "HOLD" and d.reason == "breakout_flow_against", d.reason
d = step(t2 + 45 * 60_000 + 5000, 84245.0, (-0.4, -0.5, -0.4)); print("down break with selling:", d.action, d.reason)
assert d.action == "ENTER_SHORT", d.reason
print("ALL SCENARIOS PASSED; metrics:", {k: v for k, v in broker.metrics().as_dict().items() if k in ("net_pnl_usd", "closed_trades", "accounting_valid")})
