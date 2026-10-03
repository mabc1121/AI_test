"""AI_test trade.py — contract 2.3 (AI_test): the tt_template trade file with an editable paper-truth layer.

WHAT THIS FILE IS
-----------------
AI_test runs the frozen Bybit-trained models of research/ on live Bitfinex data at four horizons (30, 60, 120,
240 minutes) as a paper-trading forward test. The strategy is the EDITABLE:SCIENTIFIC_WORKSPACE section; its model
arrays are in trade/aitest_models.json.gz (built by research/bitfinex/export_models.py).

WHAT THE PROTECTED BLOCKS PROVIDE (unchanged from the template unless noted)
--------------------------------------------------------------------------
- Real-time Bitfinex public WebSocket connection, or the tt_input market-data service.
- Raw R0 Level-3 book (individual ORDER_ID / PRICE / signed AMOUNT) and public trades.
- Sequence checking, individual-order reconstruction, CRC32 checksum verification, staleness/integrity state,
  fail-closed reconnect, and the checksum gate (unverified evidence never reaches the strategy).
- Paper accounting and the app runtime/UI contract.

CONTRACT 2.3 (AI_test) DIFFERS FROM 2.2 IN THREE WAYS
-----------------------------------------------------
1. Costs are settings: PAPER_DEFAULTS holds the fee and the slippage per side (Bitfinex: 0 fee, 0.5 bps slippage on
   top of crossing the real spread). Change them there; the Lab cannot (its tuner locks the cost mirrors).
2. Several positions at once: the paper broker keys positions by decision.metadata["position_id"] (default: the
   symbol, so single-position strategies behave as before). PositionState and ExecutionRecord carry position_id.
3. TradeRuntime.initialize() calls ScientificCore.on_runtime_initialize(context) when the strategy has one (the live
   app passes its project name; the strategy uses it to restore its saved history).
The protected-block fingerprints of 2.3 are in trade/validate_trade_contract.py. Editing a protected block is allowed
in this project: re-baseline the 2.3 fingerprints there in the same commit and run python -m tests.run_all.

HARD RULES THAT STILL HOLD
--------------------------
1. Paper only: keep paper_only=True; never add live-money order execution or credential use.
2. Never bypass sequence/checksum/staleness/reconnect checks or the checksum evidence gate.
3. If the strategy needs unavailable evidence, fail visibly/HOLD; never invent it.
4. Run run_contract_self_test() (python trade/trade.py) after any change.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict, dataclass, field
from decimal import Decimal
import inspect
import json
import logging
import math
import time
from typing import Any, Deque, Iterable, Optional
import zlib


# ============================================================================
# PROTECTED:CONTRACT BEGIN
# ============================================================================

TRADE_CONTRACT_VERSION = "2.3"

REQUIRED_RUNTIME_METHODS = (
    "initialize",
    "run_live_market_data",
    "on_l3_snapshot",
    "on_l3_update",
    "on_l3_checksum",
    "on_public_trade_snapshot",
    "on_public_trade",
    "get_status",
    "get_positions",
    "get_recent_signals",
    "get_recent_executions",
    "get_metrics",
    "health",
    "get_ui_schema",
    "get_ui_snapshot",
)

@dataclass(frozen=True)
class MarketIntegrity:
    valid: bool
    connected: bool
    book_subscribed: bool
    trades_subscribed: bool
    verified: bool
    stale: bool
    last_sequence: Optional[int]
    sequence_gaps: int
    checksum_count: int
    checksum_mismatches: int
    last_verified_checksum: Optional[int]
    last_update_ms: int
    pending_evidence: int
    error: Optional[str] = None

@dataclass(frozen=True)
class CleanMarketEvent:
    """Healthy Bitfinex public-market evidence delivered to editable science."""
    kind: str
    source: str
    symbol: str
    channel: str
    channel_id: Optional[int]
    sequence: Optional[int]
    recv_time_ns: int
    recv_time_ms: int
    exchange_time_ms: Optional[int]
    payload: Any
    raw_message: Optional[str]
    protected_context: dict[str, Any]
    best_bid: Optional[float]
    best_ask: Optional[float]
    mid: Optional[float]
    spread_bps: Optional[float]
    integrity: MarketIntegrity

# Backward-friendly alias for code that conceptually expects clean L3 events.
CleanL3Event = CleanMarketEvent

@dataclass
class TradeDecision:
    action: str = "HOLD"
    symbol: str = "tBTCUSD"
    requested_size: float = 0.0
    confidence: Optional[float] = None
    reason: str = "no_signal"
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    max_hold_seconds: Optional[int] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    def as_dict(self) -> dict[str, Any]: return asdict(self)

@dataclass
class PositionState:
    symbol: str
    side: str
    size: float
    entry_price: float
    entry_time_ms: int
    unrealized_pnl: float = 0.0
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    max_hold_seconds: Optional[int] = None
    strategy_tag: Optional[str] = None
    position_id: Optional[str] = None
    def as_dict(self) -> dict[str, Any]: return asdict(self)

@dataclass
class ExecutionRecord:
    action: str
    symbol: str
    side: str
    size: float
    fill_price: float
    fee_usd: float
    slippage_bps: float
    realized_pnl: float
    timestamp_ms: int
    reason: str
    position_id: Optional[str] = None
    def as_dict(self) -> dict[str, Any]: return asdict(self)

@dataclass
class PerformanceMetrics:
    starting_equity_usd: float
    equity_usd: float
    net_pnl_usd: float
    return_pct: float
    gross_profit_usd: float
    gross_loss_usd: float
    profit_factor: Optional[float]
    expectancy_usd: Optional[float]
    win_rate_pct: Optional[float]
    average_win_usd: Optional[float]
    average_loss_usd: Optional[float]
    max_drawdown_pct: float
    closed_trades: int
    executions: int
    accounting_valid: bool
    def as_dict(self) -> dict[str, Any]: return asdict(self)

@dataclass
class HealthStatus:
    ok: bool
    state: str
    message: str
    market_integrity: MarketIntegrity
    def as_dict(self) -> dict[str, Any]:
        data=asdict(self); data["market_integrity"]=asdict(self.market_integrity); return data

class TradeContractError(RuntimeError): pass
class L3IntegrityError(TradeContractError): pass

# ============================================================================
# PROTECTED:CONTRACT END
# ============================================================================


# ============================================================================
# EDITABLE:IDENTITY_AND_UI BEGIN
# AI/Think MAY edit values in this section, but must preserve dictionary shape.
# ============================================================================

TRADE_META: dict[str, Any] = {
    "contract_version": TRADE_CONTRACT_VERSION,
    "name": "AI_test Multi-Horizon Forward Test",
    "version": "1.0.0",
    "description": "BTCUSD long/short paper forward test of the frozen Bybit-trained models (research/) on live Bitfinex data: one slot per horizon (30/60/120/240 min) enters when the big-move probability and the direction score are both high relative to the Bybit training distribution, and exits at a symmetric volatility barrier or after the horizon. Every minute's predictions are logged for offline evaluation.",
    "symbols": ["tBTCUSD"],
    "paper_only": True,
    "expected_horizon": "minutes_to_4_hours",
    "supports_multi_symbol": False,
    "supports_multi_position": True,
}

TRADE_UI_SPEC: dict[str, Any] = {
    # Live numbers on the Trade page: every key here is in the decision metadata (ScientificCore._metadata).
    "custom_metrics": [
        {"key": "strategy_state", "label": "Strategy State", "format": "text"},
        {"key": "history_minutes", "label": "History (min)", "format": "number", "precision": 0},
        {"key": "open_positions", "label": "Open Positions", "format": "number", "precision": 0},
        {"key": "sigma1_bps", "label": "Sigma 1m (bps)", "format": "number", "precision": 2},
        {"key": "h30_p_hit", "label": "30m Big-Move Prob", "format": "number", "precision": 3},
        {"key": "h30_score", "label": "30m Direction Score", "format": "number", "precision": 3},
        {"key": "h60_p_hit", "label": "60m Big-Move Prob", "format": "number", "precision": 3},
        {"key": "h60_score", "label": "60m Direction Score", "format": "number", "precision": 3},
        {"key": "h120_p_hit", "label": "120m Big-Move Prob", "format": "number", "precision": 3},
        {"key": "h120_score", "label": "120m Direction Score", "format": "number", "precision": 3},
        {"key": "h240_p_hit", "label": "240m Big-Move Prob", "format": "number", "precision": 3},
        {"key": "h240_score", "label": "240m Direction Score", "format": "number", "precision": 3},
    ],
    # Settings editable on the Trade page (TradeConfig field names).
    "settings": [
        {"key": "base_size", "label": "BTC Size / Trade", "type": "number", "min": 0.001, "step": 0.001},
        {"key": "max_spread_bps", "label": "Max Spread (bps)", "type": "number", "min": 0.5, "max": 20.0, "step": 0.1},
        {"key": "hit_quantile", "label": "Big-Move Quantile", "type": "number", "min": 0.5, "max": 0.95, "step": 0.05},
        {"key": "score_quantile", "label": "Direction Quantile", "type": "number", "min": 0.5, "max": 0.95, "step": 0.05},
        {"key": "barrier_k", "label": "Barrier (x sigma sqrt H)", "type": "number", "min": 0.25, "max": 3.0, "step": 0.25},
        {"key": "max_positions_per_horizon", "label": "Max Positions / Horizon", "type": "number", "min": 1, "max": 60, "step": 1},
        {"key": "h30_enabled", "label": "Trade 30m (1/0)", "type": "number", "min": 0, "max": 1, "step": 1},
        {"key": "h60_enabled", "label": "Trade 60m (1/0)", "type": "number", "min": 0, "max": 1, "step": 1},
        {"key": "h120_enabled", "label": "Trade 120m (1/0)", "type": "number", "min": 0, "max": 1, "step": 1},
        {"key": "h240_enabled", "label": "Trade 240m (1/0)", "type": "number", "min": 0, "max": 1, "step": 1},
        {"key": "min_history_minutes", "label": "Min History (min)", "type": "number", "min": 60, "max": 1440, "step": 60},
    ],
}

# ============================================================================
# EDITABLE:IDENTITY_AND_UI END
# ============================================================================


# ============================================================================
# PROTECTED:MARKET_DATA BEGIN
# Do not modify. Live Bitfinex acquisition + trusted raw evidence boundary.
# ============================================================================

BITFINEX_PUBLIC_WS = "wss://api-pub.bitfinex.com/ws/2"
SEQ_ALL_FLAG = 65536
CHECKSUM_FLAG = 131072
DEFAULT_MARKET_CONFIG = {
    "url": BITFINEX_PUBLIC_WS,
    "symbol": "tBTCUSD",
    "book_precision": "R0",
    "book_frequency": "F0",
    "book_length": 250,
    "stale_seconds": 5.0,
    "reconnect_max_seconds": 30,
}

def _checksum_value(value: Any) -> str:
    # Proven R0 serialization rule, including tiny Decimal scientific notation.
    if not isinstance(value, Decimal): return str(value)
    if value == 0: return "0"
    if value.adjusted() >= -6: return format(value, "f")
    mantissa, exponent = format(value.normalize(), "e").split("e")
    return f"{mantissa}e{int(exponent)}"

@dataclass
class _RawOrder:
    order_id: int
    price: Decimal
    amount: Decimal
    first_seen_ms: int
    last_seen_ms: int
    @property
    def side(self)->str: return "bid" if self.amount>0 else "ask"
    @property
    def size(self)->float: return float(abs(self.amount))

class _ProtectedL3Book:
    def __init__(self)->None:
        self.orders: dict[int,_RawOrder]={}; self.last_update_ms=0; self.last_update_monotonic=0.0
    def clear(self)->None:
        self.orders.clear(); self.last_update_ms=0; self.last_update_monotonic=0.0
    def _touch(self,recv_ms:int)->None:
        self.last_update_ms=int(recv_ms); self.last_update_monotonic=time.monotonic()
    def apply_snapshot(self,entries:Iterable[Any],recv_ms:int)->None:
        new:dict[int,_RawOrder]={}
        for entry in entries:
            if not isinstance(entry,(list,tuple)) or len(entry)<3: raise L3IntegrityError(f"malformed_l3_snapshot_entry:{entry!r}")
            oid=int(entry[0]); price=entry[1] if isinstance(entry[1],Decimal) else Decimal(str(entry[1])); amount=entry[2] if isinstance(entry[2],Decimal) else Decimal(str(entry[2]))
            if price==0 or amount==0: raise L3IntegrityError(f"invalid_active_snapshot_order:{oid}")
            new[oid]=_RawOrder(oid,price,amount,recv_ms,recv_ms)
        if not new: raise L3IntegrityError("empty_l3_snapshot")
        self.orders=new; self._touch(recv_ms)
    def apply_update(self,entry:Any,recv_ms:int)->list[dict[str,Any]]:
        if not isinstance(entry,(list,tuple)) or len(entry)<3: raise L3IntegrityError(f"malformed_l3_entry:{entry!r}")
        oid=int(entry[0]); rp,ra=entry[1],entry[2]; price=rp if isinstance(rp,Decimal) else Decimal(str(rp)); amount=ra if isinstance(ra,Decimal) else Decimal(str(ra)); old=self.orders.get(oid); events=[]
        if price==0:
            if old is not None:
                self.orders.pop(oid,None); events.append({"kind":"remove","order_id":oid,"side":old.side,"price":float(old.price),"qty":old.size,"ts_ms":recv_ms})
            self._touch(recv_ms); return events
        if amount==0: raise L3IntegrityError(f"zero_amount_active_order:{oid}")
        if old is None:
            new=_RawOrder(oid,price,amount,recv_ms,recv_ms); self.orders[oid]=new; events.append({"kind":"add","order_id":oid,"side":new.side,"price":float(price),"qty":new.size,"ts_ms":recv_ms})
        else:
            new_side="bid" if amount>0 else "ask"
            if old.price!=price or old.side!=new_side:
                events.append({"kind":"remove","order_id":oid,"side":old.side,"price":float(old.price),"qty":old.size,"ts_ms":recv_ms})
                new=_RawOrder(oid,price,amount,recv_ms,recv_ms); self.orders[oid]=new; events.append({"kind":"add","order_id":oid,"side":new.side,"price":float(price),"qty":new.size,"ts_ms":recv_ms})
            else:
                new_size=float(abs(amount)); delta=new_size-old.size; self.orders[oid]=_RawOrder(oid,price,amount,old.first_seen_ms,recv_ms)
                if abs(delta)>1e-12: events.append({"kind":"resize_up" if delta>0 else "resize_down","order_id":oid,"side":old.side,"price":float(price),"qty":abs(delta),"new_size":new_size,"ts_ms":recv_ms})
        self._touch(recv_ms); return events
    def bids(self): return sorted((o for o in self.orders.values() if o.amount>0),key=lambda o:(-o.price,o.order_id))
    def asks(self): return sorted((o for o in self.orders.values() if o.amount<0),key=lambda o:(o.price,o.order_id))
    def best_bid(self):
        x=self.bids(); return float(x[0].price) if x else None
    def best_ask(self):
        x=self.asks(); return float(x[0].price) if x else None
    def checksum_payload(self)->str:
        bids=self.bids()[:25]; asks=self.asks()[:25]; vals=[]
        for i in range(max(len(bids),len(asks))):
            if i<len(bids): vals.extend([bids[i].order_id,bids[i].amount])
            if i<len(asks): vals.extend([asks[i].order_id,asks[i].amount])
        return ":".join(_checksum_value(v) for v in vals)
    def checksum(self)->int:
        value=zlib.crc32(self.checksum_payload().encode("utf-8")); return value if value<2**31 else value-2**32

@dataclass
class _PendingEvidence:
    kind:str; channel:str; channel_id:Optional[int]; sequence:Optional[int]; recv_time_ns:int; exchange_time_ms:Optional[int]; payload:Any; raw_message:Optional[str]; protected_context:dict[str,Any]; best_bid:Optional[float]; best_ask:Optional[float]

class _ProtectedMarketInput:
    def __init__(self,symbol:str,stale_after_seconds:float=5.0)->None:
        self.symbol=symbol; self.stale_after_seconds=float(stale_after_seconds); self.book=_ProtectedL3Book(); self.last_sequence=None; self.sequence_gaps=0; self.checksum_count=0; self.checksum_mismatches=0; self.last_verified_checksum=None; self.last_error=None; self.connected=False; self.book_subscribed=False; self.trades_subscribed=False; self.verified=False; self.pending:list[_PendingEvidence]=[]; self.pending_book=False
    def reset_connection(self,error:Optional[str]=None)->None:
        self.book.clear(); self.last_sequence=None; self.connected=False; self.book_subscribed=False; self.trades_subscribed=False; self.verified=False; self.pending.clear(); self.pending_book=False; self.last_error=error
    def mark_connected(self)->None:
        self.connected=True; self.last_error=None
    def mark_subscribed(self,channel:str)->None:
        if channel=="book": self.book_subscribed=True
        elif channel=="trades": self.trades_subscribed=True
    def sequence_only(self,sequence:Optional[int])->None: self._check_sequence(sequence)
    def _check_sequence(self,sequence:Optional[int])->None:
        if sequence is None:
            self.last_error="missing_sequence"; raise L3IntegrityError(self.last_error)
        if self.last_sequence is not None and int(sequence)!=self.last_sequence+1:
            self.sequence_gaps+=1; self.last_error=f"sequence_gap previous={self.last_sequence} current={sequence}"; raise L3IntegrityError(self.last_error)
        self.last_sequence=int(sequence)
    def _stale(self)->bool:
        if self.book.last_update_monotonic<=0:return True
        return (time.monotonic()-self.book.last_update_monotonic)>self.stale_after_seconds
    def integrity(self)->MarketIntegrity:
        bid,ask=self._top()
        crossed=(bid is None or ask is None or not all(math.isfinite(float(x)) and float(x)>0 for x in (bid,ask)) or float(bid)>=float(ask))
        stale=self._stale(); valid=self.connected and self.book_subscribed and self.trades_subscribed and self.verified and not stale and not crossed and self.last_error is None
        return MarketIntegrity(valid,self.connected,self.book_subscribed,self.trades_subscribed,self.verified,stale,self.last_sequence,self.sequence_gaps,self.checksum_count,self.checksum_mismatches,self.last_verified_checksum,self.book.last_update_ms,len(self.pending),self.last_error)
    def _top(self):
        bid=self.book.best_bid(); ask=self.book.best_ask(); return bid,ask
    def _pending(self,kind,channel,channel_id,sequence,recv_ns,exchange_ms,payload,raw_message,context)->_PendingEvidence:
        bid,ask=self._top(); return _PendingEvidence(kind,channel,channel_id,sequence,int(recv_ns),exchange_ms,payload,raw_message,context,bid,ask)
    def _clean(self,p:_PendingEvidence)->CleanMarketEvent:
        integ=self.integrity(); mid=(p.best_bid+p.best_ask)/2.0 if p.best_bid is not None and p.best_ask is not None else None; spread=(p.best_ask-p.best_bid)/mid*10000.0 if mid and p.best_bid is not None and p.best_ask is not None else None
        return CleanMarketEvent(p.kind,"bitfinex_public_ws",self.symbol,p.channel,p.channel_id,p.sequence,p.recv_time_ns,p.recv_time_ns//1_000_000,p.exchange_time_ms,p.payload,p.raw_message,dict(p.protected_context),p.best_bid,p.best_ask,mid,spread,integ)
    def release_ready(self)->list[CleanMarketEvent]:
        if self.pending_book or not self.integrity().valid or not self.pending: return []
        if time.time_ns()-self.pending[0].recv_time_ns > 5_000_000_000:
            self.pending.clear(); self.last_error="checksum_deadline_exceeded"; raise L3IntegrityError(self.last_error)
        batch=self.pending; self.pending=[]; return [self._clean(p) for p in batch]
    def snapshot(self,entries,sequence,recv_ns,channel_id=None,raw_message=None)->list[CleanMarketEvent]:
        self._check_sequence(sequence); rows=list(entries); self.book.apply_snapshot(rows,recv_ns//1_000_000); self.verified=False; self.pending_book=True; self.pending.clear(); self.pending.append(self._pending("l3_snapshot","book",channel_id,sequence,recv_ns,None,rows,raw_message,{"order_count":len(rows)})); return []
    def update(self,entry,sequence,recv_ns,channel_id=None,raw_message=None)->list[CleanMarketEvent]:
        self._check_sequence(sequence); lifecycle=self.book.apply_update(entry,recv_ns//1_000_000); self.verified=bool(self.last_verified_checksum is not None); self.pending_book=True; self.pending.append(self._pending("l3_update","book",channel_id,sequence,recv_ns,None,entry,raw_message,{"lifecycle":lifecycle})); self._guard_pending(); return []
    def trade_snapshot(self,trades,sequence,recv_ns,channel_id=None,raw_message=None)->list[CleanMarketEvent]:
        self._check_sequence(sequence); rows=list(trades); p=self._pending("public_trade_snapshot","trades",channel_id,sequence,recv_ns,None,rows,raw_message,{"trade_count":len(rows)})
        if self.pending_book or not self.integrity().valid: self.pending.append(p); self._guard_pending(); return []
        return [self._clean(p)]
    def trade(self,trade,sequence,recv_ns,channel_id=None,raw_message=None)->list[CleanMarketEvent]:
        self._check_sequence(sequence); row=list(trade); exchange=int(row[1]) if len(row)>1 else None; p=self._pending("public_trade","trades",channel_id,sequence,recv_ns,exchange,row,raw_message,{})
        if self.pending_book or not self.integrity().valid:
            if self.verified and self._stale(): self.last_error="market_stale_before_trade"; raise L3IntegrityError(self.last_error)
            self.pending.append(p); self._guard_pending(); return []
        return [self._clean(p)]
    def checksum(self,server_checksum,sequence,recv_ns,channel_id=None,raw_message=None)->list[CleanMarketEvent]:
        self._check_sequence(sequence); local=self.book.checksum(); self.checksum_count+=1
        if int(server_checksum)!=int(local):
            self.checksum_mismatches+=1; self.verified=False; self.last_error=f"checksum_mismatch server={server_checksum} local={local}"; self.pending.clear(); self.pending_book=False; raise L3IntegrityError(self.last_error)
        self.last_verified_checksum=int(server_checksum); self.verified=True; self.last_error=None; self.pending_book=False
        if not self.integrity().valid:
            self.last_error="invalid_or_stale_book_at_checksum"; self.pending.clear(); raise L3IntegrityError(self.last_error)
        cs=self._pending("l3_checksum","book",channel_id,sequence,recv_ns,None,{"checksum":int(server_checksum)},raw_message,{"local_checksum":local})
        self.pending.append(cs)
        return self.release_ready()
    def _guard_pending(self)->None:
        if len(self.pending)>10_000 or (self.pending and time.time_ns()-self.pending[0].recv_time_ns>5_000_000_000):
            self.pending.clear(); self.last_error="pending_evidence_limit"; raise L3IntegrityError(self.last_error)

class _BitfinexPublicFeed:
    """Live market data from Bitfinex directly (market source "bitfinex", default) or through tt_input
    (source "tt_input"). Both sources go through the same parser and the same protected checks."""
    def __init__(self,runtime:"TradeRuntime",config:dict[str,Any])->None:
        self.runtime=runtime; self.config=dict(DEFAULT_MARKET_CONFIG); self.config.update(config or {}); self.channels:dict[int,str]={}; self.log=logging.getLogger("trade.bitfinex")
    @staticmethod
    def _parse(raw:str): return json.loads(raw,parse_float=Decimal)
    @staticmethod
    def _split_sequence(msg:list)->tuple[list,Optional[int]]:
        # SEQ_ALL appends the sequence number. The TIMESTAMP flag (used by tt_input's exchange connection) appends
        # the exchange time in ms after it; that is dropped here (sequence numbers never reach 10**12).
        body=list(msg)
        if len(body)>=4 and isinstance(body[-1],int) and not isinstance(body[-1],bool) and body[-1]>10**12 and isinstance(body[-2],int): body=body[:-1]
        if len(body)>=3 and isinstance(body[-1],int): return body[:-1],int(body[-1])
        return body,None
    async def run(self)->None:
        try: import websockets
        except ImportError as exc: raise RuntimeError("live market feed requires the 'websockets' package; install config/requirements-market.txt") from exc
        connect=self._run_tt_input if str(self.config.get("source","bitfinex"))=="tt_input" else self._run_connection
        backoff=1
        while True:
            try:
                await connect(websockets); backoff=1
            except asyncio.CancelledError:
                self.runtime.input.reset_connection("feed_cancelled"); raise
            except Exception as exc:
                self.runtime.input.reset_connection(repr(exc)); self.log.exception("market feed failure; reconnecting")
                await asyncio.sleep(backoff); backoff=min(backoff*2,int(self.config.get("reconnect_max_seconds",30)))
    async def _run_connection(self,websockets)->None:
        url=str(self.config.get("url",BITFINEX_PUBLIC_WS)); symbol=str(self.config.get("symbol",self.runtime.symbol))
        async with websockets.connect(url,ping_interval=20,ping_timeout=20,close_timeout=5,max_queue=8192) as ws:
            self.channels.clear(); self.runtime.input.reset_connection(); self.runtime.input.mark_connected()
            await ws.send(json.dumps({"event":"conf","flags":SEQ_ALL_FLAG+CHECKSUM_FLAG}))
            await ws.send(json.dumps({"event":"subscribe","channel":"book","symbol":symbol,"prec":"R0","freq":str(self.config.get("book_frequency","F0")),"len":str(self.config.get("book_length",250))}))
            await ws.send(json.dumps({"event":"subscribe","channel":"trades","symbol":symbol}))
            while True:
                raw=await asyncio.wait_for(ws.recv(),timeout=10)
                self._handle_raw(raw,time.time_ns())
    async def _run_tt_input(self,websockets)->None:
        # tt_input raw mode: one tt_input-created starting snapshot (labelled, never presented as an exchange message),
        # then every exchange message exactly as the exchange sent it, parsed and verified here as if received directly.
        url=str(self.config.get("tt_input_url","ws://127.0.0.1:8900/stream")); feed=str(self.config.get("tt_input_feed","bitfinex."+str(self.config.get("symbol",self.runtime.symbol))))
        async with websockets.connect(url,ping_interval=20,ping_timeout=20,close_timeout=5,max_queue=8192,max_size=2**24) as ws:
            self.channels.clear(); self.runtime.input.reset_connection()
            await ws.send(json.dumps({"op":"subscribe","feed":feed,"mode":"raw"}))
            while True:
                msg=json.loads(await asyncio.wait_for(ws.recv(),timeout=10)); kind=msg.get("type"); recv_ns=time.time_ns()
                if kind=="raw": self._handle_raw(msg["m"],recv_ns)
                elif kind=="snapshot":
                    # Verified like any snapshot: nothing is released until the exchange's next checksum matches it.
                    self.channels={int(k):str(v) for k,v in msg["channels"].items()}
                    self.runtime.input.reset_connection(); self.runtime.input.mark_connected()
                    for channel in set(self.channels.values()): self.runtime.input.mark_subscribed(channel)
                    book_id=next((k for k,v in self.channels.items() if v=="book"),None)
                    rows=[[int(r[0]),Decimal(str(r[1])),Decimal(str(r[2]))] for r in msg["orders"]]
                    self.runtime.on_l3_snapshot(rows,int(msg["at_seq"]),recv_ns,book_id,None)
                elif kind=="notice":  # tt_input's own exchange connection dropped or reconnected
                    self.channels.clear(); self.runtime.input.reset_connection(f"tt_input_{msg.get('event')}:{msg.get('why')}")
                    if msg.get("event")=="upstream_connected": self.runtime.input.mark_connected()
                elif kind=="error": raise RuntimeError(f"tt_input: {msg.get('error')}")
    def _handle_raw(self,raw,recv_ns:int)->None:
        raw=raw.decode("utf-8") if isinstance(raw,(bytes,bytearray)) else raw; msg=self._parse(raw)
        if isinstance(msg,dict):
            event=msg.get("event")
            if event=="subscribed":
                cid=int(msg["chanId"]); channel=str(msg.get("channel")); self.channels[cid]=channel; self.runtime.input.mark_subscribed(channel); self.runtime._handle_events(self.runtime.input.release_ready())
            elif event=="error": raise RuntimeError(f"Bitfinex error event: {msg}")
            elif event=="info" and msg.get("code") in {20051,20060}: raise RuntimeError(f"Bitfinex websocket restart/maintenance code={msg.get('code')}")
            return
        if not isinstance(msg,list) or len(msg)<2: return
        body,seq=self._split_sequence(msg); cid=int(body[0]); channel=self.channels.get(cid)
        if channel is None: self.runtime.input.sequence_only(seq); return
        if body[1]=="hb":
            self.runtime.input.sequence_only(seq)
            if self.runtime.input._stale(): raise L3IntegrityError("heartbeat_only_stale_book")
            return
        if channel=="book":
            if body[1]=="cs": self.runtime.on_l3_checksum(int(body[2]),seq,recv_ns,cid,raw); return
            payload=body[1]
            if isinstance(payload,list) and payload and isinstance(payload[0],list): self.runtime.on_l3_snapshot(payload,seq,recv_ns,cid,raw)
            elif isinstance(payload,list): self.runtime.on_l3_update(payload,seq,recv_ns,cid,raw)
            else: raise L3IntegrityError(f"malformed_book_message:{body!r}")
            return
        if channel=="trades":
            payload=body[1]
            if isinstance(payload,list) and (not payload or isinstance(payload[0],list)):
                rows=[]
                for row in payload:
                    if not isinstance(row,list) or len(row)<4: raise L3IntegrityError(f"malformed_trade_snapshot_row:{row!r}")
                    rows.append([int(row[0]),int(row[1]),row[2],row[3]])
                self.runtime.on_public_trade_snapshot(rows,seq,recv_ns,cid,raw); return
            if isinstance(payload,str) and payload in {"te","tu"} and len(body)>=3:
                if payload=="tu": self.runtime.input.sequence_only(seq); return
                row=body[2]
                if not isinstance(row,list) or len(row)<4: raise L3IntegrityError(f"malformed_trade_row:{row!r}")
                self.runtime.on_public_trade([int(row[0]),int(row[1]),row[2],row[3]],seq,recv_ns,cid,raw); return
            raise L3IntegrityError(f"malformed_trade_message:{body!r}")

# ============================================================================
# PROTECTED:MARKET_DATA END
# ============================================================================


# ============================================================================
# PROTECTED:PAPER_TRUTH BEGIN
# Fixed benchmark accounting/fill assumptions used for objective comparison.
# Think may change trading decisions, not this truth layer.
# AI_test (contract 2.3): costs are set here; positions are keyed by position_id so several can be open at once.
# ============================================================================

PAPER_DEFAULTS = {
    "starting_equity_usd": 10_000.0,
    "fee_bps": 0.0,          # per side, on the fill notional (Bitfinex: zero maker/taker fee)
    "slippage_bps": 0.5,     # per side, on top of crossing the real spread (buy at the ask, sell at the bid)
}


class _ProtectedPaperBroker:
    """Paper broker. ENTER opens a position under decision.metadata["position_id"] (default: the symbol); EXIT
    closes the position with that id. Fills cross the verified spread plus slippage; fees are charged per side."""

    def __init__(self, starting_equity_usd: float = 10_000.0, fee_bps: Optional[float] = None, slippage_bps: Optional[float] = None) -> None:
        self.starting_equity_usd = float(starting_equity_usd)
        self.fee_bps = float(PAPER_DEFAULTS["fee_bps"] if fee_bps is None else fee_bps)
        self.slippage_bps = float(PAPER_DEFAULTS["slippage_bps"] if slippage_bps is None else slippage_bps)
        if not (0.0 <= self.fee_bps <= 100.0 and 0.0 <= self.slippage_bps <= 100.0):
            raise TradeContractError("paper costs out of range")
        self.realized_pnl_usd = 0.0
        self.positions: dict[str, PositionState] = {}
        self.entry_fees: dict[str, float] = {}
        self.executions: list[ExecutionRecord] = []
        self.trade_pnls: list[float] = []
        self.peak_equity = self.starting_equity_usd
        self.max_drawdown_pct = 0.0

    def _fill_price(self, side: str, bid: float, ask: float) -> tuple[float, float]:
        if side == "buy":
            fill = ask * (1.0 + self.slippage_bps / 10_000.0)
        else:
            fill = bid * (1.0 - self.slippage_bps / 10_000.0)
        return fill, self.slippage_bps

    def _fee(self, notional: float) -> float:
        return abs(notional) * self.fee_bps / 10_000.0

    def _unrealized_total(self, event: CleanMarketEvent) -> float:
        total = 0.0
        for position in self.positions.values():
            if position.symbol != event.symbol:
                continue
            mark = event.best_bid if position.side == "long" else event.best_ask
            if mark is None:
                continue
            signed = position.size if position.side == "long" else -position.size
            position.unrealized_pnl = signed * (float(mark) - position.entry_price)
            total += position.unrealized_pnl
        return total

    def equity(self, event: Optional[CleanMarketEvent] = None) -> float:
        unrealized = self._unrealized_total(event) if event is not None else 0.0
        equity = self.starting_equity_usd + self.realized_pnl_usd + unrealized - sum(self.entry_fees.values())
        self.peak_equity = max(self.peak_equity, equity)
        if self.peak_equity > 0:
            dd = max(0.0, (self.peak_equity - equity) / self.peak_equity)
            self.max_drawdown_pct = max(self.max_drawdown_pct, dd)
        return equity

    def execute(self, decision: TradeDecision, event: CleanMarketEvent) -> Optional[ExecutionRecord]:
        action = str(decision.action).upper()
        symbol = decision.symbol or event.symbol
        if action == "HOLD":
            return None
        if event.best_bid is None or event.best_ask is None:
            raise TradeContractError("missing_executable_bid_ask")
        now_ms = event.recv_time_ms
        pid = str((decision.metadata or {}).get("position_id") or symbol)
        current = self.positions.get(pid)

        if action in {"ENTER_LONG", "ENTER_SHORT"}:
            if current is not None or decision.requested_size <= 0:
                return None
            side = "buy" if action == "ENTER_LONG" else "sell"
            fill, slip = self._fill_price(side, event.best_bid, event.best_ask)
            fee = self._fee(fill * decision.requested_size)
            pos_side = "long" if action == "ENTER_LONG" else "short"
            self.positions[pid] = PositionState(
                symbol=symbol,
                side=pos_side,
                size=float(decision.requested_size),
                entry_price=fill,
                entry_time_ms=now_ms,
                stop_price=decision.stop_price,
                target_price=decision.target_price,
                max_hold_seconds=decision.max_hold_seconds,
                strategy_tag=str(decision.metadata.get("strategy_tag") or "default"),
                position_id=pid,
            )
            self.entry_fees[pid] = fee
            rec = ExecutionRecord(
                action=action,
                symbol=symbol,
                side=side,
                size=float(decision.requested_size),
                fill_price=fill,
                fee_usd=fee,
                slippage_bps=slip,
                realized_pnl=0.0,
                timestamp_ms=now_ms,
                reason=decision.reason,
                position_id=pid,
            )
            self.executions.append(rec)
            self.equity(event)
            return rec

        if action == "EXIT" and current is not None:
            side = "sell" if current.side == "long" else "buy"
            fill, slip = self._fill_price(side, event.best_bid, event.best_ask)
            exit_fee = self._fee(fill * current.size)
            signed = current.size if current.side == "long" else -current.size
            gross = signed * (fill - current.entry_price)
            realized = gross - float(self.entry_fees.pop(pid, 0.0)) - exit_fee
            self.realized_pnl_usd += realized
            self.trade_pnls.append(realized)
            self.positions.pop(pid, None)
            rec = ExecutionRecord(
                action="EXIT",
                symbol=symbol,
                side=side,
                size=current.size,
                fill_price=fill,
                fee_usd=exit_fee,
                slippage_bps=slip,
                realized_pnl=realized,
                timestamp_ms=now_ms,
                reason=decision.reason,
                position_id=pid,
            )
            self.executions.append(rec)
            self.equity(event)
            return rec
        return None

    def metrics(self, event: Optional[CleanMarketEvent] = None) -> PerformanceMetrics:
        equity = self.equity(event)
        wins = [x for x in self.trade_pnls if x > 0]
        losses = [x for x in self.trade_pnls if x < 0]
        gross_profit = sum(wins)
        gross_loss_abs = abs(sum(losses))
        closed = len(self.trade_pnls)
        profit_factor = gross_profit / gross_loss_abs if gross_loss_abs > 0 else None
        expectancy = sum(self.trade_pnls) / closed if closed else None
        win_rate = len(wins) / closed * 100.0 if closed else None
        avg_win = gross_profit / len(wins) if wins else None
        avg_loss = sum(losses) / len(losses) if losses else None
        net = equity - self.starting_equity_usd
        return PerformanceMetrics(
            starting_equity_usd=self.starting_equity_usd,
            equity_usd=equity,
            net_pnl_usd=net,
            return_pct=(net / self.starting_equity_usd * 100.0) if self.starting_equity_usd else 0.0,
            gross_profit_usd=gross_profit,
            gross_loss_usd=sum(losses),
            profit_factor=profit_factor,
            expectancy_usd=expectancy,
            win_rate_pct=win_rate,
            average_win_usd=avg_win,
            average_loss_usd=avg_loss,
            max_drawdown_pct=self.max_drawdown_pct * 100.0,
            closed_trades=closed,
            executions=len(self.executions),
            accounting_valid=math.isfinite(equity),
        )

# ============================================================================
# PROTECTED:PAPER_TRUTH END
# ============================================================================


# ============================================================================
# EDITABLE:SCIENTIFIC_WORKSPACE BEGIN
# AI/Think may replace this entire section. Healthy raw R0 + public-trade
# evidence arrives here without being reduced to a prescribed feature set.
# Maintain any history/state/windows/models you need.
# ============================================================================

# HOW THIS STRATEGY WORKS (AI_test)
# It runs the frozen Bybit-trained models of research/ on live Bitfinex data, at four horizons at once.
#   DATA      the order book by order id + public trades -> one row per second -> one bar per minute, built exactly as
#             research/bitfinex/bitfinex_replay.py and research/kit/research.py (stage bars2) built the research bars.
#   FEATURES  the 109 features of research.py build_features(), recomputed every minute from the minute history.
#   MODELS    trade/aitest_models.json.gz: per horizon a direction, a big-move and a volatility gradient-boosting
#             model (trained on Bybit BTCUSDT 1 Jul - 23 Sep 2026, never on Bitfinex), as plain arrays.
#   STRATEGY  one slot per horizon (30/60/120/240 min). At each minute close a slot enters when the big-move
#             probability and the |direction score| are both above quantiles of the Bybit training distribution;
#             it exits at a symmetric barrier (barrier_k * sigma * sqrt(H)) or after H minutes. Slots trade
#             independently; several positions can be open at once (max_positions_per_horizon per slot).
#   HISTORY   the minute history (24 h), the book and the open-minute state are saved every minute and restored on
#             restart; the downtime is refilled from the app's own market recordings, so a restart needs no warm-up.
#   LOG       every minute's predictions for every horizon go to <runtime>/aitest_predictions/YYYY-MM-DD.jsonl
#             (research/bitfinex/evaluate_live.py turns them into AUC / hit rate / P&L per horizon at any cost).
# Decisions are one per market event: entries chosen at a minute close are executed on the next events (milliseconds
# later), exits are checked on every event at the price the position could actually close at.

import gzip as _gzip
import os as _os
import struct as _struct
from datetime import datetime as _datetime, timezone as _timezone
from pathlib import Path as _Path

EPS = 1e-9
NAN = float("nan")
HORIZONS = (30, 60, 120, 240)
MODEL_FILE = "aitest_models.json.gz"
HISTORY_FILE = "aitest_history.json.gz"
HISTORY_SCHEMA = 1
HISTORY_KEEP_MINUTES = 1500          # > 1440: the longest feature window
BIG_TRADE_BTC = 1.0                  # research: big_buy / big_sell
FLOW_BAND_BPS = 2.0                  # research: add / remove flows within 2 bps of the mid
DEPTH_BANDS_BPS = (0.5, 1.0, 2.0, 3.0)
L2_WEIGHT_BPS = 0.2
SIGMA_WINDOW_MIN = 240
LONG_WIN, LONG_MIN = 1440, 240
FUNDING_HOURS_UTC = (0, 8, 16)
L2_SIGNALS_ALL = ("l2_imb10", "l2_wimb10", "micro_end", "l2_ofi_15", "l2_timb_15", "l2_net_asym_15",
                  "l2_flow_dep_60", "l2_slope_asym", "l2_reach_asym", "l2_tot_imb", "l2_big_imb", "l2_dep05_asym_d15")

# one minute bar = these fields, in this order (history file, prediction of every feature)
BAR_FIELDS = (["t", "open", "high", "low", "close", "n_rows", "rv", "buy_vol", "sell_vol", "n_trades", "n_large",
               "buy_tail", "sell_tail", "ofi", "ofi_tail", "added", "removed", "spread_end", "spread_mean",
               "bimb_end", "bimb_mean", "micro_end"]
              + [f"{a}{l}_{b}" for l in ("05", "1", "3") for a, b in (("dimb", "end"), ("dimb", "mean"), ("dtot", "end"), ("dtot", "mean"))]
              + [f"l2_{c}_end" for c in ("imb3", "imb5", "imb10", "wimb10", "slope_b", "slope_a", "conc_b", "conc_a",
                                          "near_b", "near_a", "tot_imb", "reach_asym", "bd05", "ad05")]
              + ["l2_imb10_m15", "l2_wimb10_m15", "l2_micro_m15", "l2_imb10_at44", "l2_imb10_at0", "l2_wimb10_at44",
                 "l2_micro_at54", "l2_micro_at44", "l2_micro_at29", "l2_micro_at0", "l2_bimb_at44", "l2_bimb_at0",
                 "l2_dimb05_at44", "l2_mid_at54", "l2_mid_at44", "l2_mid_at29", "l2_bd05_at44", "l2_ad05_at44"]
              + [f"l2_{c}_w{w}" for w in (5, 15, 30) for c in ("ofi", "buy_vol", "sell_vol", "add_b", "rem_b", "add_a", "rem_a")]
              + [f"l2_{c}_sum" for c in ("add_b", "rem_b", "add_a", "rem_a", "big_buy", "big_sell")]
              + ["l2_bd1_mean", "l2_ad1_mean", "l2_resa_num", "l2_resa_den", "l2_resb_num", "l2_resb_den"]
              + [f"sig_{s}" for s in L2_SIGNALS_ALL])
FI = {name: i for i, name in enumerate(BAR_FIELDS)}
END_FIELDS = tuple(i for i, n in enumerate(BAR_FIELDS) if n.endswith("_end"))          # forward-filled across empty minutes
SUM_FIELDS = tuple(FI[n] for n in ("rv", "buy_vol", "sell_vol", "n_trades", "n_large", "buy_tail", "sell_tail", "ofi",
                                   "ofi_tail", "added", "removed"))                    # 0 in an empty minute (bars)


def _f32(x: float) -> float:
    """research level files were float32"""
    return _struct.unpack("f", _struct.pack("f", x))[0] if x == x else x


def _runtime_dir() -> _Path:
    d = _os.environ.get("APP_RUNTIME_DIR")
    return _Path(d) if d else _Path(__file__).resolve().parents[1] / "runtime"


_MODELS: dict[str, Any] = {}


def load_models() -> dict[str, Any]:
    """The exported model file next to this trade.py (or in the app's trade/ folder for Lab candidates elsewhere)."""
    if _MODELS:
        return _MODELS
    here = _Path(__file__).resolve()
    for p in (here.with_name(MODEL_FILE), here.parents[1] / "trade" / MODEL_FILE, _Path.cwd() / "trade" / MODEL_FILE):
        if p.is_file():
            with _gzip.open(p, "rt", encoding="utf-8") as fh:
                _MODELS.update(json.load(fh))
            _MODELS["path"] = str(p)
            return _MODELS
    raise FileNotFoundError(f"{MODEL_FILE} not found next to {here}")


def _tree_raw(model: dict[str, Any], x: list) -> float:
    """sklearn HistGradientBoosting raw prediction from the exported arrays (baseline + leaf values, same order)."""
    s = model["baseline"]
    for tree in model["trees"]:
        k = 0
        while True:
            node = tree[k]
            f = node[0]
            if f < 0:
                s += node[5]
                break
            v = x[f]
            k = (node[3] if node[2] else node[4]) if v != v else (node[3] if v <= node[1] else node[4])
    return s


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z)) if z >= -700 else 0.0


