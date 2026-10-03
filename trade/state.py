"""Durable paper-account checkpoint and execution journal.

Market evidence is intentionally never restored after a restart.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path


class RecoveryError(RuntimeError):
    pass


def identity(source: Path, contract: str) -> dict:
    return {"schema": 1, "contract": contract,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}


def checkpoint(store, runtime, expected: dict) -> None:
    broker, core = runtime.broker, runtime.core
    data = {
        "identity": expected,
        "broker": {
            "starting_equity_usd": broker.starting_equity_usd,
            "realized_pnl_usd": broker.realized_pnl_usd,
            "positions": {k: asdict(v) for k, v in broker.positions.items()},
            "entry_fees": broker.entry_fees,
            "executions": [asdict(x) for x in broker.executions],
            "trade_pnls": broker.trade_pnls,
            "peak_equity": broker.peak_equity,
            "max_drawdown_pct": broker.max_drawdown_pct,
        },
        "core": {
            "cooldown_until_ms": core.cooldown_until_ms,
            "position_state": core.position_state,
        },
    }
    # JSON serialization rejects non-finite values before the transaction.
    encoded = json.dumps(data, allow_nan=False, sort_keys=True)
    with store.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS trade_journal(id INTEGER PRIMARY KEY, data_json TEXT NOT NULL)")
        last_id = db.execute("SELECT COALESCE(MAX(id),0) FROM trade_journal").fetchone()[0]
        for index, rec in enumerate(broker.executions[last_id:], last_id + 1):
            db.execute("INSERT OR IGNORE INTO trade_journal(id,data_json) VALUES(?,?)",
                       (index, json.dumps(asdict(rec), allow_nan=False, sort_keys=True)))
        db.execute("INSERT INTO kv(scope,key,value_json) VALUES('trade_state','checkpoint',?) "
                   "ON CONFLICT(scope,key) DO UPDATE SET value_json=excluded.value_json", (encoded,))


def restore(store, runtime, expected: dict, module) -> bool:
    with store.connect() as db:
        row = db.execute("SELECT value_json FROM kv WHERE scope='trade_state' AND key='checkpoint'").fetchone()
        if row is None:
            return False
        try:
            data = json.loads(row[0])
            if data.get("identity") != expected:
                # A new Trade version (e.g. an approved promotion) starts a fresh paper
                # account; the old account and its journal are archived, not lost.
                # Only when flat: an open position must never be silently dropped.
                if data.get("broker", {}).get("positions"):
                    raise RecoveryError("checkpoint identity or schema mismatch with an open position")
                db.execute("CREATE TABLE IF NOT EXISTS trade_journal(id INTEGER PRIMARY KEY, data_json TEXT NOT NULL)")
                journal = [raw for _, raw in db.execute("SELECT id,data_json FROM trade_journal ORDER BY id").fetchall()]
                key = f"{str(data.get('identity', {}).get('source_sha256', 'unknown'))[:12]}_{int(time.time())}"
                db.execute("INSERT OR REPLACE INTO kv(scope,key,value_json) VALUES('trade_state_archive',?,?)",
                           (key, json.dumps({"checkpoint": data, "journal": journal})))
                db.execute("DELETE FROM trade_journal")
                db.execute("DELETE FROM kv WHERE scope='trade_state' AND key='checkpoint'")
                return False
            b = data["broker"]
            if float(b["starting_equity_usd"]) != runtime.broker.starting_equity_usd:
                raise RecoveryError("starting balance mismatch")
            positions = {k: module.PositionState(**v) for k, v in b["positions"].items()}
            for symbol, position in positions.items():
                if (symbol != (getattr(position, "position_id", None) or position.symbol) or position.side not in {"long", "short"} or
                    not math.isfinite(float(position.size)) or position.size <= 0 or
                    not math.isfinite(float(position.entry_price)) or position.entry_price <= 0):
                    raise RecoveryError("invalid saved position")
            executions = [module.ExecutionRecord(**v) for v in b["executions"]]
            rows = db.execute("SELECT id,data_json FROM trade_journal ORDER BY id").fetchall()
            if len(rows) != len(executions) or any(i != n or json.loads(raw) != asdict(executions[n-1]) for n, (i, raw) in enumerate(rows, 1)):
                raise RecoveryError("execution journal/checkpoint mismatch")
            if any(not math.isfinite(float(x)) for x in [b["realized_pnl_usd"], b["peak_equity"], b["max_drawdown_pct"], *b["entry_fees"].values(), *b["trade_pnls"]]):
                raise RecoveryError("nonfinite accounting value")
            broker = runtime.broker
            broker.positions = positions
            broker.entry_fees = {k: float(v) for k, v in b["entry_fees"].items()}
            broker.executions = executions
            broker.trade_pnls = [float(v) for v in b["trade_pnls"]]
            broker.realized_pnl_usd = float(b["realized_pnl_usd"])
            broker.peak_equity = float(b["peak_equity"])
            broker.max_drawdown_pct = float(b["max_drawdown_pct"])
            runtime.core.cooldown_until_ms = int(data["core"]["cooldown_until_ms"])
            runtime.core.position_state = data["core"]["position_state"]
            return True
        except RecoveryError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError, sqlite3.DatabaseError) as exc:
            raise RecoveryError(f"invalid checkpoint: {exc}") from exc
