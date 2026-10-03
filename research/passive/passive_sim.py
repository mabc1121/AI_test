#!/usr/bin/env python3
"""
passive_sim.py — passive (maker) execution of the 1-minute book-imbalance signal, simulated on 1-second L2 rows.

Question: does the ~0.5 bps/minute imbalance edge survive being FILLED passively, at zero fees?

Strategy (long side; short is symmetric)
  * at the end of second t, if flat and the signal s_t >= THETA (bid-heavy book), post a BUY limit of size Q at the
    best bid B = bid1_t, joining the queue behind bsz1_t; cancel if not filled within T_ENTRY seconds.
  * fill model for a resting BUY at B (evaluated each later second u; our own order is not in the recorded book):
      - a trade printed below our price (px_min_u <= B - tick)                       -> filled at B
      - best bid still at B and cumulative aggressive SELL volume >= queue ahead + Q  -> filled at B
      - best bid moved below B (level gone) AND an aggressive sell occurred this second -> filled at B
        (we would have been the best bid; a vanished level with no sells is treated as cancellations, we keep waiting)
      - best bid above B: we are behind the touch, nothing is consumed, we wait
  * exit (EXIT_MODE):
      "target": on a fill at second u, rest a SELL at entry * (1 + TP_BPS/1e4) with the mirrored fill model, giving the
                signal its horizon; if not filled within T_EXIT seconds, exit at market at bid1_v minus SLIP_BPS.
      "touch":  rest a SELL at the best ask at fill time (pure spread capture), same expiry and market fallback.
  * one position at a time; positions are flattened at market at the end of each day.
  * baseline: the same mechanics with a random side every time we are flat (no signal) -> pure passive-execution P&L.

PRE-REGISTERED PRIMARY CELL (fixed before the 92-day run; the two sample days were used only to debug mechanics):
  signal = depth imbalance within 0.5 bps (dimb05), THETA = 0.5, T_ENTRY = 30 s, EXIT_MODE = "target",
  TP_BPS = 0.5, T_EXIT = 60 s, Q = 0.01 BTC, SLIP_BPS = 0.5 on market legs, zero fees.
  Pass requires: mean net per round trip > 0, t-stat of daily P&L >= 3, positive in >= 10 of 13 weeks, and net per
  round trip at least 0.2 bps above the random-side baseline with the same parameters. Everything else is secondary.

usage: python passive_sim.py --glob "data/bybit_1s/*.parquet" [--out out_passive] [--max-days N]
"""
import argparse
import glob
import math
import os
import time

import numpy as np
import pandas as pd

TICK = 0.1   # overridden by --tick
PRIMARY = dict(signal="dimb05", theta=0.5, t_entry=30, exit="target", tp=0.5, t_exit=60, q=0.01, slip=0.5)
GRID = [dict(signal=s, theta=th, t_entry=te, exit=ex, tp=tp, t_exit=tx, q=0.01, slip=0.5)
        for s in ("dimb05", "bimb") for th in (0.3, 0.5, 0.7) for te in (10, 30, 60)
        for ex, tp in (("target", 0.5), ("target", 1.0), ("touch", 0.0)) for tx in (60, 120)]
SECONDARY_Q = [dict(PRIMARY, q=0.1), dict(PRIMARY, slip=1.0), dict(PRIMARY, slip=0.0), dict(PRIMARY, exit="touch", tp=0.0)]


def load_day(path):
    d = pd.read_parquet(path)
    if "sec" in d.columns:
        d = d.set_index("sec")
    d = d.sort_index()
    bid, ask = d["bid1"].to_numpy(float), d["ask1"].to_numpy(float)
    bsz, asz = d["bsz1"].to_numpy(float), d["asz1"].to_numpy(float)
    out = {
        "sec": d.index.to_numpy().astype(np.int64), "bid": bid, "ask": ask, "bsz": bsz, "asz": asz,
        "buy": np.nan_to_num(d["buy_vol"].to_numpy(float)), "sell": np.nan_to_num(d["sell_vol"].to_numpy(float)),
        "pxmin": d["px_min"].to_numpy(float), "pxmax": d["px_max"].to_numpy(float),
        "mid": (bid + ask) / 2,
        "bimb": (bsz - asz) / np.maximum(bsz + asz, 1e-9),
        "dimb05": (d["bd0.5"].to_numpy(float) - d["ad0.5"].to_numpy(float)) / np.maximum(d["bd0.5"].to_numpy(float) + d["ad0.5"].to_numpy(float), 1e-9),
    }
    return out