def _interp(table: dict[str, float], q: float) -> float:
    pts = sorted((float(k), float(v)) for k, v in table.items())
    if q <= pts[0][0]:
        return pts[0][1]
    for (q0, v0), (q1, v1) in zip(pts, pts[1:]):
        if q <= q1:
            return v0 + (v1 - v0) * (q - q0) / (q1 - q0)
    return pts[-1][1]


# ---------------------------------------------------------------- rolling helpers with pandas semantics
def _cnt_sum(v: list) -> tuple[int, float]:
    n = 0; s = 0.0
    for x in v:
        if x == x:
            n += 1; s += x
    return n, s


def _rsum(v: list, minp: int) -> float:
    n, s = _cnt_sum(v)
    return s if n >= minp else NAN


def _rmean(v: list, minp: int) -> float:
    n, s = _cnt_sum(v)
    return s / n if n >= minp and n > 0 else NAN


def _rstd(v: list, minp: int) -> float:
    xs = [x for x in v if x == x]
    n = len(xs)
    if n < max(minp, 2):
        return NAN
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var) if var > 0 else 0.0


def _rmax(v: list, minp: int) -> float:
    xs = [x for x in v if x == x]
    return max(xs) if len(xs) >= minp and xs else NAN


def _rmin(v: list, minp: int) -> float:
    xs = [x for x in v if x == x]
    return min(xs) if len(xs) >= minp and xs else NAN


def _div0(a: float, b: float) -> float:
    """a / b with b == 0 -> NaN (pandas .replace(0, np.nan))"""
    return NAN if b == 0 or b != b or a != a else a / b


def _log(x: float) -> float:
    """numpy log: NaN for NaN or negative, -inf for 0"""
    if x != x or x < 0:
        return NAN
    return -math.inf if x == 0 else math.log(x)


def _nz(x: float) -> float:
    return 0.0 if x != x else x


def _asinh(x: float) -> float:
    return math.asinh(x) if x == x else NAN


class _L3Book:
    """Order book by order id with exactly the per-second statistics of research/bitfinex/bitfinex_replay.py."""

    def __init__(self) -> None:
        self.orders: dict[int, tuple[float, float]] = {}
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.btot = self.atot = 0.0
        self.reset_second()

    def reset_second(self) -> None:
        self.ofi = 0.0
        self.add_b = self.rem_b = self.add_a = self.rem_a = 0.0
        self.msgs = 0
        self.mid_hi, self.mid_lo = -math.inf, math.inf

    def clear(self) -> None:
        self.orders.clear(); self.bids.clear(); self.asks.clear(); self.btot = self.atot = 0.0

    def best(self):
        if not self.bids or not self.asks:
            return None
        pb = max(self.bids); pa = min(self.asks)
        return pb, self.bids[pb], pa, self.asks[pa]

    def _level_add(self, price: float, delta: float, is_bid: bool) -> None:
        side = self.bids if is_bid else self.asks
        new = side.get(price, 0.0) + delta
        if is_bid:
            self.btot += delta
        else:
            self.atot += delta
        if new <= 1e-12:
            side.pop(price, None)
        else:
            side[price] = new

    def _flow(self, price: float, delta: float, is_bid: bool, mid_pre) -> None:
        if mid_pre is None or abs(price - mid_pre) / mid_pre * 1e4 > FLOW_BAND_BPS:
            return
        if is_bid:
            if delta > 0: self.add_b += delta
            else: self.rem_b += -delta
        else:
            if delta > 0: self.add_a += delta
            else: self.rem_a += -delta

    def load_snapshot(self, rows) -> None:
        self.clear()
        for row in rows:
            oid, price, amount = int(row[0]), float(row[1]), float(row[2])
            self.orders[oid] = (price, amount)
            self._level_add(price, abs(amount), amount > 0)
        self.reset_second()

    def update(self, oid: int, price: float, amount: float) -> None:
        pre = self.best()
        mid_pre = (pre[0] + pre[2]) / 2 if pre else None
        old = self.orders.get(oid)
        if price == 0:
            if old is not None:
                self._level_add(old[0], -abs(old[1]), old[1] > 0)
                self._flow(old[0], -abs(old[1]), old[1] > 0, mid_pre)
                del self.orders[oid]
        else:
            if old is not None:
                self._level_add(old[0], -abs(old[1]), old[1] > 0)
                if old[0] != price:
                    self._flow(old[0], -abs(old[1]), old[1] > 0, mid_pre)
                    self._flow(price, abs(amount), amount > 0, mid_pre)
                else:
                    self._flow(price, abs(amount) - abs(old[1]), amount > 0, mid_pre)
            else:
                self._flow(price, abs(amount), amount > 0, mid_pre)
            self.orders[oid] = (price, amount)
            self._level_add(price, abs(amount), amount > 0)
        self.msgs += 1
        post = self.best()
        if pre and post:
            pb, qb, pa, qa = pre
            b, sb, a, sa = post
            self.ofi += (sb if b >= pb else 0) - (qb if b <= pb else 0) - (sa if a <= pa else 0) + (qa if a >= pa else 0)
        if post:
            m = (post[0] + post[2]) / 2
            if m > self.mid_hi: self.mid_hi = m
            if m < self.mid_lo: self.mid_lo = m

    def second_row(self, trades: list) -> dict[str, Any]:
        """the 1-second row (bitfinex_replay Book.row) followed by reset_second()"""
        r: dict[str, Any] = {"ofi": self.ofi, "add_b": self.add_b, "rem_b": self.rem_b, "add_a": self.add_a,
                             "rem_a": self.rem_a, "msgs": self.msgs}
        b = self.best()
        if b:
            pb, qb, pa, qa = b
            mid = (pb + pa) / 2
            r.update(bid1=pb, ask1=pa, bsz1=qb, asz1=qa,
                     mid_hi=self.mid_hi if self.mid_hi > -math.inf else mid, mid_lo=self.mid_lo if self.mid_lo < math.inf else mid)
            sb, sa = sorted(self.bids), sorted(self.asks)
            for k in DEPTH_BANDS_BPS:
                lo, hi = mid * (1 - k / 1e4), mid * (1 + k / 1e4)
                r[f"bd{k:g}"] = sum(self.bids[p] for p in sb if p >= lo)
                r[f"ad{k:g}"] = sum(self.asks[p] for p in sa if p <= hi)
            r["bdtot"], r["adtot"] = self.btot, self.atot
            r["breach"] = (mid - sb[0]) / mid * 1e4
            r["areach"] = (sa[-1] - mid) / mid * 1e4
            lv = [NAN] * 40
            for i in range(min(10, len(sb))):
                p = sb[-1 - i]
                lv[i], lv[10 + i] = _f32((p - mid) / mid * 1e4), _f32(self.bids[p])
            for i in range(min(10, len(sa))):
                p = sa[i]
                lv[20 + i], lv[30 + i] = _f32((p - mid) / mid * 1e4), _f32(self.asks[p])
            r["lv"] = lv
        if trades:
            buys = [a for a, _ in trades if a > 0]
            sells = [-a for a, _ in trades if a < 0]
            r.update(buy_vol=sum(buys), sell_vol=sum(sells), n_trades=len(trades),
                     big_buy=sum(a for a in buys if a >= BIG_TRADE_BTC), big_sell=sum(a for a in sells if a >= BIG_TRADE_BTC))
        self.reset_second()
        return r