def simulate_day(D, p, rng=None):
    """returns list of round trips for one day: (entry_sec, side, entry_px, exit_px, exit_sec, exit_kind, mid_at_entry, mid_60s)"""
    n = len(D["sec"])
    sig = D[p["signal"]] if rng is None else None
    bid, ask, bsz, asz, buy, sell, pxmin, pxmax, mid = (D[k] for k in ("bid", "ask", "bsz", "asz", "buy", "sell", "pxmin", "pxmax", "mid"))
    th, te, tx, q, slip = p["theta"], p["t_entry"], p["t_exit"], p["q"], p["slip"]
    exit_mode, tp = p.get("exit", "target"), p.get("tp", 0.5)

    def buy_filled(u, px, st):
        """resting BUY at px; st = [consumed]. Returns True if filled during second u."""
        if np.isfinite(pxmin[u]) and pxmin[u] <= px - TICK:
            return True
        if bid[u] == px:
            st[0] += sell[u]
            return st[0] >= queue_of(st) + q
        if bid[u] < px:
            return sell[u] > 0            # we would be the best bid; any aggressive sell hits us
        return False                       # bid above us: behind the touch

    def sell_filled(u, px, st):
        if np.isfinite(pxmax[u]) and pxmax[u] >= px + TICK:
            return True
        if ask[u] == px:
            st[0] += buy[u]
            return st[0] >= queue_of(st) + q
        if ask[u] > px:
            return buy[u] > 0
        return False

    def queue_of(st):
        return st[1]

    trips = []
    state = "flat"              # flat | entry | exit
    t = 0
    while t < n - 1:
        if state == "flat":
            if rng is not None:
                side = 1 if rng.random() < 0.5 else -1
            else:
                s = sig[t]
                side = 1 if s >= th else (-1 if s <= -th else 0)
            if side == 0 or not np.isfinite(bid[t]):
                t += 1
                continue
            px = bid[t] if side > 0 else ask[t]
            st = [0.0, bsz[t] if side > 0 else asz[t]]        # [consumed, queue ahead]
            expiry = t + te
            state = "entry"
            t += 1
            continue
        if state == "entry":
            u = t
            filled = buy_filled(u, px, st) if side > 0 else sell_filled(u, px, st)
            if filled:
                entry_px, entry_sec, m0 = px, u, mid[u]
                if exit_mode == "touch":
                    xpx = ask[u] if side > 0 else bid[u]
                else:
                    xpx = round((entry_px * (1 + side * tp / 1e4)) / TICK) * TICK
                    if side > 0:
                        xpx = max(xpx, ask[u])                 # never rest a sell below the current ask
                    else:
                        xpx = min(xpx, bid[u])
                xst = [0.0, (asz[u] if side > 0 else bsz[u]) if (xpx == (ask[u] if side > 0 else bid[u])) else 0.0]
                xexpiry = u + tx
                state = "exit"
            elif u >= expiry:
                state = "flat"
            t += 1
            continue
        if state == "exit":
            v = t
            done, kind, out_px = False, None, None
            if (sell_filled(v, xpx, xst) if side > 0 else buy_filled(v, xpx, xst)):
                done, kind, out_px = True, "passive", xpx
            if not done and (v >= xexpiry or v >= n - 2):
                done, kind = True, "market"
                out_px = bid[v] * (1 - slip / 1e4) if side > 0 else ask[v] * (1 + slip / 1e4)
            if done:
                m60 = mid[min(entry_sec + 60, n - 1)]
                trips.append((entry_sec, side, entry_px, out_px, v, kind, m0, m60))
                state = "flat"
            t += 1
            continue
    return trips


def run(files, params, label, rng_seed=None):
    rows = []
    for f in files:
        D = load_day(f)
        rng = np.random.default_rng(rng_seed) if rng_seed is not None else None
        for (es, side, ep, xp, xs, kind, m0, m60) in simulate_day(D, params, rng):
            pnl = side * (xp - ep) / ep * 1e4
            rows.append({"day": os.path.basename(f)[:10], "entry_sec": int(D["sec"][es]), "side": side, "entry_px": ep,
                         "exit_px": xp, "hold_s": int(D["sec"][xs] - D["sec"][es]), "exit": kind, "pnl_bps": pnl,
                         "adverse_bps": side * (m0 - ep) / ep * 1e4,           # mid vs our fill price at fill time
                         "mid_move_60s_bps": side * (m60 - m0) / m0 * 1e4})    # signal realisation after the fill
    T = pd.DataFrame(rows)
    T["label"] = label
    return T