class _MinuteAcc:
    """Per-minute aggregation of the 1-second rows (research.py AGG + aggregate_l2)."""

    def __init__(self, minute_s: int) -> None:
        self.minute = minute_s
        self.v = [NAN] * len(BAR_FIELDS)
        self.v[FI["t"]] = minute_s * 1000
        for name in ("n_rows", "rv", "buy_vol", "sell_vol", "n_trades", "n_large", "buy_tail", "sell_tail", "ofi", "ofi_tail",
                     "added", "removed"):
            self.v[FI[name]] = 0.0
        self.s = {k: 0.0 for k in ("spread", "bimb", "dimb05", "dtot05", "dimb1", "dtot1", "dimb3", "dtot3")}
        self.c = {k: 0 for k in ("imb10_m15", "wimb10_m15", "micro_m15", "bd1", "ad1")}
        self.m = {k: 0.0 for k in ("imb10_m15", "wimb10_m15", "micro_m15", "bd1", "ad1")}
        for name in BAR_FIELDS:
            if name.startswith("l2_") and (name.endswith(("_sum", "_num", "_den")) or "_w" in name[-4:]):
                self.v[FI[name]] = 0.0

    def add(self, sec: int, x: dict[str, float]) -> None:
        v, F = self.v, FI
        mid = x["mid"]
        if mid == mid:
            v[F["n_rows"]] += 1
            if v[F["open"]] != v[F["open"]]:
                v[F["open"]] = mid; v[F["high"]] = mid; v[F["low"]] = mid
            else:
                if mid > v[F["high"]]: v[F["high"]] = mid
                if mid < v[F["low"]]: v[F["low"]] = mid
            v[F["close"]] = mid
        if x["r2"] == x["r2"]:
            v[F["rv"]] += x["r2"]
        for name in ("buy_vol", "sell_vol", "n_trades", "n_large", "ofi"):
            v[F[name]] += x[name]
        v[F["added"]] += x["added"]; v[F["removed"]] += x["removed"]
        if sec >= 50:
            v[F["buy_tail"]] += x["buy_vol"]; v[F["sell_tail"]] += x["sell_vol"]; v[F["ofi_tail"]] += x["ofi"]
        for key, end in (("spread", "spread_end"), ("bimb", "bimb_end"), ("dimb05", "dimb05_end"), ("dtot05", "dtot05_end"),
                         ("dimb1", "dimb1_end"), ("dtot1", "dtot1_end"), ("dimb3", "dimb3_end"), ("dtot3", "dtot3_end")):
            val = x[key]
            if val == val:
                self.s[key] += val; v[F[end]] = val
        if x["micro"] == x["micro"]:
            v[F["micro_end"]] = x["micro"]
        for c in ("imb3", "imb5", "imb10", "wimb10", "slope_b", "slope_a", "conc_b", "conc_a", "near_b", "near_a",
                  "tot_imb", "reach_asym", "bd05", "ad05"):
            val = x[c]
            if val == val:
                v[F[f"l2_{c}_end"]] = val
        if sec >= 45:
            for c, key in (("imb10", "imb10_m15"), ("wimb10", "wimb10_m15"), ("micro", "micro_m15")):
                val = x[c]
                if val == val:
                    self.m[key] += val; self.c[key] += 1
        at = {44: ("imb10", "wimb10", "micro", "bimb", "dimb05", "mid", "bd05", "ad05"), 0: ("imb10", "micro", "bimb"),
              54: ("micro", "mid"), 29: ("micro", "mid")}.get(sec)
        if at:
            for c in at:
                v[F[f"l2_{c}_at{sec}"]] = x[c]
        for w in (5, 15, 30):
            if sec >= 60 - w:
                for c in ("ofi", "buy_vol", "sell_vol", "add_b", "rem_b", "add_a", "rem_a"):
                    v[F[f"l2_{c}_w{w}"]] += x[c]
        for c in ("add_b", "rem_b", "add_a", "rem_a", "big_buy", "big_sell"):
            v[F[f"l2_{c}_sum"]] += x[c]
        for c in ("bd1", "ad1"):
            val = x[c]
            if val == val:
                self.m[c] += val; self.c[c] += 1
        if x["buy_vol"] > 0:
            v[F["l2_resa_num"]] += x["add_a"]; v[F["l2_resa_den"]] += x["rem_a"]
        if x["sell_vol"] > 0:
            v[F["l2_resb_num"]] += x["add_b"]; v[F["l2_resb_den"]] += x["rem_b"]

    def bar(self, prev: Optional[list]) -> list:
        v, F = list(self.v), FI
        n = v[F["n_rows"]]
        for key, mean in (("spread", "spread_mean"), ("bimb", "bimb_mean"), ("dimb05", "dimb05_mean"), ("dtot05", "dtot05_mean"),
                          ("dimb1", "dimb1_mean"), ("dtot1", "dtot1_mean"), ("dimb3", "dimb3_mean"), ("dtot3", "dtot3_mean")):
            v[F[mean]] = self.s[key] / n if n > 0 else NAN
        for key in ("imb10_m15", "wimb10_m15", "micro_m15"):
            v[F[f"l2_{key}"]] = self.m[key] / self.c[key] if self.c[key] else NAN
        for key in ("bd1", "ad1"):
            v[F[f"l2_{key}_mean"]] = self.m[key] / self.c[key] if self.c[key] else NAN
        return _finish_bar(v, prev)


def _finish_bar(v: list, prev: Optional[list]) -> list:
    """research.py finish_bars for one minute: close forward-filled, OHL = close when missing, *_end forward-filled"""
    F = FI
    if v[F["close"]] != v[F["close"]] and prev is not None:
        v[F["close"]] = prev[F["close"]]
    for k in ("open", "high", "low"):
        if v[F[k]] != v[F[k]]:
            v[F[k]] = v[F["close"]]
    if prev is not None:
        for i in END_FIELDS:
            if v[i] != v[i]:
                v[i] = prev[i]
    return v


def _empty_bar(minute_s: int, prev: Optional[list]) -> list:
    """a minute without any 1-second row (research: reindexed minute): sums 0, means and l2 aggregates NaN"""
    v = [NAN] * len(BAR_FIELDS)
    v[FI["t"]] = minute_s * 1000
    v[FI["n_rows"]] = 0.0
    for i in SUM_FIELDS:
        v[i] = 0.0
    return _finish_bar(v, prev)


@dataclass
class TradeConfig:
    # Position size per trade (BTC) and the paper-cost mirrors (estimates only; PAPER_DEFAULTS is the truth).
    base_size: float = 0.01
    assumed_equity_usd: float = 10_000.0
    paper_fee_bps_each_side: float = 0.0
    paper_slippage_bps_each_side: float = 0.5

    # Horizon slots: 1 = trade this horizon, 0 = only predict and log it.
    h30_enabled: int = 1
    h60_enabled: int = 1
    h120_enabled: int = 1
    h240_enabled: int = 1
    max_positions_per_horizon: int = 1   # > 1 lets a slot open a new position every minute the rule fires

    # Entry rule, as quantiles of the Bybit TRAINING distribution of each model (fixed before any Bitfinex data).
    hit_quantile: float = 0.6667         # big-move probability in its top tercile ...
    score_quantile: float = 0.8          # ... and |direction score| in its top 20 %
    # Exit: symmetric barrier at barrier_k * sigma(1 min, trailing 240 min) * sqrt(H), time cap H minutes.
    barrier_k: float = 1.0

    # Filters.
    max_spread_bps: float = 5.0
    min_history_minutes: int = 240       # trade once the 240-min normalisers exist; 1440 = full research history
    min_rows_per_minute: int = 30        # research quality rule for a usable minute

    # History and logs.
    carry_gap_seconds: int = 3600        # silent seconds up to this long carry the book forward (research replay rule)
    save_history: int = 1
    save_history_every_minutes: int = 5  # the recordings refill whatever happened after the last save
    max_gapfill_minutes: int = 360       # refill at most this much downtime from the recordings at start-up
    lazy_gapfill_minutes: int = 15       # Lab shadows: adopt the saved history if it is at most this old
    write_prediction_log: int = 1
    entry_max_delay_seconds: int = 30    # drop a queued entry if it cannot be executed this soon after the minute close