def summarize(T, label, n_days):
    if T.empty:
        return {"label": label, "round_trips": 0}
    daily = T.groupby("day")["pnl_bps"].sum()
    wk = T.assign(week=pd.to_datetime(T["day"]).dt.to_period("W")).groupby("week")["pnl_bps"].sum()
    t = daily.mean() / (daily.std(ddof=1) / math.sqrt(len(daily))) if len(daily) > 2 and daily.std(ddof=1) > 0 else np.nan
    return {"label": label, "round_trips": len(T), "per_day": len(T) / n_days, "mean_pnl_bps": T["pnl_bps"].mean(),
            "median_pnl_bps": T["pnl_bps"].median(), "passive_exit_share": (T["exit"] == "passive").mean(),
            "pnl_passive_exits": T.loc[T["exit"] == "passive", "pnl_bps"].mean(),
            "pnl_market_exits": T.loc[T["exit"] == "market", "pnl_bps"].mean(), "hold_s_median": T["hold_s"].median(),
            "adverse_at_fill_bps": T["adverse_bps"].mean(), "mid_move_60s_bps": T["mid_move_60s_bps"].mean(),
            "daily_pnl_mean_bps": daily.mean(), "t_daily": t, "weeks_pos": f"{int((wk > 0).sum())}/{len(wk)}",
            "days_pos": f"{int((daily > 0).sum())}/{len(daily)}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="data/bybit_1s/*.parquet")
    ap.add_argument("--out", default="out_passive")
    ap.add_argument("--max-days", type=int, default=None)
    ap.add_argument("--quick", action="store_true", help="primary cell + baseline only")
    ap.add_argument("--tick", type=float, default=0.1, help="price tick: 0.1 Bybit BTCUSDT, 1.0 Bitfinex tBTCUSD")
    a = ap.parse_args()
    global TICK
    TICK = a.tick
    files = sorted(glob.glob(a.glob))
    if a.max_days:
        files = files[: a.max_days]
    if not files:
        raise SystemExit(f"no files match {a.glob}")
    os.makedirs(a.out, exist_ok=True)
    n_days = len(files)
    t0 = time.time()
    results = []
    T = run(files, PRIMARY, "PRIMARY " + str(PRIMARY))
    T.to_csv(os.path.join(a.out, "passive_trades_primary.csv"), index=False)
    results.append(summarize(T, "PRIMARY " + str(PRIMARY), n_days))
    B = run(files, PRIMARY, "BASELINE random side " + str(PRIMARY), rng_seed=1)
    results.append(summarize(B, "BASELINE random side " + str(PRIMARY), n_days))
    print(f"primary + baseline done ({time.time()-t0:.0f}s)")
    if not a.quick:
        for p in SECONDARY_Q + GRID:
            if p == PRIMARY:
                continue
            results.append(summarize(run(files, p, str(p)), str(p), n_days))
        print(f"grid done ({time.time()-t0:.0f}s)")
    R = pd.DataFrame(results)
    R.to_csv(os.path.join(a.out, "passive_cells.csv"), index=False)
    pd.set_option("display.width", 250)
    cols = ["label", "round_trips", "per_day", "mean_pnl_bps", "median_pnl_bps", "passive_exit_share", "pnl_passive_exits",
            "pnl_market_exits", "hold_s_median", "adverse_at_fill_bps", "mid_move_60s_bps", "t_daily", "weeks_pos", "days_pos"]
    print(R[cols].round(3).to_string(index=False))
    P, Bs = results[0], results[1]
    ok = (P.get("round_trips", 0) > 0 and P["mean_pnl_bps"] > 0 and P["t_daily"] >= 3 and
          int(P["weeks_pos"].split("/")[0]) >= min(10, int(P["weeks_pos"].split("/")[1])) and
          P["mean_pnl_bps"] - Bs.get("mean_pnl_bps", 0) >= 0.2)
    print("\nPRIMARY VERDICT:", "PASS" if ok else "FAIL",
          f"| net per round trip {P.get('mean_pnl_bps', float('nan')):.3f} bps vs baseline {Bs.get('mean_pnl_bps', float('nan')):.3f}, "
          f"t {P.get('t_daily', float('nan')):.2f}, weeks {P.get('weeks_pos')}")


if __name__ == "__main__":
    main()