class ScientificCore:
    """AI_test: frozen Bybit models at four horizons on live Bitfinex data (see the comment above TradeConfig).
    The platform needs: config, event_history, position_state (JSON-safe), cooldown_until_ms (int), last_features,
    _apply_event(event), decide(event, positions)."""

    def __init__(self, config: Optional[TradeConfig] = None) -> None:
        self.config = config or TradeConfig()
        self.event_history: Deque[CleanMarketEvent] = deque(maxlen=200)
        self.position_state: dict[str, dict[str, Any]] = {}
        self.cooldown_until_ms = 0
        self.last_features: dict[str, Any] = {}
        self.first_event_ms: Optional[int] = None
        self.book = _L3Book()
        self.hist: Deque[list] = deque(maxlen=HISTORY_KEEP_MINUTES)
        self.acc: Optional[_MinuteAcc] = None
        self.cur_sec: Optional[int] = None
        self.prev_mid = NAN
        self.last_closed: Optional[int] = None
        self.sec_trades: list[tuple[float, float]] = []
        self.entry_queue: Deque[dict[str, Any]] = deque()
        self.predictions: dict[str, dict[str, Any]] = {}
        self.history_mode = "lazy"          # "primary" (the live champion), "lazy" (shadows, replays), "off" (tests)
        self.history_checked = False
        self.backfilling = False
        self.model_error: Optional[str] = None
        self.status = "warming_up"

    # ================================ DATA ================================

    def on_runtime_initialize(self, context: dict[str, Any]) -> None:
        """Called by TradeRuntime.initialize(): the live app passes its project name, tests do not."""
        if context.get("project"):
            self.history_mode = "primary"
            self.history_checked = True
            self._restore_history(gapfill_until_ms=int(time.time() * 1000), max_gap_min=int(self.config.max_gapfill_minutes))
        else:
            self.history_mode = "off"

    def _apply_event(self, event: CleanMarketEvent) -> None:
        self.event_history.append(event)
        t = int(event.recv_time_ms)
        if self.first_event_ms is None:
            self.first_event_ms = t
        if not self.history_checked:
            self.history_checked = True
            if self.history_mode == "lazy":
                self._restore_history(gapfill_until_ms=t, max_gap_min=int(self.config.lazy_gapfill_minutes))
        self.ingest(event.kind, t, event.payload)

    def ingest(self, kind: str, t_ms: int, payload: Any) -> None:
        """One market event (live, recorded or replayed): advance the 1-second clock, then update book / trades."""
        sec = int(t_ms) // 1000
        self._advance(sec)
        if kind == "l3_update":
            if isinstance(payload, (list, tuple)) and len(payload) >= 3:
                self.book.update(int(payload[0]), float(payload[1]), float(payload[2]))
        elif kind == "l3_snapshot":
            self.book.load_snapshot([r for r in (payload or []) if isinstance(r, (list, tuple)) and len(r) >= 3])
        elif kind == "public_trade":
            if isinstance(payload, (list, tuple)) and len(payload) >= 4:
                amount, price = float(payload[2]), float(payload[3])
                if amount != 0 and price > 0:
                    self.sec_trades.append((amount, price))
        # public_trade_snapshot: past trades after a (re)connect, not counted (research replay rule)

    def _advance(self, sec: int) -> None:
        if self.cur_sec is None:
            self.cur_sec = sec
            return
        if sec <= self.cur_sec:
            return
        self._close_second(self.cur_sec)
        gap = sec - self.cur_sec - 1
        if gap <= int(self.config.carry_gap_seconds):
            for g in range(self.cur_sec + 1, sec):
                self._close_second(g)
        else:                                            # long outage: minutes without data, book state is stale
            self._roll_minutes_to(sec // 60 * 60)
            self.prev_mid = NAN
        self.cur_sec = sec

    def _roll_minutes_to(self, minute_s: int) -> None:
        if self.acc is not None and self.acc.minute < minute_s:
            self._finish_minute()
        last = (int(self.hist[-1][FI["t"]]) // 1000) if self.hist else None
        if last is not None:
            missing = (minute_s - last) // 60 - 1
            if missing > HISTORY_KEEP_MINUTES:
                self.hist.clear()
            else:
                for k in range(1, missing + 1):
                    self.hist.append(_empty_bar(last + 60 * k, self.hist[-1]))
                    self._minute_closed(backfill=True)

    def _close_second(self, s: int) -> None:
        row = self.book.second_row(self.sec_trades)
        self.sec_trades = []
        x = self._derive(row, s)
        minute = s // 60 * 60
        if self.acc is None or self.acc.minute != minute:
            self._roll_minutes_to(minute)
            self.acc = _MinuteAcc(minute)
        self.acc.add(s % 60, x)
        self.last_closed = s
        if s % 60 == 59:
            self._finish_minute()

    def _derive(self, r: dict[str, Any], s: int) -> dict[str, float]:
        """research.py derive_seconds + derive_l2 for one row"""
        bid, ask = r.get("bid1", NAN), r.get("ask1", NAN)
        mid = (bid + ask) / 2.0
        x: dict[str, float] = {"mid": mid}
        x["spread"] = (ask - bid) / mid * 1e4 if mid == mid else NAN
        bs, as_ = r.get("bsz1", NAN), r.get("asz1", NAN)
        tot = bs + as_
        if tot == tot and tot != 0:
            x["bimb"] = (bs - as_) / tot
            x["micro"] = ((bid * as_ + ask * bs) / tot - mid) / mid * 1e4
        else:
            x["bimb"] = x["micro"] = NAN
        if mid == mid and self.prev_mid == self.prev_mid and s % 86400 != 0:
            rr = math.log(mid) - math.log(self.prev_mid)
            x["r2"] = rr * rr
        else:
            x["r2"] = NAN
        self.prev_mid = mid
        x["buy_vol"] = r.get("buy_vol", 0.0); x["sell_vol"] = r.get("sell_vol", 0.0); x["n_trades"] = float(r.get("n_trades", 0))
        x["big_buy"] = r.get("big_buy", 0.0); x["big_sell"] = r.get("big_sell", 0.0)
        x["n_large"] = x["big_buy"] + x["big_sell"]
        x["ofi"] = r["ofi"]
        for c in ("add_b", "rem_b", "add_a", "rem_a"):
            x[c] = r[c]
        x["added"] = r["add_b"] + r["add_a"]; x["removed"] = r["rem_b"] + r["rem_a"]
        for lvl, k in (("05", "0.5"), ("1", "1"), ("3", "3")):
            b, a = r.get(f"bd{k}", NAN), r.get(f"ad{k}", NAN)
            t = b + a
            if t == t and t != 0:
                x[f"dimb{lvl}"] = (b - a) / t; x[f"dtot{lvl}"] = t
            else:
                x[f"dimb{lvl}"] = x[f"dtot{lvl}"] = NAN
        lv = r.get("lv")
        if lv is not None:
            bd = [abs(lv[i]) for i in range(10)]
            bsz = [lv[10 + i] if lv[10 + i] != lv[10 + i] else max(lv[10 + i], 0.0) for i in range(10)]
            ad = [abs(lv[20 + i]) for i in range(10)]
            asz = [lv[30 + i] if lv[30 + i] != lv[30 + i] else max(lv[30 + i], 0.0) for i in range(10)]
            for k in (3, 5, 10):
                sb, sa = sum(bsz[:k]), sum(asz[:k])
                x[f"imb{k}"] = (sb - sa) / (sb + sa + EPS)
            swb = sum(bsz[i] * math.exp(-bd[i] / L2_WEIGHT_BPS) for i in range(10))
            swa = sum(asz[i] * math.exp(-ad[i] / L2_WEIGHT_BPS) for i in range(10))
            x["wimb10"] = (swb - swa) / (swb + swa + EPS)
            sb10, sa10 = sum(bsz), sum(asz)
            x["slope_b"] = sum(bsz[i] * bd[i] for i in range(10)) / (sb10 + EPS)
            x["slope_a"] = sum(asz[i] * ad[i] for i in range(10)) / (sa10 + EPS)
            x["conc_b"] = bsz[0] / (sb10 + EPS); x["conc_a"] = asz[0] / (sa10 + EPS)
        else:
            for c in ("imb3", "imb5", "imb10", "wimb10", "slope_b", "slope_a", "conc_b", "conc_a"):
                x[c] = NAN
        b05, a05, b3, a3 = r.get("bd0.5", NAN), r.get("ad0.5", NAN), r.get("bd3", NAN), r.get("ad3", NAN)
        x["near_b"] = b05 / (b3 + EPS); x["near_a"] = a05 / (a3 + EPS)
        x["bd05"], x["ad05"] = b05, a05
        x["bd1"], x["ad1"] = r.get("bd1", NAN), r.get("ad1", NAN)
        bt, at_ = r.get("bdtot", NAN), r.get("adtot", NAN)
        x["tot_imb"] = (bt - at_) / (bt + at_ + EPS)
        rb, ra = r.get("breach", NAN), r.get("areach", NAN)
        x["reach_asym"] = (rb - ra) / (rb + ra + EPS)
        return x

    def _finish_minute(self) -> None:
        if self.acc is None:
            return
        prev = self.hist[-1] if self.hist else None
        self.hist.append(self.acc.bar(prev))
        self.acc = None
        self._minute_closed(backfill=self.backfilling)

    # ================================ FEATURES ================================

    def _col(self, name: str, n: int) -> list:
        i = FI[name]; h = self.hist
        L = len(h)
        return [h[j][i] for j in range(max(0, L - n), L)]

    def features(self) -> dict[str, float]:
        """research.py build_features() for the latest minute of self.hist"""
        h = self.hist; F = FI; cur = h[-1]; L = len(h)
        g = lambda name: cur[F[name]]
        f: dict[str, float] = {}
        closes = self._col("close", 241)
        lc = [_log(c) for c in closes]
        for n in (1, 5, 15, 60, 240):
            f[f"ret_{n}"] = (lc[-1] - lc[-1 - n]) * 1e4 if len(lc) > n else NAN
        for n in (5, 15, 60, 240, 1440):
            f[f"lrv_{n}"] = _log(_rsum(self._col("rv", n), max(2, n // 2)) / n + 1e-12)
        f["rvr_5_60"] = f["lrv_5"] - f["lrv_60"]
        f["rvr_60_1440"] = f["lrv_60"] - f["lrv_1440"]
        sig = _rmean(self._col("rv", SIGMA_WINDOW_MIN), 60)
        sig = math.sqrt(sig) * 1e4 if sig == sig else NAN
        lc241 = [_log(c) for c in self._col("close", SIGMA_WINDOW_MIN + 1)]
        ret1 = [(lc241[i] - lc241[i - 1]) * 1e4 for i in range(1, len(lc241))]
        if len(lc241) <= SIGMA_WINDOW_MIN:                # the first bar has no previous close: ret_1 is NaN there
            ret1 = [NAN] + ret1
        sig_alt = _rstd(ret1[-SIGMA_WINDOW_MIN:], 60)
        if not (sig == sig and sig > 0):
            sig = sig_alt
        if sig == sig and sig < 0.5:
            sig = 0.5
        f["sigma1"] = sig
        c = g("close")
        for n in (60, 240):
            hi, lo = _rmax(self._col("high", n), n), _rmin(self._col("low", n), n)
            f[f"rpos_{n}"] = _div0(c - lo, hi - lo)
        hi15, lo15 = _rmax(self._col("high", 15), 15), _rmin(self._col("low", 15), 15)
        f["hl15_sig"] = _log(hi15 / lo15) * 1e4 / (sig * math.sqrt(15)) if hi15 == hi15 and lo15 == lo15 and sig == sig else NAN
        ts = _datetime.fromtimestamp(cur[F["t"]] / 1000, tz=_timezone.utc)
        mins = ts.hour * 60 + ts.minute
        f["tod_sin"] = math.sin(2 * math.pi * mins / 1440); f["tod_cos"] = math.cos(2 * math.pi * mins / 1440)
        f["dow"] = float(ts.weekday())
        f["mins_to_funding"] = float(min((hh * 60 - mins) % 1440 for hh in FUNDING_HOURS_UTC))
        buy, sell = self._col("buy_vol", LONG_WIN), self._col("sell_vol", LONG_WIN)
        for n in (1, 5, 15, 60):
            bs_, ss_ = _rsum(buy[-n:], n), _rsum(sell[-n:], n)
            f[f"timb_{n}"] = _div0(bs_ - ss_, bs_ + ss_)
        f["timb_tail"] = _div0(g("buy_tail") - g("sell_tail"), g("buy_tail") + g("sell_tail"))
        vol = [a + b for a, b in zip(buy, sell)]
        vma = _rmean(vol, LONG_MIN)
        for n in (1, 5, 60):
            f[f"volz_{n}"] = _log((_rmean(vol[-n:], n) + 1e-9) / (vma + 1e-9))
        nl = self._col("n_large", LONG_WIN)
        nlma = _rmean(nl, LONG_MIN)
        f["nlarge_5"] = _log((_rmean(nl[-5:], 5) + 1e-3) / (nlma + 1e-3))
        f["nlarge_60"] = _log((_rmean(nl[-60:], 60) + 1e-3) / (nlma + 1e-3))
        ofi = self._col("ofi", LONG_WIN)
        ofi_sd = _rstd(ofi, LONG_MIN)
        ofi_sd = NAN if ofi_sd == 0 else ofi_sd
        for n in (1, 5, 15):
            f[f"ofi_{n}"] = _rsum(ofi[-n:], n) / (ofi_sd * math.sqrt(n))
        f["ofi_tail"] = g("ofi_tail") / ofi_sd
        net = [a - b for a, b in zip(self._col("added", LONG_WIN), self._col("removed", LONG_WIN))]
        sd = _rstd(net, LONG_MIN)
        sd = NAN if sd == 0 else sd
        f["netadd_1"] = net[-1] / sd
        f["netadd_5"] = _rsum(net[-5:], 5) / (sd * math.sqrt(5))
        f["spread_end"] = g("spread_end")
        f["spread_m5"] = _rmean(self._col("spread_mean", 5), 5)
        f["bimb_end"] = g("bimb_end")
        f["bimb_m5"] = _rmean(self._col("bimb_mean", 5), 5)
        f["bimb_m15"] = _rmean(self._col("bimb_mean", 15), 15)
        f["micro_end"] = g("micro_end")
        for lvl in ("05", "1", "3"):
            f[f"dimb{lvl}_end"] = g(f"dimb{lvl}_end")
            f[f"dimb{lvl}_m5"] = _rmean(self._col(f"dimb{lvl}_mean", 5), 5)
            f[f"ldtot{lvl}_end"] = _log(g(f"dtot{lvl}_end") + 1e-9)
        f["dtot1_chg"] = _log((g("dtot1_end") + 1e-9) / (_rmean(self._col("dtot1_mean", 60), 60) + 1e-9))
        # ---- full L2 block (research.py v2)
        for k in (3, 5, 10):
            f[f"l2_imb{k}"] = g(f"l2_imb{k}_end")
        f["l2_wimb10"] = g("l2_wimb10_end")
        f["l2_imb10_m15"], f["l2_wimb10_m15"] = g("l2_imb10_m15"), g("l2_wimb10_m15")
        f["l2_imb10_d15"] = g("l2_imb10_end") - g("l2_imb10_at44")
        f["l2_imb10_d60"] = g("l2_imb10_end") - g("l2_imb10_at0")
        f["l2_wimb10_d15"] = g("l2_wimb10_end") - g("l2_wimb10_at44")
        f["l2_slope_b"], f["l2_slope_a"] = g("l2_slope_b_end"), g("l2_slope_a_end")
        f["l2_slope_asym"] = _log((g("l2_slope_a_end") + 1e-3) / (g("l2_slope_b_end") + 1e-3))
        f["l2_conc_b"], f["l2_conc_a"] = g("l2_conc_b_end"), g("l2_conc_a_end")
        f["l2_conc_asym"] = g("l2_conc_b_end") - g("l2_conc_a_end")
        f["l2_near_b"], f["l2_near_a"] = g("l2_near_b_end"), g("l2_near_a_end")
        f["l2_near_asym"] = g("l2_near_b_end") - g("l2_near_a_end")
        f["l2_tot_imb"], f["l2_reach_asym"] = g("l2_tot_imb_end"), g("l2_reach_asym_end")
        f["l2_micro_m15"] = g("l2_micro_m15")
        for w, s in ((5, 54), (15, 44), (30, 29), (60, 0)):
            f[f"l2_micro_d{w}"] = g("micro_end") - g(f"l2_micro_at{s}")
        for w, s in ((5, 54), (15, 44), (30, 29)):
            f[f"l2_mret_{w}"] = _log(c / g(f"l2_mid_at{s}")) * 1e4
        f["l2_bimb_d15"] = g("bimb_end") - g("l2_bimb_at44")
        f["l2_bimb_d60"] = g("bimb_end") - g("l2_bimb_at0")
        f["l2_dimb05_d15"] = g("dimb05_end") - g("l2_dimb05_at44")
        for w in (5, 15, 30):
            f[f"l2_ofi_{w}"] = g(f"l2_ofi_w{w}") / (ofi_sd * math.sqrt(w / 60))
        for w in (5, 15, 30):
            bw, sw = g(f"l2_buy_vol_w{w}"), g(f"l2_sell_vol_w{w}")
            f[f"l2_timb_{w}"] = _div0(bw - sw, bw + sw)
        bd1m, ad1m = g("l2_bd1_mean"), g("l2_ad1_mean")
        f["l2_net_b_15"] = _asinh((g("l2_add_b_w15") - g("l2_rem_b_w15")) / (bd1m + EPS))
        f["l2_net_a_15"] = _asinh((g("l2_add_a_w15") - g("l2_rem_a_w15")) / (ad1m + EPS))
        f["l2_net_asym_15"] = f["l2_net_b_15"] - f["l2_net_a_15"]
        f["l2_net_b_60"] = _asinh((g("l2_add_b_sum") - g("l2_rem_b_sum")) / (bd1m + EPS))
        f["l2_net_a_60"] = _asinh((g("l2_add_a_sum") - g("l2_rem_a_sum")) / (ad1m + EPS))
        f["l2_net_asym_60"] = f["l2_net_b_60"] - f["l2_net_a_60"]
        f["l2_dep05_b_d15"] = _log((g("l2_bd05_end") + 1e-3) / (g("l2_bd05_at44") + 1e-3))
        f["l2_dep05_a_d15"] = _log((g("l2_ad05_end") + 1e-3) / (g("l2_ad05_at44") + 1e-3))
        f["l2_dep05_asym_d15"] = f["l2_dep05_b_d15"] - f["l2_dep05_a_d15"]
        f["l2_resil_b"] = _log((g("l2_resb_num") + 0.01) / (g("l2_resb_den") + 0.01))
        f["l2_resil_a"] = _log((g("l2_resa_num") + 0.01) / (g("l2_resa_den") + 0.01))
        f["l2_resil_asym"] = f["l2_resil_b"] - f["l2_resil_a"]
        f["l2_flow_dep_60"] = _asinh((g("buy_vol") - g("sell_vol")) / (0.5 * (bd1m + ad1m) + EPS))
        f["l2_buy_dep_15"] = _asinh(g("l2_buy_vol_w15") / (ad1m + EPS))
        f["l2_sell_dep_15"] = _asinh(g("l2_sell_vol_w15") / (bd1m + EPS))
        f["l2_flow_dep_15"] = f["l2_buy_dep_15"] - f["l2_sell_dep_15"]
        bb, bsl = _nz(g("l2_big_buy_sum")), _nz(g("l2_big_sell_sum"))
        f["l2_big_imb"] = (bb - bsl) / (bb + bsl + 1e-6)
        bb15 = sum(_nz(x) for x in self._col("l2_big_buy_sum", 15)); bs15 = sum(_nz(x) for x in self._col("l2_big_sell_sum", 15))
        f["l2_big_imb_15m"] = (bb15 - bs15) / (bb15 + bs15 + 1e-6) if L >= 15 else NAN
        # agreement / extremes across the sign-aligned signals: trailing 1440-min z-scores of each signal
        for s in L2_SIGNALS_ALL:
            cur[F[f"sig_{s}"]] = f.get(s, NAN)
        signals = load_models().get("l2_signals", list(L2_SIGNALS_ALL)) if not self.model_error else list(L2_SIGNALS_ALL)
        zs = []
        for s in signals:
            xs = self._col(f"sig_{s}", LONG_WIN)
            mu, sd_ = _rmean(xs, LONG_MIN), _rstd(xs, LONG_MIN)
            sd_ = NAN if sd_ == 0 else sd_
            z = (xs[-1] - mu) / sd_ if xs[-1] == xs[-1] and mu == mu and sd_ == sd_ else NAN
            zs.append(max(-5.0, min(5.0, z)) if z == z else NAN)
        ok = [z for z in zs if z == z]
        f["l2_agree"] = float(sum((1.0 if z > 0 else (-1.0 if z < 0 else 0.0)) * (abs(z) > 1) for z in ok)) if ok else NAN
        f["l2_zsum"] = float(sum(max(-3.0, min(3.0, z)) for z in ok)) if ok else NAN
        f["l2_n_extreme"] = float(sum(abs(z) > 2 for z in ok))
        return f

    def _minute_closed(self, backfill: bool = False) -> None:
        """a minute bar was appended: features, predictions for every horizon, queued entries, log, history"""
        c = self.config
        cur = self.hist[-1]
        try:
            models = load_models()
        except Exception as exc:                           # fail visibly, never invent predictions
            self.model_error = f"{type(exc).__name__}: {exc}"; self.status = "models_missing"
            return
        f = self.features()
        self.feature_values = f
        names = models["feature_names"]
        missing = [n for n in names if n not in f]
        if missing:                                        # the model expects a feature this code does not build
            self.model_error = f"features missing: {missing[:5]}"; self.status = "models_missing"
            return
        x = [f.get(n, NAN) for n in names]
        n_hist = len(self.hist)
        quality = bool(cur[FI["n_rows"]] >= c.min_rows_per_minute and f["lrv_1440"] == f["lrv_1440"] and f["sigma1"] == f["sigma1"])
        warm = n_hist >= int(c.min_history_minutes)
        preds: dict[str, dict[str, Any]] = {}
        for H in HORIZONS:
            hm = models["horizons"][str(H)]
            p_up = _sigmoid(_tree_raw(hm["models"]["dir"], x))
            p_hit = _sigmoid(_tree_raw(hm["models"]["hit"], x))
            vol_raw = _tree_raw(hm["models"]["vol"], x)
            score = p_up - hm["prior_up"]
            th = hm["thresholds"]
            hit_thr = _interp(th["p_hit_quantiles"], float(c.hit_quantile))
            score_thr = _interp(th["abs_score_quantiles"], float(c.score_quantile))
            signal = None
            if p_hit >= hit_thr and abs(score) >= score_thr:
                signal = "long" if score > 0 else "short"
            preds[str(H)] = {"p_up": p_up, "p_hit": p_hit, "score": score, "vol_raw": vol_raw,
                             "vol_bps": math.sqrt(math.exp(vol_raw) * H) * 1e4, "hit_thr": hit_thr, "score_thr": score_thr,
                             "signal": signal}
            enabled = int(getattr(c, f"h{H}_enabled", 1)) == 1
            if signal and enabled and warm and not backfill and f["sigma1"] == f["sigma1"]:
                self.entry_queue.append({"H": H, "side": signal, "minute_ms": int(cur[FI["t"]]), "p_hit": p_hit, "score": score,
                                         "sigma1": f["sigma1"], "queued_ms": int(cur[FI["t"]]) + 60_000})
        self.predictions = preds
        self.status = "active" if warm else "warming_up"
        self.last_features = {"history_minutes": n_hist, "quality": quality, "warm": warm, "sigma1_bps": f["sigma1"],
                              "spread_end_bps": f["spread_end"], "close": cur[FI["close"]], "minute_ms": int(cur[FI["t"]]),
                              **{f"h{H}_{k}": preds[str(H)][k] for H in HORIZONS for k in ("p_hit", "score", "vol_bps", "signal")}}
        if self.history_mode == "primary":
            if int(c.write_prediction_log):
                self._log_predictions(cur, f, preds, n_hist, quality, backfill)
            every = max(1, int(c.save_history_every_minutes))
            if int(c.save_history) and not self.backfilling and (int(cur[FI["t"]]) // 60_000) % every == every - 1:
                self._save_history()

    # ================================ HISTORY / LOG ================================

    def _log_predictions(self, cur: list, f: dict[str, float], preds: dict, n_hist: int, quality: bool, backfill: bool) -> None:
        try:
            t = int(cur[FI["t"]])
            d = _runtime_dir() / "aitest_predictions"
            d.mkdir(parents=True, exist_ok=True)
            row = {"t": t, "open": cur[FI["open"]], "high": cur[FI["high"]], "low": cur[FI["low"]], "close": cur[FI["close"]], "rv": cur[FI["rv"]],
                   "n_rows": cur[FI["n_rows"]], "spread_end": cur[FI["spread_end"]], "sigma1": f["sigma1"], "history_min": n_hist,
                   "quality": quality, "backfilled": bool(backfill),
                   **{f"H{H}": {k: preds[str(H)][k] for k in ("p_up", "p_hit", "score", "vol_raw", "signal")} for H in HORIZONS}}
            line = json.dumps(row, allow_nan=True, separators=(",", ":")).replace("NaN", "null")
            with open(d / f"{time.strftime('%Y-%m-%d', time.gmtime(t / 1000))}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as exc:  # logging must never stop trading
            self.last_features["log_error"] = repr(exc)

    def _save_history(self) -> None:
        try:
            if self.acc is not None or self.last_closed is None or self.last_closed % 60 != 59:
                return
            data = {"schema": HISTORY_SCHEMA, "fields": BAR_FIELDS, "last_sec": self.last_closed, "prev_mid": self.prev_mid,
                    "bars": list(self.hist), "saved_at": time.time(),
                    "book": {"orders": [[k, p, a] for k, (p, a) in self.book.orders.items()],
                             "bids": list(self.book.bids.items()), "asks": list(self.book.asks.items()),
                             "btot": self.book.btot, "atot": self.book.atot}}
            path = _runtime_dir() / HISTORY_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with _gzip.open(tmp, "wt", encoding="utf-8", compresslevel=3) as fh:
                json.dump(data, fh, separators=(",", ":"))
            _os.replace(tmp, path)
        except Exception as exc:
            self.last_features["history_error"] = repr(exc)

    def _restore_history(self, gapfill_until_ms: int, max_gap_min: int) -> None:
        """adopt the saved minute history + book, then refill the gap from the app's market recordings"""
        path = _runtime_dir() / HISTORY_FILE
        try:
            if not path.is_file():
                return
            with _gzip.open(path, "rt", encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("schema") != HISTORY_SCHEMA or list(data.get("fields") or []) != list(BAR_FIELDS):
                return
            last_sec = int(data["last_sec"])
            if not (last_sec * 1000 < gapfill_until_ms <= last_sec * 1000 + max_gap_min * 60_000 + 60_000):
                return                                     # too old, or from the future of a replay
            self.hist.clear()
            for b in data["bars"]:
                self.hist.append([NAN if v is None else v for v in b])
            bk = data["book"]
            self.book.clear()
            self.book.orders = {int(k): (float(p), float(a)) for k, p, a in bk["orders"]}
            self.book.bids = {float(p): float(s) for p, s in bk["bids"]}
            self.book.asks = {float(p): float(s) for p, s in bk["asks"]}
            self.book.btot, self.book.atot = float(bk["btot"]), float(bk["atot"])
            self.book.reset_second()
            self.cur_sec = last_sec + 1
            self.last_closed = last_sec
            self.prev_mid = NAN if data.get("prev_mid") is None else float(data["prev_mid"])
            self.acc = None
            self.last_features["history_restored_minutes"] = len(self.hist)
            self._gapfill((last_sec + 1) * 1000, gapfill_until_ms)
        except Exception as exc:
            self.last_features["history_error"] = repr(exc)

    def _gapfill(self, start_ms: int, end_ms: int) -> None:
        directory = _runtime_dir() / "recordings"
        if not directory.is_dir():
            return
        lo = time.strftime("%Y%m%d-%H", time.gmtime(start_ms / 1000)); hi = time.strftime("%Y%m%d-%H", time.gmtime(end_ms / 1000))
        files = sorted(p for p in directory.glob("*.jsonl.gz") if lo <= p.name[:11] <= hi)
        n = 0
        self.backfilling = True
        try:
            for p in files:
                try:
                    with _gzip.open(p, "rt", encoding="utf-8") as fh:
                        for line in fh:
                            try:
                                row = json.loads(line)
                            except ValueError:
                                continue
                            t = int(row.get("t") or 0)
                            if start_ms <= t < end_ms:
                                self.ingest(str(row.get("k")), t, row.get("p")); n += 1
                except (EOFError, OSError):
                    continue
        finally:
            self.backfilling = False
        self.last_features["gapfill_events"] = n

    # ================================ STRATEGY / RISK ================================

    def _hold(self, event: CleanMarketEvent, reason: str, positions: int) -> TradeDecision:
        return TradeDecision(action="HOLD", symbol=event.symbol, reason=reason, metadata=self._metadata(reason, positions))

    def _metadata(self, state: str, positions: int, **extra: Any) -> dict[str, Any]:
        lf = self.last_features
        m = {"strategy_tag": extra.pop("slot", state), "strategy_state": state, "history_minutes": lf.get("history_minutes"),
             "open_positions": positions, "sigma1_bps": lf.get("sigma1_bps")}
        for H in HORIZONS:
            m[f"h{H}_p_hit"] = lf.get(f"h{H}_p_hit"); m[f"h{H}_score"] = lf.get(f"h{H}_score")
        m.update(extra)
        return m

    def _check_exit(self, event: CleanMarketEvent, pid: str, pos: PositionState, n_open: int) -> Optional[TradeDecision]:
        st = self.position_state.get(pid)
        if st is None:                                     # after a restart without strategy state: rebuild from the broker
            H = int(pid.split("-")[0][1:]) if pid.startswith("H") and "-" in pid else 60
            st = {"slot": f"H{H}", "H": H, "side": pos.side, "entry_ms": pos.entry_time_ms, "entry_est": pos.entry_price,
                  "tp": pos.target_price, "sl": pos.stop_price,
                  "expire_ms": pos.entry_time_ms + int(pos.max_hold_seconds or H * 60) * 1000}
            self.position_state[pid] = st
        long_ = pos.side == "long"
        px = float(event.best_bid) if long_ else float(event.best_ask)
        sign = 1.0 if long_ else -1.0
        net = sign * (px - pos.entry_price) / pos.entry_price * 1e4 - 2 * self.config.paper_fee_bps_each_side - self.config.paper_slippage_bps_each_side
        why = None
        if st.get("tp") is not None and sign * (px - float(st["tp"])) >= 0:
            why = "target"
        elif st.get("sl") is not None and sign * (px - float(st["sl"])) <= 0:
            why = "stop"
        elif int(event.recv_time_ms) >= int(st["expire_ms"]):
            why = "time"
        if not why:
            return None
        self.position_state.pop(pid, None)
        reason = f"{st['slot']}_{why}"
        return TradeDecision(action="EXIT", symbol=event.symbol, reason=reason, confidence=1.0,
                             metadata=self._metadata(reason, n_open - 1, position_id=pid, slot=st["slot"], estimated_net_bps=net))

    def _try_entry(self, event: CleanMarketEvent, e: dict[str, Any], held: dict[str, PositionState]) -> Optional[TradeDecision]:
        c = self.config; now = int(event.recv_time_ms); H = int(e["H"]); slot = f"H{H}"
        if now - int(e["queued_ms"]) > int(c.entry_max_delay_seconds) * 1000:
            return None
        if event.spread_bps is None or not math.isfinite(float(event.spread_bps)) or not 0 <= float(event.spread_bps) <= c.max_spread_bps:
            return None
        if sum(1 for pid in held if pid.startswith(slot + "-")) >= int(c.max_positions_per_horizon):
            return None
        slip = c.paper_slippage_bps_each_side / 1e4
        long_ = e["side"] == "long"
        entry = float(event.best_ask) * (1 + slip) if long_ else float(event.best_bid) * (1 - slip)
        d_bps = float(c.barrier_k) * float(e["sigma1"]) * math.sqrt(H)
        sign = 1.0 if long_ else -1.0
        tp = entry * math.exp(sign * d_bps / 1e4); sl = entry * math.exp(-sign * d_bps / 1e4)
        pid = f"{slot}-{int(e['minute_ms'])}"
        if pid in held:
            return None
        self.position_state[pid] = {"slot": slot, "H": H, "side": e["side"], "entry_ms": now, "entry_est": entry, "tp": tp, "sl": sl,
                                    "expire_ms": now + H * 60_000, "p_hit": e["p_hit"], "score": e["score"], "barrier_bps": d_bps}
        reason = f"{slot}_{e['side']}"
        return TradeDecision(action="ENTER_LONG" if long_ else "ENTER_SHORT", symbol=event.symbol, requested_size=float(c.base_size),
                             confidence=float(e["p_hit"]), reason=reason, stop_price=sl, target_price=tp, max_hold_seconds=H * 60,
                             metadata=self._metadata(reason, len(held) + 1, position_id=pid, slot=slot, horizon_min=H,
                                                     p_hit=e["p_hit"], dir_score=e["score"], barrier_bps=d_bps, estimated_net_bps=0.0))

    def decide(self, event: CleanMarketEvent, positions: list[PositionState]) -> TradeDecision:
        self._apply_event(event)
        held = {(p.position_id or p.symbol): p for p in positions}
        for pid in list(self.position_state):
            if pid not in held and not pid.startswith("_"):
                self.position_state.pop(pid, None)
        for pid, pos in held.items():
            d = self._check_exit(event, pid, pos, len(held))
            if d is not None:
                return d
        while self.entry_queue:
            d = self._try_entry(event, self.entry_queue.popleft(), held)
            if d is not None:
                return d
        if self.model_error:
            return self._hold(event, "models_missing", len(held))
        return self._hold(event, "in_position" if held else self.status, len(held))

# ============================================================================
# EDITABLE:SCIENTIFIC_WORKSPACE END
# ============================================================================


# ============================================================================
# PROTECTED:RUNTIME BEGIN
# Public runtime contract. Do not modify signatures or integrity routing.
# ============================================================================

class TradeRuntime:
    def __init__(self,symbol:Optional[str]=None,starting_equity_usd:Optional[float]=None,config:Optional[TradeConfig]=None,market_config:Optional[dict[str,Any]]=None)->None:
        self.market_config=dict(DEFAULT_MARKET_CONFIG); self.market_config.update(market_config or {}); self.symbol=symbol or str(self.market_config.get("symbol") or TRADE_META["symbols"][0]); self.market_config["symbol"]=self.symbol; self.input=_ProtectedMarketInput(self.symbol,float(self.market_config.get("stale_seconds",5.0))); self.broker=_ProtectedPaperBroker(starting_equity_usd or float(PAPER_DEFAULTS["starting_equity_usd"])); self.core=ScientificCore(config); self.signals:Deque[dict[str,Any]]=deque(maxlen=2000); self.last_event:Optional[CleanMarketEvent]=None; self.initialized=False
    def initialize(self,context:Optional[dict[str,Any]]=None)->None:
        self.initialized=True; hook=getattr(self.core,"on_runtime_initialize",None)
        if callable(hook): hook(dict(context or {}))
    def _handle_events(self,events:list[CleanMarketEvent])->TradeDecision:
        last=TradeDecision(action="HOLD",symbol=self.symbol,reason="awaiting_verified_market_evidence")
        for event in events:
            if not event.integrity.valid: raise L3IntegrityError("protected layer attempted to release unhealthy evidence")
            if (event.best_bid is None or event.best_ask is None or
                not math.isfinite(float(event.best_bid)) or not math.isfinite(float(event.best_ask)) or
                float(event.best_bid)<=0 or float(event.best_bid)>=float(event.best_ask)):
                raise L3IntegrityError("invalid_executable_quote")
            self.last_event=event
            # Delayed checksum verification may release historical evidence.
            # Reconstruct features, but only a current quote may execute.
            if event is not events[-1] or time.time_ns()-event.recv_time_ns > 2_000_000_000:
                if hasattr(self.core,"_apply_event"): self.core._apply_event(event)
                continue
            if hasattr(getattr(self.core,"config",None),"assumed_equity_usd"):
                self.core.config.assumed_equity_usd=max(0.0,self.broker.equity(event))
            decision=self.core.decide(event,list(self.broker.positions.values()))
            if not isinstance(decision,TradeDecision): raise TradeContractError("ScientificCore.decide must return TradeDecision")
            self.signals.append({"timestamp_ms":event.recv_time_ms,"event_kind":event.kind,"sequence":event.sequence,"decision":decision.as_dict()}); self.broker.execute(decision,event)
            if getattr(self,"persist_callback",None): self.persist_callback(self)
            last=decision
        return last
    async def run_live_market_data(self)->None:
        await _BitfinexPublicFeed(self,self.market_config).run()
    def on_l3_snapshot(self,entries:Iterable[Any],sequence:Optional[int],recv_time_ns:Optional[int]=None,channel_id:Optional[int]=None,raw_message:Optional[str]=None)->TradeDecision:
        return self._handle_events(self.input.snapshot(entries,sequence,recv_time_ns or time.time_ns(),channel_id,raw_message))
    def on_l3_update(self,entry:Any,sequence:Optional[int],recv_time_ns:Optional[int]=None,channel_id:Optional[int]=None,raw_message:Optional[str]=None)->TradeDecision:
        return self._handle_events(self.input.update(entry,sequence,recv_time_ns or time.time_ns(),channel_id,raw_message))
    def on_l3_checksum(self,server_checksum:int,sequence:Optional[int],recv_time_ns:Optional[int]=None,channel_id:Optional[int]=None,raw_message:Optional[str]=None)->TradeDecision:
        return self._handle_events(self.input.checksum(server_checksum,sequence,recv_time_ns or time.time_ns(),channel_id,raw_message))
    def on_public_trade_snapshot(self,trades:Iterable[Any],sequence:Optional[int],recv_time_ns:Optional[int]=None,channel_id:Optional[int]=None,raw_message:Optional[str]=None)->TradeDecision:
        return self._handle_events(self.input.trade_snapshot(trades,sequence,recv_time_ns or time.time_ns(),channel_id,raw_message))
    def on_public_trade(self,trade:Any,sequence:Optional[int],recv_time_ns:Optional[int]=None,channel_id:Optional[int]=None,raw_message:Optional[str]=None)->TradeDecision:
        return self._handle_events(self.input.trade(trade,sequence,recv_time_ns or time.time_ns(),channel_id,raw_message))
    def get_status(self)->dict[str,Any]:
        i=self.input.integrity(); return {"name":TRADE_META["name"],"version":TRADE_META["version"],"contract_version":TRADE_CONTRACT_VERSION,"symbol":self.symbol,"paper_only":True,"initialized":self.initialized,"position_count":len(self.broker.positions),"market_source":"bitfinex_public_ws","live_feed_configured":True,"connected":i.connected,"verified":i.verified}
    def get_positions(self): return [p.as_dict() for p in self.broker.positions.values()]
    def get_recent_signals(self,limit:int=100): return list(self.signals)[-int(limit):]
    def get_recent_executions(self,limit:int=100): return [x.as_dict() for x in self.broker.executions[-int(limit):]]
    def get_metrics(self): return self.broker.metrics(self.last_event).as_dict()
    def health(self)->dict[str,Any]:
        i=self.input.integrity(); state="healthy" if i.valid else ("disconnected" if not i.connected else ("stale" if i.stale else ("verifying" if not i.verified else "integrity_error"))); message="ok" if i.valid else (i.error or state); return HealthStatus(i.valid,state,message,i).as_dict()
    def get_ui_schema(self): return {"meta":dict(TRADE_META),"ui":dict(TRADE_UI_SPEC)}
    def get_ui_snapshot(self):
        custom={}; metadata=self.signals[-1]["decision"].get("metadata",{}) if self.signals else {}
        for item in TRADE_UI_SPEC.get("custom_metrics",[]): custom[item.get("key")]=metadata.get(item.get("key"))
        return {"status":self.get_status(),"health":self.health(),"positions":self.get_positions(),"signals":self.get_recent_signals(20),"executions":self.get_recent_executions(20),"metrics":self.get_metrics(),"custom_metrics":custom}

def create_trade_system(**kwargs:Any)->TradeRuntime: return TradeRuntime(**kwargs)

def run_contract_self_test()->dict[str,Any]:
    r=create_trade_system(); r.initialize({"test":True}); r.input.mark_connected(); r.input.mark_subscribed("book"); r.input.mark_subscribed("trades"); now=time.time_ns(); snapshot=[[101,Decimal("100.00"),Decimal("1.0")],[102,Decimal("100.10"),Decimal("-1.0")]]
    before=len(r.core.event_history); r.on_l3_snapshot(snapshot,1,now,11,'[11,[[101,100.00,1.0],[102,100.10,-1.0]],1]'); assert len(r.core.event_history)==before
    cs=r.input.book.checksum(); r.on_l3_checksum(cs,2,now+1_000_000,11,f'[11,"cs",{cs},2]'); assert len(r.core.event_history)>=2; assert r.health()["ok"]
    r.on_public_trade([9001,now//1_000_000,Decimal("0.01"),Decimal("100.05")],3,now+2_000_000,12,'[12,"te",[9001,0,0.01,100.05],3]'); assert r.core.event_history[-1].kind=="public_trade"
    assert _checksum_value(Decimal("0.00000003"))=="3e-8"
    assert set(REQUIRED_RUNTIME_METHODS).issubset(set(dir(r))); assert r.get_metrics()["accounting_valid"] is True
    e=r.last_event; b=r.broker; b.execute(TradeDecision("ENTER_LONG",r.symbol,0.01,metadata={"position_id":"t1"}),e); b.execute(TradeDecision("ENTER_SHORT",r.symbol,0.01,metadata={"position_id":"t2"}),e)
    assert sorted(b.positions)==["t1","t2"] and b.positions["t1"].position_id=="t1"; b.execute(TradeDecision("EXIT",r.symbol,metadata={"position_id":"t1"}),e)
    assert list(b.positions)==["t2"] and b.executions[-1].position_id=="t1" and r.get_metrics()["accounting_valid"] is True
    return {"ok":True,"contract_version":TRADE_CONTRACT_VERSION,"multi_position":True,"fee_bps":b.fee_bps,"slippage_bps":b.slippage_bps,"market_source":"bitfinex_public_ws","checksum_gate":True,"public_trades":True,"runtime_methods":list(REQUIRED_RUNTIME_METHODS)}

# ============================================================================
# PROTECTED:RUNTIME END
# ============================================================================


if __name__ == "__main__":
    print(run_contract_self_test())
